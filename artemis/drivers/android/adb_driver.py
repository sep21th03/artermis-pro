# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Concrete Android Device Driver implementation using ADB and UIAutomator2."""

import asyncio
import base64
import hashlib
from io import BytesIO
import os
from pathlib import Path
import time
from typing import Any, Literal

from adbutils import AdbClient, AdbDevice
from artemis.config.paths import get_temp_dir
from artemis.drivers.base import BaseDeviceDriver, KeyCode, ScreenData, SwipeDirection
from artemis.perf import PerfStage, STATUS_ERROR, STATUS_SUCCESS, perf_span, record_event
from artemis.runtime.adb_endpoint import current_adb_endpoint
from artemis.toolchain import find_ffmpeg, find_scrcpy
from artemis.utils.video import build_scrcpy_record_command
from artemis.utils.ui_filter import filter_ui_hierarchy
from third_party.mobile_use.clients.ui_automator_client import (
    UIAutomatorClient,
    parse_hierarchy_xml_to_elements,
)
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

# KeyCode mapping from string name to Android keycode integer
ANDROID_KEYCODE_MAP: dict[str, int] = {
    "home": 3,
    "back": 4,
    "enter": 66,
    "delete": 67,
    "power": 26,
    "app_switch": 187,
    "volume_up": 24,
    "volume_down": 25,
}


