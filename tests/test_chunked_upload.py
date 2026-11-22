"""Chunked PUT /api/uploads/{id}?offset=N[&size=TOTAL] (spec 2026-11-23-parts-mode §6).

Cloudflare rejects request bodies over 100 MB, so the dashboard sends a file in
50 MB chunks. Each chunk must start where the last one ended (offset 0 starts
over); the chunk that carries `size` closes the upload and runs the duration
probe. Without `offset` the endpoint is the legacy whole-body PUT.
"""
import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest

app_module = pytest.importorskip("app")


@pytest.fixture()
def dirs(tmp_path, monkeypatch):
    out_root = tmp_path / "output"
    up_root = tmp_path / "uploads"
    out_root.mkdir()
    up_root.mkdir()
    monkeypatch.setattr(app_module, "OUTPUT_DIR", str(out_root))
    monkeypatch.setattr(app_module, "UPLOAD_DIR", str(up_root))
    monkeypatch.setattr(app_module, "jobs", {})
    monkeypatch.setattr(app_module, "job_queue", asyncio.PriorityQueue())
    monkeypatch.setattr(app_module, "_media_duration_seconds", lambda p: 120.0)

    async def _reserve(request, url, input_path, job_id, max_minutes=None, **_kw):
        return None, 2, None, None, None

    async def _no_gate(url, force_low):
        return None

    monkeypatch.setattr(app_module, "reserve_process_minutes", _reserve)
    monkeypatch.setattr(app_module, "_quality_gate", _no_gate)
    return SimpleNamespace(out=out_root, up=up_root)


def _call(method, path, **kw):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     headers={"X-Gemini-Key": "test-key"}) as c:
            return await c.request(method, path, **kw)
    return asyncio.run(_do())


def _slot():
    resp = _call("POST", "/api/uploads", json={"filename": "talk.mp4"})
    assert resp.status_code == 200, resp.text
    return resp.json()["upload_id"]


def _put(upload_id, body, **params):
    return _call("PUT", f"/api/uploads/{upload_id}", content=body, params=params)


def _path(upload_id):
    return app_module.pending_uploads[upload_id]["path"]


