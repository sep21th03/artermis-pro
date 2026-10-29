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

"""Tải "các bước" của một lần chạy (kèm ảnh) — và luật quan trọng nhất của nó.

Ba tính chất được khoá:

1. **Chỉ tạo khi tester bấm.** Không file nào được ghi ra đĩa: gói ZIP dựng trong bộ nhớ rồi trả về
   dạng attachment. Nếu không khoá điều này thì mỗi lần xem lại một task là một thư mục mới trên ổ
   đĩa — đúng thứ tester không yêu cầu.
2. **Có ảnh thật.** Ảnh Trước/Sau của từng bước phải nằm trong gói, và tên file trong gói phải khớp
   khoá ``images`` trong ``steps.json`` (nếu không thì gói vô dụng khi mở ra).
3. **Thiếu ảnh không làm hỏng gói.** Ảnh không lấy được thì ghi vào ``missing_images``; ``steps.json``
   vẫn đủ để đọc lại diễn biến.
"""

from __future__ import annotations

import importlib
import io
import json
import zipfile
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

console_server = importlib.import_module("apps.test_console.server")

TASK_ID = "abcd1234"
SESSION_ID = "99999999-8888-7777-6666-555555555555"


def _task(**overrides):
    payload = {
        "id": TASK_ID,
        "goal": "đăng nhập tài khoản 0999 rồi chuyển nhà",
        "profile": "pro",
        "package": "com.iot.hunonic",
        "serial": "R7AY30DH7GR",
        "test_name": "task_2910",
        "goal_sent": "đăng nhập tài khoản 0999",
        "max_seconds": 1200,
        "session_id": SESSION_ID,
        "status": "failed",
        "cost_vnd": 480.0,
        "requests": 33,
    }
    payload.update(overrides)
    return console_server.Task(**payload)


class FakeRunner:
    def __init__(self, task):
        self.task = task

    def get_task(self, task_id):
        return self.task if task_id == self.task.id else None


@pytest.fixture
def client(monkeypatch, tmp_path):
    task = _task()
    monkeypatch.setattr(console_server, "runner", FakeRunner(task))
    monkeypatch.setattr(console_server, "LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(console_server, "IMAGES_DIR", tmp_path / "images")
    (tmp_path / "images").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(console_server, "_activity", lambda task: [{"kind": "tool", "label": "tap"}])
    monkeypatch.setattr(console_server, "_task_summary", lambda task: {"verdict": "KHÔNG ĐẠT"})
    return TestClient(console_server.app), tmp_path


JPEG_PRE = b"\xff\xd8\xff" + b"PRE-ONE"
JPEG_POST = b"\xff\xd8\xff" + b"POST-ONE"


def _steps_with_images(tmp_path):
    """Hai ảnh JPEG thật (đủ magic bytes) + một bước trỏ tới ảnh không tồn tại."""
    images = tmp_path / "images"
    (images / "pre1.jpg").write_bytes(JPEG_PRE)
    (images / "post1.jpg").write_bytes(JPEG_POST)
    return [
        {
            "number": 1,
            "action": "click",
            "label": "Bấm Đăng xuất",
            "before": f"/api/tasks/{TASK_ID}/image/pre1",
            "after": f"/api/tasks/{TASK_ID}/image/post1",
            "result": "success",
            "detail": "",
            "tokens": 1200,
            "thinking": "vì công đoạn là chuyển nhà",
        },
        {
            "number": 2,
            "action": "input_text",
            "label": "Gõ mật khẩu",
            "before": f"/api/tasks/{TASK_ID}/image/khong_ton_tai",
            "after": "",
            "result": "success",
            "detail": "",
            "tokens": 900,
            "thinking": "",
        },
    ]


# ── gói tải có gì ───────────────────────────────────────────────────────
def test_export_returns_a_zip_with_steps_json_and_images(client, monkeypatch):
    http, tmp_path = client
    monkeypatch.setattr(console_server, "_fetch_steps", lambda task: _steps_with_images(tmp_path))
    (tmp_path / "logs" / f"{TASK_ID}_final.png").write_bytes(b"FINAL")

    response = http.get(f"/api/tasks/{TASK_ID}/steps-export")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-disposition"].endswith('.zip"')

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert {"steps.json", "README.txt"} <= names
        # Ảnh Trước/Sau của bước 1 nằm trong gói, tên file khớp `images` trong steps.json.
        assert "images/step001_before.jpg" in names
        assert "images/step001_after.jpg" in names
        assert archive.read("images/step001_before.jpg") == JPEG_PRE
        assert "images/final.png" in names

        payload = json.loads(archive.read("steps.json"))
        step = payload["steps"][0]
        assert step["images"] == {
            "before": "images/step001_before.jpg",
            "after": "images/step001_after.jpg",
        }
        assert payload["task"]["id"] == TASK_ID
        assert payload["task"]["max_seconds"] == 1200
        assert payload["summary"]["verdict"] == "KHÔNG ĐẠT"
        assert payload["activity"][0]["label"] == "tap"
        assert payload["final_image"] == "images/final.png"


def test_a_missing_image_is_reported_not_fatal(client, monkeypatch):
    """Thiếu ảnh thì ghi vào ``missing_images``; phần chữ vẫn đủ để đọc lại diễn biến."""
    http, tmp_path = client
    monkeypatch.setattr(console_server, "_fetch_steps", lambda task: _steps_with_images(tmp_path))

    response = http.get(f"/api/tasks/{TASK_ID}/steps-export")

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        payload = json.loads(archive.read("steps.json"))
        assert payload["missing_images"] == ["khong_ton_tai"]
        assert len(payload["steps"]) == 2
        assert "images/step002_before.jpg" not in archive.namelist()


def test_the_export_writes_nothing_to_disk(client, monkeypatch, tmp_path):
    """Luật trung tâm: file chỉ được tạo khi bấm, và **không** bản sao nào nằm lại trên server."""
    http, tmp_path = client
    steps = _steps_with_images(tmp_path)  # tạo ảnh nguồn một lần, rồi mới chụp danh sách
    monkeypatch.setattr(console_server, "_fetch_steps", lambda task: steps)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))

    http.get(f"/api/tasks/{TASK_ID}/steps-export")
    http.get(f"/api/tasks/{TASK_ID}/steps-export")  # bấm hai lần vẫn không sinh file

    after = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    assert before == after


def test_an_unknown_task_is_a_404(client):
    http, _ = client
    assert http.get("/api/tasks/khong-co/steps-export").status_code == 404


def test_a_task_without_steps_still_exports(client, monkeypatch):
    """Task không có thao tác nào vẫn phải tải được: steps.json rỗng + README, không lỗi."""
    http, _ = client
    monkeypatch.setattr(console_server, "_fetch_steps", lambda task: [])

    response = http.get(f"/api/tasks/{TASK_ID}/steps-export")

    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        payload = json.loads(archive.read("steps.json"))
        assert payload["steps"] == []
        assert "Số bước: 0" in archive.read("README.txt").decode("utf-8")
