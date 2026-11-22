"""/api/process `mode` and `part_seconds` (spec 2026-11-23-parts-mode §1).

mode=parts turns the request into `main.py ... --parts SECONDS` with the hook
render forced on and the extra Gemini/face passes off; the clip-mode controls
(layouts, target_clips, the duration band) are ignored. Without mode (or with
mode=clips) the job is exactly what it was before. The queue is never drained
here, so main.py never runs: the job is read back from `app_module.jobs`.
"""
import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest

app_module = pytest.importorskip("app")

URL = "https://www.youtube.com/watch?v=ok"

# Process env that would mask what the endpoint writes.
_ENV_VARS = ("AUTO_HOOK", "AUTO_HOOK_STYLE", "HOOK_GROUNDING", "PRO_MODE", "AUTO_CAPTIONS",
             "CLIP_TARGET_MIN", "CLIP_TARGET_MAX", "CLIP_MIN_SECONDS", "CLIP_MAX_SECONDS",
             "AUTO_LAYOUT", "SPLIT_LAYOUT", "SCREENCAST_LAYOUT", "SPEAKER_CUT", "PUNCH_IN",
             "SPEAKER_SIGNAL")


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
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)

    async def _reserve(request, url, input_path, job_id, max_minutes=None, **_kw):
        return None, 2, None, None, None

    async def _no_gate(url, force_low):
        return None

    async def _allow(url):
        return None

    monkeypatch.setattr(app_module, "reserve_process_minutes", _reserve)
    monkeypatch.setattr(app_module, "_quality_gate", _no_gate)
    monkeypatch.setattr(app_module, "_validate_source_url", _allow)
    return SimpleNamespace(out=out_root, up=up_root)


def _post(**kw):
    async def _do():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     headers={"X-Gemini-Key": "test-key"}) as c:
            return await c.post("/api/process", **kw)
    return asyncio.run(_do())


def _process(**fields):
    resp = _post(json={"url": URL, "acknowledged": True, **fields})
    assert resp.status_code == 200, resp.text
    return app_module.jobs[resp.json()["job_id"]]


def _assert_refused(dirs, resp):
    assert resp.status_code == 400, resp.text
    assert app_module.jobs == {}
    assert app_module.job_queue.qsize() == 0
    assert os.listdir(dirs.out) == [] and os.listdir(dirs.up) == []


def test_parts_mode_adds_flag_and_hook_env(dirs):
    job = _process(mode="parts")
    assert job["cmd"][-2:] == ["--parts", "60"]
    assert job["env"]["AUTO_HOOK"] == "1"
    assert job["env"]["HOOK_GROUNDING"] == "0"
    assert job["env"]["PRO_MODE"] == "off"
    assert app_module.job_queue.qsize() == 1


def test_part_seconds_passes_through(dirs):
    assert _process(mode="parts", part_seconds=90)["cmd"][-2:] == ["--parts", "90"]
    assert _process(mode="parts", part_seconds="45")["cmd"][-2:] == ["--parts", "45"]
    assert _process(mode="parts", part_seconds=30)["cmd"][-2:] == ["--parts", "30"]
    assert _process(mode="parts", part_seconds=180)["cmd"][-2:] == ["--parts", "180"]


def test_parts_flag_comes_after_format(dirs):
    cmd = _process(mode="parts", output_format="vertical")["cmd"]
    assert cmd[-4:] == ["--format", "vertical", "--parts", "60"]


@pytest.mark.parametrize("bad", [29, 181, 60.5, "abc", "inf", "-inf", "nan"])
def test_part_seconds_out_of_range_is_400(dirs, bad):
    _assert_refused(dirs, _post(json={"url": URL, "acknowledged": True,
                                      "mode": "parts", "part_seconds": bad}))


@pytest.mark.parametrize("bad", ["foo", "PARTS", 5])
def test_unknown_mode_is_400(dirs, bad):
    _assert_refused(dirs, _post(json={"url": URL, "acknowledged": True, "mode": bad}))


def test_parts_mode_ignores_clip_controls(dirs):
    job = _process(mode="parts", target_clips=3, clip_min_seconds=20, clip_max_seconds=40,
                   layouts=["auto", "split", "punch_in"])
    env = job["env"]
    for name in ("CLIP_TARGET_MIN", "CLIP_TARGET_MAX", "CLIP_MIN_SECONDS", "CLIP_MAX_SECONDS",
                 "AUTO_LAYOUT", "SPLIT_LAYOUT", "PUNCH_IN", "SPEAKER_SIGNAL"):
        assert name not in env, name


def test_parts_mode_ignores_out_of_range_clip_controls(dirs):
    # Ignored means not validated either: the dashboard may keep sending them.
    job = _process(mode="parts", target_clips=99, clip_min_seconds=1, clip_max_seconds=999)
    assert job["cmd"][-2:] == ["--parts", "60"]


def test_parts_mode_still_honors_captions(dirs):
    assert _process(mode="parts", captions=False)["env"]["AUTO_CAPTIONS"] == "0"


def test_clip_mode_unchanged(dirs):
    job = _process()
    assert "--parts" not in job["cmd"]
    for name in ("AUTO_HOOK", "HOOK_GROUNDING", "PRO_MODE"):
        assert name not in job["env"], name
    # Clip controls still apply outside parts mode.
    job = _process(target_clips=3, layouts=["split"])
    assert job["env"]["CLIP_TARGET_MIN"] == "3" and job["env"]["SPLIT_LAYOUT"] == "1"
    assert "--parts" not in job["cmd"]


def test_explicit_clips_mode_matches_absent_mode_and_ignores_part_seconds(dirs):
    plain = _process(target_clips=4, output_format="square")
    clips = _process(mode="clips", part_seconds=999, target_clips=4, output_format="square")

    def _norm(job):
        return [a.replace(os.path.basename(job["output_dir"]), "<job>") for a in job["cmd"]]
    assert _norm(clips) == _norm(plain)
    assert clips["env"] == plain["env"]


def test_parts_mode_from_multipart_form(dirs):
    resp = _post(data={"acknowledged": "true", "mode": "parts", "part_seconds": "45",
                       "target_clips": "3", "captions": "false"},
                 files={"file": ("talk.mp4", b"\x00" * 1000, "video/mp4")})
    assert resp.status_code == 200, resp.text
    job = app_module.jobs[resp.json()["job_id"]]
    assert job["cmd"][-2:] == ["--parts", "45"]
    assert job["env"]["AUTO_HOOK"] == "1" and job["env"]["PRO_MODE"] == "off"
    assert job["env"]["AUTO_CAPTIONS"] == "0"
    assert "CLIP_TARGET_MIN" not in job["env"]


def test_multipart_bad_part_seconds_writes_nothing(dirs):
    resp = _post(data={"acknowledged": "true", "mode": "parts", "part_seconds": "200"},
                 files={"file": ("talk.mp4", b"\x00" * 1000, "video/mp4")})
    _assert_refused(dirs, resp)


def test_full_framing_is_the_wide_render_and_needs_no_segments():
    # Parts of a 16:9 source carry recipe.framing = "full" (main.py,
    # parts_framing): it must map to the WIDE full-frame render, and a recipe
    # with only a framing must still yield the clip's own range.
    assert app_module._FRAMING_STRATEGIES["full"] == "WIDE"
    segments, canonical = app_module._clip_recipe_parts(
        {"start": 60.0, "end": 118.5, "recipe": {"framing": "full"}})
    assert segments == [{"start": 60.0, "end": 118.5}]
    assert canonical == {"start": 60.0, "end": 118.5}