def test_three_chunks_complete(dirs):
    uid = _slot()
    r = _put(uid, b"a" * 10, offset=0)
    assert r.status_code == 200, r.text
    assert r.json() == {"upload_id": uid, "received": 10, "complete": False}
    assert app_module.pending_uploads[uid]["bytes"] == 10
    r = _put(uid, b"b" * 10, offset=10)
    assert r.status_code == 200 and r.json()["received"] == 20 and r.json()["complete"] is False
    assert app_module.pending_uploads[uid]["complete"] is False
    r = _put(uid, b"c" * 10, offset=20, size=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["received"] == 30 and body["complete"] is True
    assert body["duration_seconds"] == 120.0
    slot = app_module.pending_uploads[uid]
    assert slot["bytes"] == 30 and slot["complete"] is True
    with open(slot["path"], "rb") as f:
        assert f.read() == b"a" * 10 + b"b" * 10 + b"c" * 10


def test_wrong_offset_409(dirs):
    uid = _slot()
    assert _put(uid, b"a" * 10, offset=0).status_code == 200
    r = _put(uid, b"b" * 10, offset=5)
    assert r.status_code == 409
    assert r.json() == {"detail": "expected offset 10"}
    assert app_module.pending_uploads[uid]["bytes"] == 10
    assert os.path.getsize(_path(uid)) == 10


def test_offset_without_file_on_disk_expects_zero(dirs):
    uid = _slot()
    r = _put(uid, b"a" * 10, offset=10)
    assert r.status_code == 409 and r.json() == {"detail": "expected offset 0"}


def test_offset_zero_restarts(dirs):
    uid = _slot()
    assert _put(uid, b"a" * 10, offset=0).status_code == 200
    r = _put(uid, b"z" * 4, offset=0)
    assert r.status_code == 200 and r.json()["received"] == 4
    with open(_path(uid), "rb") as f:
        assert f.read() == b"z" * 4


def test_offset_zero_restarts_a_completed_upload(dirs):
    uid = _slot()
    assert _put(uid, b"a" * 10, offset=0, size=10).json()["complete"] is True
    # A chunk past the start of a finished upload is refused...
    for off in (5, 10):
        r = _put(uid, b"b" * 10, offset=off)
        assert r.status_code == 409 and r.json() == {"detail": "expected offset 10"}
    assert app_module.pending_uploads[uid]["complete"] is True
    assert os.path.getsize(_path(uid)) == 10
    # ...offset 0 starts over and the slot is incomplete until the last chunk.
    r = _put(uid, b"z" * 4, offset=0)
    assert r.status_code == 200 and r.json() == {"upload_id": uid, "received": 4, "complete": False}
    assert app_module.pending_uploads[uid]["complete"] is False


def test_no_offset_is_legacy_put(dirs):
    uid = _slot()
    r = _put(uid, b"x" * 5000)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["upload_id"] == uid and body["bytes"] == 5000
    assert body["duration_seconds"] == 120.0 and "hint" in body
    assert app_module.pending_uploads[uid]["complete"] is True
    # A second whole-body PUT replaces the first.
    r = _put(uid, b"y" * 7)
    assert r.status_code == 200 and r.json()["bytes"] == 7
    assert os.path.getsize(_path(uid)) == 7


def test_cumulative_cap_413(dirs, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_FILE_SIZE_MB", 1)
    limit = 1024 * 1024
    uid = _slot()
    assert _put(uid, b"a" * (limit - 10), offset=0).status_code == 200
    r = _put(uid, b"b" * 20, offset=limit - 10)
    assert r.status_code == 413
    assert not os.path.exists(_path(uid))
    assert app_module.pending_uploads[uid]["bytes"] == 0
    assert app_module.pending_uploads[uid]["complete"] is False


def test_empty_chunk_400(dirs):
    uid = _slot()
    assert _put(uid, b"a" * 10, offset=0).status_code == 200
    r = _put(uid, b"", offset=10)
    assert r.status_code == 400
    assert app_module.pending_uploads[uid]["bytes"] == 10
    assert os.path.getsize(_path(uid)) == 10


@pytest.mark.parametrize("params", [{"offset": "-1"}, {"offset": "abc"}, {"offset": ""},
                                    {"offset": "0", "size": "-5"}, {"offset": "0", "size": "x"}])
def test_invalid_offset_or_size_400(dirs, params):
    uid = _slot()
    r = _put(uid, b"a" * 10, **params)
    assert r.status_code == 400
    assert app_module.pending_uploads[uid]["bytes"] == 0


def test_process_before_last_chunk_409(dirs):
    uid = _slot()
    assert _put(uid, b"a" * 10, offset=0).status_code == 200
    r = _call("POST", "/api/process", json={"upload_id": uid, "acknowledged": True})
    assert r.status_code == 409, r.text
    assert app_module.jobs == {}
    assert _put(uid, b"b" * 10, offset=10, size=20).json()["complete"] is True
    r = _call("POST", "/api/process", json={"upload_id": uid, "acknowledged": True})
    assert r.status_code == 200, r.text
    assert uid not in app_module.pending_uploads


def test_size_mismatch_400(dirs):
    uid = _slot()
    assert _put(uid, b"a" * 10, offset=0).status_code == 200
    r = _put(uid, b"b" * 10, offset=10, size=30)
    assert r.status_code == 400
    assert r.json() == {"detail": "size mismatch"}
    assert app_module.pending_uploads[uid]["complete"] is False
    assert app_module.pending_uploads[uid]["bytes"] == 20


def test_unreadable_final_chunk_400(dirs, monkeypatch):
    monkeypatch.setattr(app_module, "_media_duration_seconds", lambda p: 0.0)
    uid = _slot()
    assert _put(uid, b"a" * 10, offset=0).status_code == 200
    r = _put(uid, b"b" * 10, offset=10, size=20)
    assert r.status_code == 400
    slot = app_module.pending_uploads[uid]
    assert slot["complete"] is False and slot["bytes"] == 0
    assert not os.path.exists(slot["path"])
