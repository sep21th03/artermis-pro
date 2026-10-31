"""Tests cho importer workflow KB (``scripts/kb_import_workflows.py``).

Vì sao test ở đây chứ không chỉ chạy script: hai luật an toàn của importer — **GET trước PUT** và
**không bao giờ ghi đè bản đã có** — cùng cách đếm kết quả của ``--dry-run`` là thứ phải không thể
sai do vô ý. Bản đầu của script in ``Tạo mới 0 · bỏ qua 60`` trong khi log có 3 dòng ``SẼ TẠO``:
đúng lúc cần biết còn bao nhiêu việc thì tổng kết lại nói 0. Các test dưới đây khoá lại, không cần
KB thật (mọi lời gọi API đều được thay bằng hàm giả).
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "kb_import_workflows.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("kb_import_workflows", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wf = _load_module()


def _payload(workflow_id: str) -> dict:
    return {"id": workflow_id, "app_id": "hunonic", "status": "draft", "name": workflow_id}


def _write(tmp_path: Path, payloads: list) -> Path:
    path = tmp_path / "workflows.json"
    path.write_text(json.dumps(payloads, ensure_ascii=False), encoding="utf-8")
    return path


def _run(monkeypatch: pytest.MonkeyPatch, source: Path, *extra: str) -> int:
    monkeypatch.setattr(sys, "argv", ["kb_import_workflows.py", str(source), *extra])
    return wf.main()


def test_dry_run_dem_dung_so_se_tao(tmp_path, monkeypatch, capsys):
    """``--dry-run`` phải đếm ``would-create`` — tổng kết nói 0 khi log có dòng SẼ TẠO là lỗi."""
    monkeypatch.setattr(
        wf,
        "api_get",
        lambda base, path: (
            (200, {"status": "draft"}) if path.endswith("device.view_all") else (404, {})
        ),
    )
    monkeypatch.setattr(wf, "api_put", lambda *a, **k: pytest.fail("dry-run không được PUT"))

    source = _write(tmp_path, [_payload("device.view_all"), _payload("a.moi"), _payload("b.moi")])
    code = _run(monkeypatch, source, "--dry-run")

    out = capsys.readouterr().out
    assert code == 0
    assert "sẽ tạo 2" in out
    assert "bỏ qua 1" in out
    assert "lỗi 0" in out


def test_khong_ghi_de_ban_da_co(tmp_path, monkeypatch, capsys):
    """ID đã tồn tại (kể cả ``verified``) thì bỏ qua, tuyệt đối không PUT."""
    monkeypatch.setattr(wf, "api_get", lambda base, path: (200, {"status": "verified"}))
    monkeypatch.setattr(wf, "api_put", lambda *a, **k: pytest.fail("không được ghi đè bản đã có"))

    code = _run(monkeypatch, _write(tmp_path, [_payload("auth.logout")]))

    out = capsys.readouterr().out
    assert code == 0
    assert "BỎ QUA (đã có: verified)" in out


def test_payload_thieu_id_bi_coi_la_loi(tmp_path, monkeypatch, capsys):
    """Thiếu ``id`` là lỗi dữ liệu: đếm vào ``lỗi`` và thoát mã 1, không im lặng bỏ qua."""
    monkeypatch.setattr(wf, "api_get", lambda base, path: (404, {}))
    monkeypatch.setattr(wf, "api_put", lambda *a, **k: pytest.fail("thiếu id thì không PUT"))

    code = _run(monkeypatch, _write(tmp_path, [{"app_id": "hunonic"}]), "--dry-run")

    assert code == 1
    assert "lỗi 1" in capsys.readouterr().out


def test_dry_run_van_ghi_bao_cao(tmp_path, monkeypatch):
    """``--dry-run`` không ghi KB nhưng vẫn phải để lại báo cáo để đối chiếu."""
    monkeypatch.setattr(wf, "api_get", lambda base, path: (404, {}))
    monkeypatch.setattr(wf, "api_put", lambda *a, **k: pytest.fail("dry-run không được PUT"))

    report = tmp_path / "report.json"
    code = _run(
        monkeypatch, _write(tmp_path, [_payload("a.moi")]), "--dry-run", "--report", str(report)
    )

    assert code == 0
    rows = json.loads(report.read_text(encoding="utf-8"))
    assert [row["result"] for row in rows] == ["would-create"]


def test_tao_moi_va_index_embedding_thanh_cong(tmp_path, monkeypatch, capsys):
    """Tạo mới xong và response xác nhận ``embedded`` ⇒ thoát mã 0."""
    monkeypatch.setattr(wf, "api_get", lambda base, path: (404, {}))
    monkeypatch.setattr(
        wf,
        "api_put",
        lambda base, path, payload: (200, {"embedded": True, "status": "draft", "revision_no": 1}),
    )

    code = _run(monkeypatch, _write(tmp_path, [_payload("a.moi")]))

    assert code == 0
    assert "Tạo mới 1" in capsys.readouterr().out


def test_tao_duoc_nhung_chua_index_embedding_thi_bao_loi(tmp_path, monkeypatch):
    """Ghi được mà chưa index embedding là lỗi (KB sẽ không tra được) ⇒ thoát mã 1."""
    monkeypatch.setattr(wf, "api_get", lambda base, path: (404, {}))
    monkeypatch.setattr(wf, "api_put", lambda base, path, payload: (200, {"embedded": False}))

    assert _run(monkeypatch, _write(tmp_path, [_payload("a.moi")])) == 1
