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

"""Đăng nhập quản trị Knowledge base: ai được **thêm/sửa/xoá** workflow.

**Vì sao cần.** Router ``/api/kb/*`` cho phép sửa và xoá workflow nhưng trước đây **không có xác
thực riêng** — nó chỉ được mount trên máy dev và dựa vào việc không mở ra Internet. Khi quản trị KB
từ máy khác (hoặc qua VPS) thì phải có đăng nhập thật: một tài khoản admin có quyền ghi, còn **đọc**
vẫn dùng khoá truy cập hiện có (để agent ở máy tester gọi ``/api/kb/retrieve`` mà không cần tài khoản).

Cơ chế (không thêm dependency nào, chỉ thư viện chuẩn):

* Mật khẩu băm bằng ``hashlib.scrypt`` (có salt), so sánh bằng ``hmac.compare_digest``.
* Token = payload JSON (base64url) + chữ ký ``HMAC-SHA256``; hết hạn theo ``expires_at``.
* Tài khoản admin lấy từ env: ``KB_ADMIN_EMAIL`` / ``KB_ADMIN_PASSWORD`` (mặc định
  ``admin@admin`` / ``admin`` — **phải đổi** khi console lộ ra ngoài).
* Secret ký token: ``KB_AUTH_SECRET``; nếu không đặt thì suy ra từ email+mật khẩu admin (token sống
  qua restart) và ghi cảnh báo một lần.
* ``KB_AUTH_DISABLED=1`` tắt kiểm tra (chỉ dùng cho máy dev một mình, không bao giờ cho máy chủ).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

ADMIN_EMAIL_ENV = "KB_ADMIN_EMAIL"
ADMIN_PASSWORD_ENV = "KB_ADMIN_PASSWORD"
SECRET_ENV = "KB_AUTH_SECRET"
TTL_ENV = "KB_AUTH_TTL_SECONDS"
DISABLED_ENV = "KB_AUTH_DISABLED"

DEFAULT_ADMIN_EMAIL = "admin@admin"
DEFAULT_ADMIN_PASSWORD = "admin"
DEFAULT_TTL_SECONDS = 12 * 3600

#: scrypt: N=2**14, r=8, p=1 — mức mặc định của Python, đủ cho một mật khẩu admin.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_LEN = 32

_warned_default_password = False


def auth_disabled() -> bool:
    """``True`` khi đặt ``KB_AUTH_DISABLED=1`` — chỉ dành cho máy dev một mình."""
    return (os.getenv(DISABLED_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def admin_email() -> str:
    return (os.getenv(ADMIN_EMAIL_ENV) or DEFAULT_ADMIN_EMAIL).strip()


def admin_password() -> str:
    return os.getenv(ADMIN_PASSWORD_ENV) or DEFAULT_ADMIN_PASSWORD


def token_ttl_seconds() -> int:
    raw = (os.getenv(TTL_ENV) or "").strip()
    try:
        ttl = int(raw) if raw else DEFAULT_TTL_SECONDS
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return ttl if ttl > 0 else DEFAULT_TTL_SECONDS


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Băm mật khẩu: ``scrypt$<salt_hex>$<hash_hex>`` (mỗi lần gọi có salt mới)."""
    salt = salt or secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """So khớp mật khẩu với chuỗi đã băm, so sánh hằng thời gian."""
    try:
        scheme, salt_hex, digest_hex = (stored or "").split("$", 2)
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=len(expected) or _KEY_LEN,
    )
    return hmac.compare_digest(actual, expected)


def verify_credentials(email: str, password: str) -> bool:
    """Đúng tài khoản admin? Email không phân biệt hoa/thường; mật khẩu so hằng thời gian."""
    email_ok = hmac.compare_digest((email or "").strip().casefold(), admin_email().casefold())
    password_ok = hmac.compare_digest(password or "", admin_password())
    if email_ok and password_ok:
        _warn_about_default_password()
    return bool(email_ok and password_ok)


def _warn_about_default_password() -> None:
    global _warned_default_password
    if _warned_default_password:
        return
    _warned_default_password = True
    if admin_password() == DEFAULT_ADMIN_PASSWORD:
        # ``ArtemisLogger.warning`` chỉ nhận **một** tham số ⇒ phải dùng f-string.
        logger.warning(
            "KB admin đang dùng mật khẩu mặc định"
            f" ({DEFAULT_ADMIN_EMAIL} / {DEFAULT_ADMIN_PASSWORD}). Đổi bằng KB_ADMIN_EMAIL và"
            " KB_ADMIN_PASSWORD trước khi mở console ra ngoài Internet."
        )


def _secret() -> bytes:
    raw = (os.getenv(SECRET_ENV) or "").strip()
    if raw:
        return raw.encode("utf-8")
    # Không có secret riêng ⇒ suy ra từ tài khoản admin (ổn định qua restart), an toàn hơn một secret
    # ngẫu nhiên theo tiến trình ở chỗ token không chết mỗi lần restart console.
    return hashlib.sha256(f"{admin_email()}:{admin_password()}".encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


@dataclass(frozen=True)
class TokenInfo:
    token: str
    email: str
    expires_at: float


def issue_token(email: str | None = None, *, ttl_seconds: int | None = None) -> TokenInfo:
    """Phát token admin: payload (email + hạn) ký HMAC-SHA256."""
    who = (email or admin_email()).strip() or admin_email()
    expires_at = time.time() + (ttl_seconds if ttl_seconds is not None else token_ttl_seconds())
    payload = json.dumps({"sub": who, "exp": expires_at}, separators=(",", ":"), sort_keys=True)
    body = _b64(payload.encode("utf-8"))
    signature = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    return TokenInfo(token=f"{body}.{_b64(signature)}", email=who, expires_at=expires_at)


def verify_token(token: str | None) -> str | None:
    """Email của chủ token nếu token hợp lệ và còn hạn; ``None`` cho mọi trường hợp khác."""
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    expected = hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest()
    try:
        given = _unb64(signature)
    except Exception:  # noqa: BLE001 - token rác chỉ là token không hợp lệ
        return None
    if not hmac.compare_digest(expected, given):
        return None
    try:
        payload = json.loads(_unb64(body).decode("utf-8"))
        expires_at = float(payload.get("exp") or 0)
    except Exception:  # noqa: BLE001
        return None
    if expires_at <= time.time():
        return None
    subject = str(payload.get("sub") or "").strip()
    return subject or None


def bearer_token(authorization_header: str | None) -> str:
    """Lấy token từ ``Authorization: Bearer <token>`` (rỗng khi không có)."""
    header = (authorization_header or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""

