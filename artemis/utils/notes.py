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

import difflib
import json
import os
from pathlib import Path
import re

from artemis.core.tool_failure import ToolFailure
from artemis.data_engine.engine import _CURRENT_DATA_ENGINE
from artemis.utils.plan_grammar import parse_plan
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

#: Dòng checkbox của kế hoạch (cùng cú pháp với ``artemis.utils.plan_grammar``): giữ cả nhóm
#: ``indent`` và ``status`` để gộp khối mà **không** làm mất trạng thái đã đạt.
_PLAN_LINE_RE = re.compile(r"^(?P<indent>\s*)-\s*\[(?P<status>[ x/!])\]\s*(?P<text>.*)$")

#: Mức "đã đi xa" của trạng thái — khi gộp hai bản trùng thì giữ mức xa nhất.
_PLAN_STATUS_RANK = {" ": 0, "!": 1, "/": 2, "x": 3}

#: Câu cảnh báo trả cho agent khi note ``task_plan`` bị gộp bản trùng lúc ghi.
PLAN_DEDUP_WARNING = (
    "Ghi chú: kế hoạch có {count} khối milestone trùng nhau (do lần sửa trước chèn lại cả kế"
    " hoạch) nên đã được gộp tại chỗ; trạng thái checkbox xa nhất và mọi dòng verify/assert được"
    " giữ. Hãy đọc lại note trước khi sửa tiếp."
)

# Centralized Description and Documentation Constants (Single Source of Truth)
READ_NOTE_DOCSTRING = (
    "[NOTE] Reads a previously saved note from persistent memory by its"
    " key.\nUse this as persistent cross-turn memory to retrieve key data, plan"
    " cross-turn subgoals, and coordinate step progress.\nYou can optionally"
    " specify a line range to read by providing start_line and end_line"
    " (1-indexed, inclusive).\nTIP: For large notes, prefer reading a small"
    " range of lines at a time (e.g., up to 200 lines). For chronological"
    " files, consider reading from back to front to see the latest entries"
    " first.\n\nUse the key 'task_plan' to read the active task plan"
    " (checklist)."
)
READ_NOTE_ARG_KEY_DESC = "The key of the note to read."

LIST_NOTES_DOCSTRING = (
    "[NOTE] Lists all note keys currently stored in persistent memory, along"
    " with the number of lines in each note.\nUse this to coordinate step"
    " progress and recall persistent cross-turn memories."
)

SAVE_NOTE_DOCSTRING = (
    "[NOTE] Saves a text note to persistent memory with the given key. Use this"
    " as persistent cross-turn memory to save key data, plan cross-turn"
    " subgoals, and coordinate step progress. If the key already exists, it"
    " will be overwritten."
)
SAVE_NOTE_ARG_KEY_DESC = "The unique key under which to save or overwrite the note."
SAVE_NOTE_ARG_CONTENT_DESC = "The text content to write to the note."

APPEND_NOTE_DOCSTRING = (
    "[NOTE] Appends text content to an existing note in persistent memory with"
    " the given key. Use this as persistent cross-turn memory to record"
    " chronological logs, save key data, or coordinate step progress. If the"
    " note does not exist, it will be created."
)
APPEND_NOTE_ARG_KEY_DESC = "The key of the note to append to."
APPEND_NOTE_ARG_CONTENT_DESC = "The text content to append to the note."

UPDATE_NOTE_DOCSTRING = (
    "[NOTE] Updates a note by replacing a specific target string with"
    " replacement content. Use this as persistent cross-turn memory to modify"
    " specific planning nodes or step details without overwriting the whole"
    " file.\n\nUse the key 'task_plan' to update specific sections of the task"
    " plan (checklist)."
)
UPDATE_NOTE_ARG_KEY_DESC = "The key of the note to update."
UPDATE_NOTE_ARG_TARGET_DESC = (
    "The exact target string currently in the note that you wish to replace."
)
UPDATE_NOTE_ARG_REPLACEMENT_DESC = (
    "The new replacement content to insert instead of the target content."
)