def _escape_for_adb_text(s: str) -> str:
    """Escapes special characters for adb shell input text."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("'", "\\'")
        .replace("`", "\\`")
        .replace("$", "\\$")
        .replace("&", "\\&")
        .replace("|", "\\|")
        .replace(";", "\\;")
        .replace("<", "\\<")
        .replace(">", "\\>")
        .replace("(", "\\(")
        .replace(")", "\\)")
        .replace("*", "\\*")
        .replace("?", "\\?")
        .replace("~", "\\~")
        .replace(" ", "%s")
    )


class AndroidAdbDriver(BaseDeviceDriver):
    """Android device driver using ADB and UIAutomator2."""

    def __init__(
        self,
        device_id: str,
        adb_client: AdbClient,
        ui_adb_client: UIAutomatorClient | None = None,
        width: int = 1080,
        height: int = 2400,
    ):
        self._device_id = device_id
        self._adb_client = adb_client
        self._ui_adb_client = ui_adb_client
        self._width = width
        self._height = height
        self._device: AdbDevice | None = None
        self._recording_process: asyncio.subprocess.Process | None = None
        self._recording_output_path: Path | None = None
        # ── Đo hiệu năng (chỉ quan sát) ─────────────────────────────────
        #: Hash UI lần quan sát trước, để biết một hành động có làm UI đổi hay không.
        self._last_ui_fingerprint: str | None = None
        #: Hành động vừa thực hiện, chờ lần quan sát kế tiếp để đối chiếu UI.
        self._pending_action: dict[str, Any] | None = None

    # ── Đo hiệu năng: ghi nhận hành động & đối chiếu UI ─────────────────
    def _note_action(self, action_type: str, span_id: str | None, **detail: Any) -> None:
        """Ghi nhớ hành động vừa thực hiện để lần quan sát kế tiếp đối chiếu UI.

        Không có bước này thì không thể trả lời hai câu hỏi của lớp đo: *hành động nào
        không làm UI thay đổi* và *mất bao lâu mới xác minh được UI sau hành động*.
        """
        if span_id is None:
            return
        self._pending_action = {
            "action_type": action_type,
            "span_id": span_id,
            "at": time.time(),
            "detail": detail,
        }

    def _resolve_pending_action(self, fingerprint: str | None, changed: bool | None) -> None:
        """Phát sự kiện ``action_outcome`` cho hành động vừa rồi (nếu có)."""
        pending = self._pending_action
        self._pending_action = None
        if not pending:
            return
        record_event(
            PerfStage.ADB_ACTION,
            "action_outcome",
            duration_ms=round((time.time() - float(pending["at"])) * 1000.0, 3),
            action_type=str(pending["action_type"]),
            ui_fingerprint=fingerprint,
            ui_changed=changed,
            detail={
                **dict(pending.get("detail") or {}),
                "action_span_id": pending["span_id"],
                "kind": "outcome",
            },
        )

    @staticmethod
    def _ui_fingerprint(ui_elements: list[dict[str, Any]], xml: str | None) -> str | None:
        """Hash trạng thái UI — chỉ để so sánh, không lưu nội dung màn hình."""
        parts: list[str] = []
        for element in ui_elements[:200]:
            if isinstance(element, dict):
                text = element.get("text") or element.get("content-desc") or ""
                resource = element.get("resource-id") or ""
                parts.append(f"{resource}|{text}")
        payload = "\n".join(parts) if parts else (xml or "")
        if not payload:
            return None
        return hashlib.sha1(payload.encode("utf-8", "ignore")).hexdigest()[:16]

    def _emit_action(
        self,
        action_type: str,
        started: float,
        *,
        status: str = STATUS_SUCCESS,
        **detail: Any,
    ) -> None:
        """Phát sự kiện cho một hành động thiết bị đã xong.

        ``started`` là mốc ``time.perf_counter()`` lấy ngay đầu method; duration đo từ
        đó nên bao gồm cả thời gian chờ ADB thật, không chỉ thời gian lệnh chạy.
        """
        error = detail.pop("error", None)
        error_type: str | None = None
        if isinstance(error, BaseException):
            error_type = type(error).__name__
            detail.setdefault("error_message", str(error)[:200])
        elif error is not None:
            error_type = str(error)[:120]
        event = record_event(
            PerfStage.ADB_ACTION,
            f"adb_{action_type}",
            duration_ms=round((time.perf_counter() - started) * 1000.0, 3),
            status=status,
            action_type=action_type,
            error_type=error_type,
            detail=detail,
        )
        if status == STATUS_SUCCESS and event is not None:
            self._note_action(action_type, event.span_id, **detail)

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._width, self._height)

    @property
    def device(self) -> AdbDevice:
        if self._device is None:
            self._device = self._adb_client.device(serial=self._device_id)
        return self._device

    async def connect(self) -> None:
        """Verify device availability."""
        try:
            state = self.device.get_state()
            logger.info(f"Android device '{self._device_id}' connected (state: {state})")
        except Exception as e:
            logger.warning(f"Connecting to Android device '{self._device_id}': {e}")

    async def disconnect(self) -> None:
        """Clean up ongoing recordings and tunnels."""
        if self._recording_process:
            await self.stop_video_recording()
        if self._ui_adb_client and hasattr(self._ui_adb_client, "disconnect"):
            await asyncio.to_thread(self._ui_adb_client.disconnect)

    async def get_screen_data(self, skip_settling: bool = False) -> ScreenData:
        """Captures screenshot and XML hierarchy concurrently."""
        # Đo hiệu năng: mốc thời gian cho từng pha của một lần quan sát màn hình.
        _perf_start = time.perf_counter()
        _settle_ms: float | None = None
        _screenshot_ms: float | None = None
        _hierarchy_ms: float | None = None
        _hierarchy_from_combined = False
        if not skip_settling:
            _t = time.perf_counter()
            await asyncio.sleep(0.3)
            _settle_ms = (time.perf_counter() - _t) * 1000.0

        # 1. Capture raw screenshot
        _t = time.perf_counter()
        screenshot_bytes: bytes = b""
        screenshot_base64: str = ""
        ui_hierarchy_xml: str | None = None
        ui_elements: list[dict[str, Any]] = []

        # Check if ui_adb_client has direct screen data support
        if self._ui_adb_client and hasattr(self._ui_adb_client, "get_screen_data"):
            try:
                raw_ui_data = self._ui_adb_client.get_screen_data()
                if (
                    hasattr(raw_ui_data, "base64")
                    and isinstance(raw_ui_data.base64, str)
                    and raw_ui_data.base64
                ):
                    screenshot_base64 = raw_ui_data.base64
                    screenshot_bytes = base64.b64decode(screenshot_base64)
                if hasattr(raw_ui_data, "width") and isinstance(raw_ui_data.width, int):
                    self._width = raw_ui_data.width
                if hasattr(raw_ui_data, "height") and isinstance(raw_ui_data.height, int):
                    self._height = raw_ui_data.height
                if hasattr(raw_ui_data, "hierarchy_xml") and isinstance(
                    raw_ui_data.hierarchy_xml, str
                ):
                    ui_hierarchy_xml = raw_ui_data.hierarchy_xml
                if hasattr(raw_ui_data, "elements"):
                    raw_elements = raw_ui_data.elements
                    if isinstance(raw_elements, dict):
                        ui_elements = [raw_elements]
                    elif isinstance(raw_elements, list):
                        ui_elements = raw_elements
            except Exception as e:
                logger.debug(f"Direct ui_adb_client.get_screen_data error: {e}")
        _hierarchy_from_combined = bool(ui_elements)

        if not screenshot_base64 or not isinstance(screenshot_base64, str):
            try:
                pil_img = await asyncio.to_thread(self.device.screenshot)
                if hasattr(pil_img, "save"):
                    buf = BytesIO()
                    pil_img.save(buf, format="PNG")
                    screenshot_bytes = buf.getvalue()
                    self._width, self._height = pil_img.size
                    screenshot_base64 = base64.b64encode(screenshot_bytes).decode("utf-8")
            except Exception as e:
                logger.warning(f"Device screenshot capture failed on {self._device_id}: {e}")

        if not screenshot_base64 or not isinstance(screenshot_base64, str):
            # Fallback for headless testing environments
            screenshot_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            screenshot_bytes = base64.b64decode(screenshot_base64)
        _screenshot_ms = (time.perf_counter() - _t) * 1000.0

        # 2. Capture UI Hierarchy via UIAutomator
        _t = time.perf_counter()
        _hierarchy_fetched = False
        if self._ui_adb_client and not ui_elements:
            _hierarchy_fetched = True
            try:
                if hasattr(self._ui_adb_client, "get_hierarchy"):
                    res = self._ui_adb_client.get_hierarchy()
                    hierarchy = await res if asyncio.iscoroutine(res) else res
                    if isinstance(hierarchy, str):
                        ui_hierarchy_xml = hierarchy
                        ui_elements = parse_hierarchy_xml_to_elements(hierarchy)
                    else:
                        ui_elements = hierarchy
                elif hasattr(self._ui_adb_client, "get_ui_elements"):
                    res = self._ui_adb_client.get_ui_elements()
                    ui_elements = await res if asyncio.iscoroutine(res) else res
            except Exception as e:
                logger.debug(f"UI hierarchy extraction failed: {e}")
        _hierarchy_ms = (time.perf_counter() - _t) * 1000.0

        if isinstance(ui_elements, dict):
            clean_ui_elements = [ui_elements]
        elif isinstance(ui_elements, list):
            clean_ui_elements = ui_elements
        else:
            clean_ui_elements = []

        w = self._width if isinstance(self._width, int) else 1080
        h = self._height if isinstance(self._height, int) else 2400
        clean_ui_elements = filter_ui_hierarchy(
            clean_ui_elements,
            screen_width=w,
            screen_height=h,
        )
        clean_b64 = (
            screenshot_base64
            if isinstance(screenshot_base64, str) and screenshot_base64
            else "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        clean_bytes = (
            screenshot_bytes
            if isinstance(screenshot_bytes, bytes) and screenshot_bytes
            else base64.b64decode(clean_b64)
        )

        # ── Đo hiệu năng: từng pha của lần quan sát này ─────────────────
        # Ba pha là ba span *lá*, đo riêng, nên bảng Performance có đúng thời gian
        # chụp ảnh / lấy hierarchy / đợi màn hình ổn định mà không cộng trùng.
        fingerprint = self._ui_fingerprint(clean_ui_elements, ui_hierarchy_xml)
        changed: bool | None = None
        if fingerprint is not None and self._last_ui_fingerprint is not None:
            changed = fingerprint != self._last_ui_fingerprint
        if fingerprint is not None:
            self._last_ui_fingerprint = fingerprint
        if _settle_ms is not None:
            record_event(
                PerfStage.SCREEN_SETTLING, "screen_settling", duration_ms=round(_settle_ms, 3)
            )
        if _screenshot_ms is not None:
            record_event(
                PerfStage.SCREENSHOT,
                "screenshot_capture",
                duration_ms=round(_screenshot_ms, 3),
                detail={
                    # Một số client trả ảnh + hierarchy trong cùng một lời gọi: khi đó
                    # thời gian lấy hierarchy nằm *trong* pha này, không phải pha riêng.
                    "hierarchy_included": _hierarchy_from_combined,
                },
            )
        if _hierarchy_ms is not None and _hierarchy_fetched:
            record_event(
                PerfStage.UI_HIERARCHY,
                "ui_hierarchy",
                duration_ms=round(_hierarchy_ms, 3),
                ui_fingerprint=fingerprint,
                ui_changed=changed,
                detail={
                    "elements": len(clean_ui_elements),
                    "has_hierarchy": bool(ui_hierarchy_xml),
                },
            )
        self._resolve_pending_action(fingerprint, changed)

        return ScreenData(
            screenshot_bytes=clean_bytes,
            screenshot_base64=clean_b64,
            ui_hierarchy_xml=ui_hierarchy_xml,
            ui_elements=clean_ui_elements,
            width=w,
            height=h,
            platform="android",
        )

    async def tap(
        self,
        x: int,
        y: int,
        duration_ms: int = 100,
        times: int = 1,
        delay_ms: int = 100,
    ) -> bool:
        _t = time.perf_counter()
        try:
            if duration_ms >= 500:
                cmd = f"input swipe {x} {y} {x} {y} {duration_ms}"
            else:
                if times <= 1:
                    cmd = f"input tap {x} {y}"
                else:
                    taps = [f"input tap {x} {y}"] * times
                    cmd = f" && sleep {delay_ms / 1000.0:.3f} && ".join(taps)

            logger.info(f"[ADB] {cmd}")
            await asyncio.to_thread(self.device.shell, cmd)
            self._emit_action("tap", _t, x=x, y=y, times=times, duration_ms=duration_ms)
            return True
        except Exception as e:
            logger.error(f"Tap failed at ({x}, {y}): {e}")
            self._emit_action("tap", _t, status=STATUS_ERROR, x=x, y=y, error=e)
            return False

    async def long_press(self, x: int, y: int, duration_ms: int = 1000) -> bool:
        return await self.tap(x=x, y=y, duration_ms=duration_ms)

    async def swipe(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration_ms: int = 800,
    ) -> bool:
        _t = time.perf_counter()
        try:
            cmd = f"input swipe {start_x} {start_y} {end_x} {end_y} {duration_ms}"
            logger.info(f"[ADB] {cmd}")
            await asyncio.to_thread(self.device.shell, cmd)
            self._emit_action(
                "swipe",
                _t,
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                duration_ms=duration_ms,
            )
            return True
        except Exception as e:
            logger.error(f"Swipe failed from ({start_x},{start_y}) to ({end_x},{end_y}): {e}")
            self._emit_action(
                "swipe",
                _t,
                status=STATUS_ERROR,
                start_x=start_x,
                start_y=start_y,
                end_x=end_x,
                end_y=end_y,
                error=e,
            )
            return False

    async def swipe_direction(
        self,
        direction: SwipeDirection | Literal["up", "down", "left", "right"],
        duration_ms: int = 800,
    ) -> bool:
        dir_str = str(direction).lower()
        mid_x = int(
            self._width * 0.6
        )  # Lock to 60% width to avoid edge gestures and alphabet fast-scroll sidebar
        mid_y = self._height // 2

        if dir_str == "up":
            # Drag bottom to top -> scrolls down (0.7 -> 0.3 leaves ~50-60% overlap, 800ms prevents fling)
            return await self.swipe(
                mid_x, int(self._height * 0.7), mid_x, int(self._height * 0.3), duration_ms
            )
        elif dir_str == "down":
            # Drag top to bottom -> scrolls up
            return await self.swipe(
                mid_x, int(self._height * 0.3), mid_x, int(self._height * 0.7), duration_ms
            )
        elif dir_str == "left":
            # Drag right to left -> scrolls right
            return await self.swipe(
                int(self._width * 0.75), mid_y, int(self._width * 0.25), mid_y, duration_ms
            )
        elif dir_str == "right":
            # Drag left to right -> scrolls left
            return await self.swipe(
                int(self._width * 0.25), mid_y, int(self._width * 0.75), mid_y, duration_ms
            )
        return False

    async def input_text(self, text: str, clear_existing: bool = True) -> bool:
        _t = time.perf_counter()
        try:
            if clear_existing:
                # Safe & robust clearing: Move to End -> Shift+Home selection -> Delete -> Fallback backspaces
                clear_cmd = (
                    "input keyevent 123 && "
                    "input keyevent --meta 1 122 && "
                    "input keyevent 67 && "
                    "input keyevent 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67 67"
                )
                await asyncio.to_thread(self.device.shell, clear_cmd)
            else:
                # Append mode: move cursor to the very end of existing text
                await asyncio.to_thread(
                    self.device.shell,
                    "input keyevent 123",  # KEYCODE_MOVE_END
                )

            # Normalize literal escaped newlines from LLM / tool call serialization
            norm_text = text.replace(r"\r\n", "\n").replace(r"\n", "\n").replace(r"\r", "\n")

            # 1. Tier 1: Try clipboard injection + KEYCODE_PASTE (Zero IME interference, preserves multiline, works for all charsets)
            if self._ui_adb_client:
                try:
                    set_clip_ok = False
                    if hasattr(self._ui_adb_client, "set_clipboard"):
                        set_clip_ok = self._ui_adb_client.set_clipboard(norm_text)
                    elif hasattr(self._ui_adb_client, "_device") and self._ui_adb_client._device:
                        self._ui_adb_client._device.set_clipboard(norm_text)
                        set_clip_ok = True

                    if set_clip_ok:
                        await asyncio.to_thread(self.device.shell, "input keyevent 279")
                        return True
                except Exception as e:
                    logger.debug(f"Clipboard paste fallback to ADB input: {e}")

            # 2. Tier 2: Check if ADBKeyboard is currently active
            try:
                default_ime = await asyncio.to_thread(
                    self.device.shell, "settings get secure default_input_method"
                )
                if "adbkeyboard" in str(default_ime).lower():
                    b64_text = base64.b64encode(norm_text.encode("utf-8")).decode("utf-8")
                    broadcast_cmd = f"am broadcast -a ADB_INPUT_B64 --es msg '{b64_text}'"
                    await asyncio.to_thread(self.device.shell, broadcast_cmd)
                    return True
            except Exception as e:
                # ADBKeyboard probe/broadcast failed; fall through to native input.
                logger.debug(f"ADBKeyboard IME path failed, falling back to ADB input: {e}")

            # 3. Tier 3: Universal Native ADB input text fallback
            lines = norm_text.split("\n")
            for i, line in enumerate(lines):
                if i > 0:
                    # Send Enter key between lines
                    await asyncio.to_thread(self.device.shell, "input keyevent 66")
                if line:
                    escaped = _escape_for_adb_text(line)
                    await asyncio.to_thread(self.device.shell, f"input text {escaped}")
            # Không ghi nội dung đã gõ (có thể là mật khẩu) — chỉ ghi độ dài.
            self._emit_action(
                "input_text", _t, length=len(text or ""), clear_existing=clear_existing
            )
            return True
        except Exception as e:
            logger.error(f"Input text failed for '{text}': {e}")
            self._emit_action(
                "input_text", _t, status=STATUS_ERROR, length=len(text or ""), error=e
            )
            return False

    async def press_key(self, key: KeyCode | str | int) -> bool:
        _t = time.perf_counter()
        try:
            keycode_val = key
            if isinstance(key, (KeyCode, str)):
                key_name = str(key).lower().replace("keycode.", "")
                keycode_val = ANDROID_KEYCODE_MAP.get(key_name, key)

            await asyncio.to_thread(self.device.shell, f"input keyevent {keycode_val}")
            self._emit_action("press_key", _t, key=str(keycode_val))
            return True
        except Exception as e:
            logger.error(f"Press key failed for '{key}': {e}")
            self._emit_action(
                "press_key", _t, status=STATUS_ERROR, key=str(key), error=e
            )
            return False

    async def launch_app(self, package_name: str) -> bool:
        _t = time.perf_counter()
        try:
            cmd = f"monkey -p {package_name} -c android.intent.category.LAUNCHER 1"
            await asyncio.to_thread(self.device.shell, cmd)
            self._emit_action("launch_app", _t, target=package_name)
            return True
        except Exception as e:
            logger.error(f"Launch app failed for '{package_name}': {e}")
            self._emit_action(
                "launch_app", _t, status=STATUS_ERROR, target=package_name, error=e
            )
            return False

    async def stop_app(self, package_name: str) -> bool:
        _t = time.perf_counter()
        try:
            await asyncio.to_thread(self.device.shell, f"am force-stop {package_name}")
            self._emit_action("stop_app", _t, target=package_name)
            return True
        except Exception as e:
            logger.error(f"Stop app failed for '{package_name}': {e}")
            self._emit_action(
                "stop_app", _t, status=STATUS_ERROR, target=package_name, error=e
            )
            return False

    async def get_current_package(self) -> str | None:
        try:
            # 1. Try modern current_app from adbutils
            if hasattr(self.device, "current_app"):
                try:
                    app_info = await asyncio.to_thread(self.device.current_app)
                    if app_info and getattr(app_info, "package", None):
                        return app_info.package
                except Exception as e:
                    # current_app is unsupported on some devices; use dumpsys below.
                    logger.debug(f"current_app query failed, falling back to dumpsys: {e}")

            # 2. Structured dumpsys extraction
            out = await asyncio.to_thread(
                self.device.shell, "dumpsys window displays | grep -E 'mCurrentFocus|mFocusedApp'"
            )
            for line in str(out).splitlines():
                if "/" in line:
                    for token in line.split():
                        if "/" in token and "." in token:
                            clean = token.split("/")[0].strip("{} ,")
                            if clean and not clean.startswith("Window") and "." in clean:
                                return clean
        except Exception as e:
            logger.debug(f"Error querying current package: {e}")
        return None

    async def execute_shell(self, command: str, timeout_seconds: float = 15.0) -> str:
        _t = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.device.shell, command),
                timeout=timeout_seconds,
            )
            self._emit_action(
                "shell", _t, target=command.split()[0] if command else "", timeout=timeout_seconds
            )
            return result
        except Exception as e:
            self._emit_action("shell", _t, status=STATUS_ERROR, error=e)
            return f"Error: {e}"

    async def start_video_recording(self, output_dir: Path | None = None) -> None:
        """Starts screen recording via scrcpy in background."""
        out_dir = output_dir or get_temp_dir("recordings")
        out_dir.mkdir(parents=True, exist_ok=True)
        self._recording_mkv_path = out_dir / "recording.mkv"
        self._recording_output_path = out_dir / "recording.mp4"
        logger.info(f"Starting scrcpy video recording to {self._recording_mkv_path}...")
        if not current_adb_endpoint().is_local_default and not os.environ.get("ADB"):
            # scrcpy shells out to its own adb, which would use the DEV machine's
            # local server. Point ADB at a wrapper (see README section 17.4).
            logger.warning(
                "Video recording with a remote ADB server needs the ADB environment "
                "variable to point at a wrapper script (`adb -H <host> -P <port>`); "
                "scrcpy may fail to find the device otherwise."
            )
        try:
            scrcpy_bin = find_scrcpy()
            cmd = build_scrcpy_record_command(
                scrcpy_bin,
                self.device_id,
                self._recording_mkv_path,
                lock_capture_orientation=False,
            )
            self._scrcpy_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Failed to start scrcpy subprocess: {e}")

    async def stop_video_recording(self) -> str | None:
        """Stops background video capture and returns local recording file path."""
        if hasattr(self, "_scrcpy_process") and self._scrcpy_process:
            try:
                self._scrcpy_process.terminate()
                await asyncio.wait_for(self._scrcpy_process.wait(), timeout=5.0)
            except (TimeoutError, ProcessLookupError, OSError) as e:
                # Already exited, or did not stop within the timeout; a lingering
                # scrcpy may keep the MKV file locked on Windows.
                logger.debug(f"scrcpy process did not terminate cleanly: {e}")
            self._scrcpy_process = None

        mkv = getattr(self, "_recording_mkv_path", None)
        mp4 = getattr(self, "_recording_output_path", None)

        if mkv and mkv.exists() and mp4:
            try:
                proc = await asyncio.create_subprocess_exec(
                    find_ffmpeg(),
                    "-y",
                    "-i",
                    str(mkv),
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    str(mp4),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.wait()
                if mp4.exists() and mp4.stat().st_size > 0:
                    try:
                        mkv.unlink()
                    except OSError:
                        # Best-effort cleanup of the intermediate MKV file.
                        pass
                    return str(mp4)
            except Exception as e:
                logger.warning(f"Failed to convert MKV to MP4 in AdbDriver: {e}")

        if mp4 and mp4.exists():
            return str(mp4)
        if mkv and mkv.exists():
            return str(mkv)
        return None