# Core Business Logic Filesystem Functions


def get_notes_dir(base_dir: str | Path) -> Path:
    """Gets the absolute path to the notes directory inside the session's base directory."""
    return Path(base_dir) / "notes"


def get_note_file_path(base_dir: str | Path, key: str) -> Path:
    """Gets the path of a note file by its key, mapping 'task_plan' to 'task_plan.md'.

    Case-insensitively strips any trailing '.md' from the key to prevent
    double-extension bugs.
    """
    notes_dir = get_notes_dir(base_dir)
    # Strip any trailing .md (e.g. 'findings.md' -> 'findings')
    clean_key = key[:-3] if key.lower().endswith(".md") else key
    if clean_key == "task_plan":
        return notes_dir / "task_plan.md"
    return notes_dir / f"{clean_key}.md"


def list_notes_keys(base_dir: str | Path) -> list[str]:
    """Lists all note keys currently stored in persistent memory."""
    notes_dir = get_notes_dir(base_dir)
    if not notes_dir.exists():
        return []
    return [f.stem for f in notes_dir.glob("*.md")]


def list_notes_info(base_dir: str | Path) -> dict[str, int]:
    """Lists all note keys currently stored in persistent memory, along with their line counts."""
    notes_dir = get_notes_dir(base_dir)
    if not notes_dir.exists():
        return {}
    info = {}
    for f in notes_dir.glob("*.md"):
        try:
            content = f.read_text(encoding="utf-8")
            info[f.stem] = len(content.splitlines())
        except Exception:
            info[f.stem] = -1
    return info


def read_note_content(
    base_dir: str | Path,
    key: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Reads the raw content of a note, optionally restricted to a line range (1-indexed, inclusive)."""
    file_path = get_note_file_path(base_dir, key)
    if not file_path.exists():
        raise FileNotFoundError(f"Note '{key}' not found.")
    content = file_path.read_text(encoding="utf-8")

    if start_line is not None or end_line is not None:
        lines = content.splitlines()
        start = (start_line - 1) if start_line else 0
        end = end_line if end_line else len(lines)
        return "\n".join(lines[start:end])

    return content


def save_note_content(base_dir: str | Path, key: str, content: str) -> None:
    """Saves or overwrites the raw content of a note."""
    notes_dir = get_notes_dir(base_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    file_path = get_note_file_path(base_dir, key)

    content_before = ""
    if key == "task_plan" and file_path.exists():
        try:
            content_before = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "Failed to read previous task_plan content from %s; subgoal"
                " rename tracking will be skipped for this save: %s",
                file_path,
                exc,
            )

    if key == "task_plan":
        content, collapsed = collapse_duplicate_plan_blocks(content)
        if collapsed:
            logger.warning(
                f"task_plan: gộp {collapsed} khối milestone trùng trước khi ghi (save_note)"
            )

    file_path.write_text(content, encoding="utf-8")

    if key == "task_plan":
        try:
            record_subgoal_hash_chain(base_dir, content_before, content)
        except Exception as exc:
            # Hash-chain bookkeeping must never break a note save, but a
            # missed record can later confuse plan ratchet validation.
            logger.warning("Failed to record subgoal hash chain for task_plan: %s", exc)

    try:
        if _CURRENT_DATA_ENGINE:
            _CURRENT_DATA_ENGINE._publish("note_saved", {"key": key, "content": content})
    except Exception as exc:
        # Telemetry side channel: publishing must never break a note save.
        logger.debug("Failed to publish note_saved event for key '%s': %s", key, exc)


def append_note_content(base_dir: str | Path, key: str, content: str) -> None:
    """Appends raw content to a note, inserting a newline if the file is not empty and lacks a trailing newline."""
    notes_dir = get_notes_dir(base_dir)
    notes_dir.mkdir(parents=True, exist_ok=True)
    file_path = get_note_file_path(base_dir, key)

    prefix = ""
    if file_path.exists() and file_path.stat().st_size > 0:
        # Open in binary mode for safe and portable backward seek
        with open(file_path, "rb") as f:
            try:
                f.seek(-1, os.SEEK_END)
                last_char_bytes = f.read(1)
                if last_char_bytes != b"\n":
                    prefix = "\n"
            except OSError:
                # Cannot inspect the last byte; worst case is a missing
                # separator newline before the appended content.
                pass

    with open(file_path, "a", encoding="utf-8") as f:
        f.write(prefix + content + "\n")


def clean_line(line: str) -> str:
    """Cleans a line by lowering case, stripping quotes, removing markdown bullet/checkbox markers, and all whitespace."""
    s = line.lower()
    s = s.replace('"', "").replace("'", "")
    # Strip markdown bullet patterns: optional start spaces, bullet character (-*+), optional spaces, optional checkbox [ xX/]
    s = re.sub(r"^\s*[-*+]\s*(\[[ xX/]\])?\s*", "", s)
    return "".join(s.split())


def find_relaxed_match(content: str, target: str) -> tuple[int, int] | None:
    """Finds a unique relaxed match (ignoring whitespaces, casing, quotes, and list bullets).

    Returns (start_char_idx, end_char_idx) if a unique match is found, otherwise
    None.
    """
    note_lines = content.splitlines(keepends=True)
    target_lines = target.splitlines()

    clean_note = [clean_line(line) for line in note_lines]
    clean_target = [clean_line(line) for line in target_lines if clean_line(line)]

    n = len(clean_note)
    m = len(clean_target)
    if m == 0 or n < m:
        return None

    matches = []
    for i in range(n - m + 1):
        window = clean_note[i : i + m]
        if window == clean_target:
            matches.append((i, i + m))

    if len(matches) == 1:
        start_line, end_line = matches[0]
        start_idx = sum(len(line) for line in note_lines[:start_line])
        end_idx = sum(len(line) for line in note_lines[:end_line])
        return start_idx, end_idx

    return None


def find_fuzzy_match(content: str, target: str, threshold: float = 0.90) -> tuple[int, int] | None:
    """Uses sliding window difflib similarity to find a unique close match above the threshold.

    Returns (start_char_idx, end_char_idx) if a unique close match is found,
    otherwise None.
    """
    note_lines = content.splitlines(keepends=True)
    target_lines = target.splitlines()

    n = len(note_lines)
    m = len(target_lines)
    if m == 0 or n < m:
        return None

    best_match = None
    best_ratio = 0.0
    ambiguous = False

    target_clean = "\n".join(clean_line(line) for line in target_lines)

    for i in range(n - m + 1):
        window_clean = "\n".join(clean_line(line) for line in note_lines[i : i + m])
        matcher = difflib.SequenceMatcher(None, window_clean, target_clean)
        ratio = matcher.ratio()

        if ratio >= threshold:
            if ratio > best_ratio + 0.05:
                best_ratio = ratio
                best_match = (i, i + m)
                ambiguous = False
            elif abs(ratio - best_ratio) <= 0.05:
                ambiguous = True

    if best_match and not ambiguous:
        start_line, end_line = best_match
        start_idx = sum(len(line) for line in note_lines[:start_line])
        end_idx = sum(len(line) for line in note_lines[:end_line])
        return start_idx, end_idx

    return None


def apply_replacement(content: str, start_idx: int, end_idx: int, replacement: str) -> str:
    """Applies a replacement, preserving the trailing newline of the original slice if present."""
    original_slice = content[start_idx:end_idx]
    actual_replacement = replacement
    if original_slice.endswith("\n") and not actual_replacement.endswith("\n"):
        actual_replacement += "\n"
    return content[:start_idx] + actual_replacement + content[end_idx:]


def update_note_content(
    base_dir: str | Path, key: str, target: str, replacement: str
) -> str | None:
    """Replaces target content in a note using hierarchical exact, relaxed, or fuzzy match.

    Returns a warning message if relaxed/fuzzy match was used, otherwise None.
    Raises ValueError if target is not found or if the match is ambiguous/not
    unique.
    """
    file_path = get_note_file_path(base_dir, key)
    if not file_path.exists():
        raise FileNotFoundError(f"Note '{key}' not found.")

    content = file_path.read_text(encoding="utf-8")
    new_content_to_write = None
    warning = None

    # Phase 1: Exact Match
    if target in content:
        count = content.count(target)
        if count == 1:
            new_content_to_write = content.replace(target, replacement)
            warning = None
        elif count > 1:
            raise ValueError(
                f"Target string '{target}' is not unique in note '{key}' (found"
                f" {count} occurrences). To prevent accidental changes, please"
                " provide a longer and more complete target string."
            )

    # Phase 2: Relaxed Match (Whitespace, casing, quotes, checkboxes ignored)
    if new_content_to_write is None:
        relaxed_range = find_relaxed_match(content, target)
        if relaxed_range:
            start_idx, end_idx = relaxed_range
            new_content_to_write = apply_replacement(content, start_idx, end_idx, replacement)
            warning = (
                "We did our best to apply changes despite some inaccuracies."
                " Double check if the edit applied is what you intended."
            )

    # Phase 3: Fuzzy Match (difflib similarity)
    if new_content_to_write is None:
        fuzzy_range = find_fuzzy_match(content, target, threshold=0.90)
        if fuzzy_range:
            start_idx, end_idx = fuzzy_range
            new_content_to_write = apply_replacement(content, start_idx, end_idx, replacement)
            warning = (
                "We did our best to apply changes despite some inaccuracies."
                " Double check if the edit applied is what you intended."
            )

    # Phase 4: Write and Return, or Not Found Error
    if new_content_to_write is not None:
        if key == "task_plan":
            new_content_to_write, collapsed = collapse_duplicate_plan_blocks(new_content_to_write)
            if collapsed:
                logger.warning(
                    f"task_plan: gộp {collapsed} khối milestone trùng trước khi ghi (update_note)"
                )
                dedup_note = PLAN_DEDUP_WARNING.format(count=collapsed)
                warning = f"{warning} {dedup_note}".strip() if warning else dedup_note
        file_path.write_text(new_content_to_write, encoding="utf-8")
        if key == "task_plan":
            try:
                record_subgoal_hash_chain(base_dir, content, new_content_to_write)
            except Exception as exc:
                # Hash-chain bookkeeping must never break a note edit, but a
                # missed record can later confuse plan ratchet validation.
                logger.warning("Failed to record subgoal hash chain for task_plan: %s", exc)
        return warning

    raise ValueError(
        f"Target string '{target}' not found in note '{key}'. Please re-read"
        " the note and ensure spelling, spacing, and indentation are close."
    )


# Shared Formatting Functions for Tool Outputs


def _plan_block_key(text: str) -> str:
    """Khoá so trùng của một dòng kế hoạch: bỏ trạng thái checkbox, gộp khoảng trắng, hạ chữ."""
    match = _PLAN_LINE_RE.match(text)
    body = match.group("text") if match else text
    return re.sub(r"\s+", " ", body).strip().lower()


def _plan_status_rank(status: str) -> int:
    """Mức "đã đi xa" của một trạng thái checkbox — dùng khi gộp hai bản trùng."""
    return _PLAN_STATUS_RANK.get(status, 0)


def _split_plan_blocks(content: str) -> tuple[list[str], list[list[str]], list[str]]:
    """Tách note kế hoạch thành ``(đầu, các khối cấp cao nhất, đuôi)``.

    Một khối = dòng checkbox ở **mức thụt nhỏ nhất** + mọi dòng theo sau nó (dòng con, ``verify``/
    ``assert``, dòng trống) cho tới khi gặp khối kế tiếp. Giữ nguyên văn từng dòng để không mất
    định dạng của agent.
    """
    lines = content.splitlines()
    indent = None
    for line in lines:
        match = _PLAN_LINE_RE.match(line)
        if match:
            depth = len(match.group("indent"))
            indent = depth if indent is None else min(indent, depth)
    if indent is None:
        return lines, [], []

    head: list[str] = []
    blocks: list[list[str]] = []
    tail: list[str] = []
    current: list[str] | None = None
    for line in lines:
        match = _PLAN_LINE_RE.match(line)
        is_top = bool(match) and len(match.group("indent")) == indent
        if is_top:
            if current is not None:
                blocks.append(current)
            current = [line]
        elif current is not None:
            current.append(line)
        elif blocks:
            tail.append(line)
        else:
            head.append(line)
    if current is not None:
        blocks.append(current)
    return head, blocks, tail


def _merge_plan_blocks(blocks: list[list[str]]) -> tuple[list[list[str]], int]:
    """Gộp các khối trùng nhau, giữ trạng thái xa nhất và hợp các dòng con/kiểm tra."""
    merged: list[list[str]] = []
    index_of: dict[str, int] = {}
    removed = 0
    for block in blocks:
        key = _plan_block_key(block[0])
        if key and key in index_of:
            target = merged[index_of[key]]
            target[0] = _merge_plan_line(target[0], block[0])
            seen_children = {_plan_block_key(line) for line in target[1:] if line.strip()}
            for line in block[1:]:
                child_key = _plan_block_key(line)
                if not child_key:
                    continue
                if child_key in seen_children:
                    # Dòng con đã có: nếu là checkbox thì nâng trạng thái tại chỗ (giữ tiến độ).
                    if _PLAN_LINE_RE.match(line):
                        for idx, existing in enumerate(target[1:], start=1):
                            if _plan_block_key(existing) == child_key:
                                target[idx] = _merge_plan_line(existing, line)
                                break
                    continue
                target.append(line)
                seen_children.add(child_key)
            removed += 1
            continue
        if key:
            index_of[key] = len(merged)
        merged.append(list(block))
    return merged, removed


def _merge_plan_line(first: str, second: str) -> str:
    """Giữ dòng ``first`` nhưng nâng trạng thái checkbox lên mức xa nhất của hai dòng."""
    a = _PLAN_LINE_RE.match(first)
    b = _PLAN_LINE_RE.match(second)
    if not a or not b:
        return first
    if _plan_status_rank(b.group("status")) <= _plan_status_rank(a.group("status")):
        return first
    return first[: a.start("status")] + b.group("status") + first[a.end("status") :]


def collapse_duplicate_plan_blocks(content: str) -> tuple[str, int]:
    """Gộp các khối milestone **trùng nhau** trong một note ``task_plan``; trả ``(text, số gộp)``.

    **Vì sao cần** (đo trên session ``d110b1ef``, task ``1371db04``): agent gọi ``update_note`` với
    ``target`` chỉ khớp **một phần** kế hoạch nhưng ``replacement`` là **cả kế hoạch**, nên mỗi lần
    sửa lại chèn thêm một bản của các milestone còn lại ⇒ ``task_plan.md`` có cùng 4 milestone lặp
    **4 lần**. Hậu quả đo được: gate ledger bounce **10 lần**, checker khai **6 tiêu chí với 4 bản
    trùng**, prompt phình và lượt LLM vô ích.

    Sửa **tại nguồn ghi** (không phải dedup lúc đọc): gộp theo vị trí khối cấp cao nhất, giữ

    * vị trí xuất hiện **đầu tiên** (thứ tự kế hoạch không đổi);
    * trạng thái checkbox **xa nhất** (``x`` > ``/`` > ``!`` > `` ``) ⇒ không mất tiến độ;
    * hợp các dòng con và các dòng ``verify``/``assert`` ⇒ **không mất tiêu chí kiểm**.

    Chỉ gộp khi hai khối giống nhau **sau khi bỏ trạng thái**; khối khác nội dung giữ nguyên.
    """
    head, blocks, tail = _split_plan_blocks(content)
    if len(blocks) < 2:
        return content, 0
    merged, removed = _merge_plan_blocks(blocks)
    if not removed:
        return content, 0
    lines = [*head]
    for block in merged:
        lines.extend(block)
    lines.extend(tail)
    text = "\n".join(lines)
    if content.endswith("\n") and not text.endswith("\n"):
        text += "\n"
    return text, removed


def format_read_note_success(
    key: str,
    content: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """The read result: a one-line header naming the note and its extent, then the content."""
    if start_line is not None or end_line is not None:
        extent = f"lines {start_line or 1} to {end_line or 'end'}"
    else:
        extent = f"{len(content.splitlines())} lines"
    return f"Note '{key}' ({extent}):\n{content}"


def format_read_note_failure(key: str, error: str) -> str:
    """Formats the failure message for reading a note."""
    if "not found" in error.lower():
        return ToolFailure(f"Note '{key}' not found. Use list_notes to see the saved keys.")
    return ToolFailure(f"Failed to read note '{key}': {error}")


def format_list_notes_success(notes_info: dict[str, int]) -> str:
    """Formats the success message for listing notes with line counts."""
    if not notes_info:
        return "No notes saved yet."
    lines = [f"- {key} ({count} lines)" for key, count in notes_info.items()]
    return "Here are all the notes:\n" + "\n".join(lines)


def format_list_notes_failure(error: str) -> str:
    """Formats the failure message for listing notes."""
    return ToolFailure(f"Failed to list notes: {error}")


def record_subgoal_hash_chain(
    base_dir: str | Path, content_before: str, content_after: str
) -> None:
    """Detects if the active subgoal was renamed/changed, and records it in the subgoal_hash_chain.json."""
    if not content_before or not content_after:
        return

    # Helper to get the active subgoal hash from task_plan string.
    # Parsing goes through the plan-grammar single source; the hash is
    # ``PlanItem.key`` (= ``plan_grammar.subgoal_hash``, md5 of the stripped
    # subgoal text) -- byte-identical to the historical inline md5, so anchors
    # already persisted in subgoal_hash_chain.json stay valid.
    def get_active_hash(task_plan: str) -> str | None:
        try:
            snapshot = parse_plan(task_plan)
            # Last active [/] item wins (its own key, not the parent's) --
            # matches the historical reversed-scan behavior of this helper.
            item = snapshot.last_active()
            if item is None:
                # Fallback to the first pending [ ] item if no [/].
                item = next((i for i in snapshot.items if i.is_pending), None)
            return item.key if item is not None else None
        except Exception as exc:
            # A failure here is unexpected and silently disables subgoal
            # rename tracking.
            logger.warning("Failed to compute active subgoal hash: %s", exc)
        return None

    old_active = get_active_hash(content_before)
    new_active = get_active_hash(content_after)

    if old_active and new_active and old_active != new_active:
        notes_dir = Path(base_dir) / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        chain_path = notes_dir / "subgoal_hash_chain.json"

        chain = {}
        if chain_path.exists():
            try:
                chain = json.loads(chain_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(
                    "Failed to load subgoal hash chain %s (starting a fresh"
                    " chain; previous rename history is lost): %s",
                    chain_path,
                    exc,
                )

        # Record mapping: old -> new
        chain[old_active] = new_active

        try:
            chain_path.write_text(
                json.dumps(chain, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning(
                "Failed to persist subgoal hash chain to %s (rename %s -> %s not recorded): %s",
                chain_path,
                old_active,
                new_active,
                exc,
            )
