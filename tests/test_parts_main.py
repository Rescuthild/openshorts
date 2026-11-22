"""Parts mode in main.py's job flow (spec 2026-11-23-parts-mode-design.md §2,
§3.1, §3.4, §5): ``--parts SECONDS`` cuts the whole video into sequential
parts instead of letting the model pick moments.
"""
import json
import os
import subprocess
import sys

import pytest

main = pytest.importorskip("main")
import scene_detection  # noqa: E402


def must_not_be_called(*args, **kwargs):
    pytest.fail("parts mode must not call Gemini without any spoken words")


@pytest.fixture(autouse=True)
def _no_prepared_scenes(monkeypatch):
    """Every test starts without loaded scene probabilities (module state)."""
    monkeypatch.setitem(scene_detection._prepared, "probs", None)
    monkeypatch.setitem(scene_detection._prepared, "fps", None)


def test_apply_parts_env_sets_required_flags():
    env = {"AUTO_HOOK": "0", "PRO_MODE": "suggest"}
    main.apply_parts_env(env)
    assert env == {"AUTO_HOOK": "1", "HOOK_GROUNDING": "0", "PRO_MODE": "off",
                   "AUTO_LAYOUT": "0"}


def test_parts_force_strategy_vertical_source_is_none():
    assert main.parts_force_strategy(1080, 1920) is None
    assert main.parts_force_strategy(1920, 1080) == "WIDE"


def test_parts_framing_records_full_for_a_wide_source():
    # Spec §5: the editor's re-renders (/api/clip/rerender source path,
    # /api/clip/reframe) must keep the full frame instead of re-classifying.
    clips = [{"start": 0.0, "end": 60.0}, {"start": 60.0, "end": 120.0}]
    main.parts_framing(clips, "WIDE")
    assert [c["recipe"] for c in clips] == [{"framing": "full"}, {"framing": "full"}]


def test_parts_framing_leaves_a_vertical_source_alone():
    clips = [{"start": 0.0, "end": 60.0}]
    main.parts_framing(clips, None)
    assert clips == [{"start": 0.0, "end": 60.0}]


def test_build_parts_without_transcript_makes_parts_and_no_gemini_call(monkeypatch):
    monkeypatch.setattr(main, "_run_gemini_stage", must_not_be_called)
    out = main.build_parts(None, 139.0, 60)
    assert [c["viral_hook_text"] for c in out["shorts"]] == ["1. Part", "2. Part"]
    assert out["shorts"][-1]["end"] == 139.0
    assert "cost_analysis" in out


SOURCE = "/jobs/x/source.mp4"


def _write_scene_checkpoint(directory, duration, fps, cut_frame, name="source.mp4"):
    n = int(duration * fps)
    probs = [0.0] * n
    probs[cut_frame] = 0.9
    payload = scene_detection.scenes_payload(name, duration, probs, fps)
    assert scene_detection.valid_scenes_payload(payload)
    with open(os.path.join(directory, scene_detection.SCENES_CHECKPOINT), "w") as f:
        json.dump(payload, f)


def test_parts_scene_cuts_come_from_the_validated_checkpoint(tmp_path):
    _write_scene_checkpoint(str(tmp_path), 150.0, 10, 580)
    assert main.parts_scene_cuts() == []            # nothing validated yet
    assert scene_detection.use_prepared(str(tmp_path), SOURCE, 150.0)
    assert main.parts_scene_cuts() == [pytest.approx(58.0)]


def test_parts_scene_cuts_ignore_another_sources_checkpoint(tmp_path):
    _write_scene_checkpoint(str(tmp_path), 150.0, 10, 580, name="other.mp4")
    assert not scene_detection.use_prepared(str(tmp_path), SOURCE, 150.0)
    assert main.parts_scene_cuts() == []
    _write_scene_checkpoint(str(tmp_path), 150.0, 10, 580)
    assert not scene_detection.use_prepared(str(tmp_path), SOURCE, 300.0)  # other length
    assert main.parts_scene_cuts() == []


def test_parts_scene_cuts_missing_or_broken_is_empty(tmp_path):
    assert not scene_detection.use_prepared(str(tmp_path), SOURCE, 150.0)
    assert main.parts_scene_cuts() == []
    with open(os.path.join(str(tmp_path), scene_detection.SCENES_CHECKPOINT), "w") as f:
        f.write("{not json")
    assert not scene_detection.use_prepared(str(tmp_path), SOURCE, 150.0)
    assert main.parts_scene_cuts() == []


def test_parts_prepare_scenes_reuses_a_valid_checkpoint(monkeypatch, tmp_path):
    # A resumed job must not run TransNetV2 over the whole source again.
    _write_scene_checkpoint(str(tmp_path), 150.0, 10, 580)
    monkeypatch.setattr(main, "prepare_scenes", must_not_be_called)
    assert main.parts_prepare_scenes(SOURCE, str(tmp_path), 150.0)
    assert main.parts_scene_cuts() == [pytest.approx(58.0)]


def test_parts_prepare_scenes_prepares_when_there_is_no_checkpoint(monkeypatch, tmp_path):
    calls = []

    def fake_prepare(input_video, output_dir):
        calls.append(input_video)
        _write_scene_checkpoint(output_dir, 150.0, 10, 580)
        return True
    monkeypatch.setattr(main, "prepare_scenes", fake_prepare)
    assert main.parts_prepare_scenes(SOURCE, str(tmp_path), 150.0)
    assert calls == [SOURCE]
    assert main.parts_scene_cuts() == [pytest.approx(58.0)]


def test_parts_prepare_scenes_replaces_another_sources_checkpoint(monkeypatch, tmp_path):
    _write_scene_checkpoint(str(tmp_path), 150.0, 10, 580, name="other.mp4")
    calls = []

    def fake_prepare(input_video, output_dir):
        calls.append(input_video)
        return False                                 # off the card: nothing written
    monkeypatch.setattr(main, "prepare_scenes", fake_prepare)
    assert not main.parts_prepare_scenes(SOURCE, str(tmp_path), 150.0)
    assert calls == [SOURCE]
    assert main.parts_scene_cuts() == []


def test_build_parts_uses_scene_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_run_gemini_stage", must_not_be_called)
    _write_scene_checkpoint(str(tmp_path), 150.0, 10, 580)
    assert scene_detection.use_prepared(str(tmp_path), SOURCE, 150.0)
    out = main.build_parts(None, 150.0, 60)
    assert out["shorts"][0]["end"] == pytest.approx(58.0)


def test_build_parts_warns_when_cap_truncates(monkeypatch, capsys):
    monkeypatch.setattr(main, "_run_gemini_stage", must_not_be_called)
    monkeypatch.setenv("CLIP_MAX_PER_JOB", "2")
    out = main.build_parts(None, 600.0, 60)
    assert len(out["shorts"]) == 2
    assert out["shorts"][-1]["end"] < 600.0
    assert "⚠️" in capsys.readouterr().out


class _Probe:
    def __init__(self, stdout=b"", returncode=0):
        self.stdout, self.returncode = stdout, returncode


def test_parts_source_duration_prefers_ffprobe(monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return _Probe(b"151.433000\n")
    monkeypatch.setattr(main.subprocess, "run", fake_run)
    assert main.parts_source_duration("in.mkv", 150.0) == pytest.approx(151.433)
    assert seen[0][0] == "ffprobe" and "format=duration" in seen[0]


@pytest.mark.parametrize("probe", [_Probe(b"N/A\n"), _Probe(b""), _Probe(b"0\n"),
                                   _Probe(b"nan\n"), _Probe(b"inf\n"),
                                   _Probe(b"151.4\n", returncode=1)])
def test_parts_source_duration_falls_back_to_the_frame_count(monkeypatch, probe):
    monkeypatch.setattr(main.subprocess, "run", lambda cmd, **kw: probe)
    assert main.parts_source_duration("in.mkv", 150.0) == 150.0


def test_parts_source_duration_survives_a_missing_ffprobe(monkeypatch):
    def boom(cmd, **kw):
        raise FileNotFoundError("ffprobe")
    monkeypatch.setattr(main.subprocess, "run", boom)
    assert main.parts_source_duration("in.mkv", 150.0) == 150.0


@pytest.fixture()
def subtitle_check(monkeypatch):
    """The burned-in subtitle check can run: enabled and a key is set."""
    import burned_subtitles
    monkeypatch.setattr(burned_subtitles, "ENABLED", True)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    return burned_subtitles


def test_parts_burned_subtitles_found_turns_captions_off(subtitle_check, monkeypatch, capsys):
    monkeypatch.setattr(subtitle_check, "detect", lambda path: 0.8)
    env = {}
    main.parts_burned_subtitles("video.mp4", env)
    assert env == {"AUTO_CAPTIONS": "0"}
    assert ("📝 Parts: source has burned-in subtitles — keeping them, no captions added."
            in capsys.readouterr().out)


def test_parts_burned_subtitles_not_found_keeps_captions(subtitle_check, monkeypatch, capsys):
    monkeypatch.setattr(subtitle_check, "detect", lambda path: None)
    env = {}
    main.parts_burned_subtitles("video.mp4", env)
    assert env == {}
    assert "📝 Parts: no burned-in subtitles — adding captions." in capsys.readouterr().out


def test_parts_burned_subtitles_error_keeps_captions(subtitle_check, monkeypatch, capsys):
    def boom(path):
        raise RuntimeError("vision down")
    monkeypatch.setattr(subtitle_check, "detect", boom)
    env = {}
    main.parts_burned_subtitles("video.mp4", env)
    assert env == {}
    out = capsys.readouterr().out
    assert "📝 Parts: subtitle check unavailable — adding captions." in out
    assert "no burned-in subtitles" not in out


def test_parts_burned_subtitles_check_disabled_says_so(monkeypatch, capsys):
    import burned_subtitles
    monkeypatch.setattr(burned_subtitles, "ENABLED", False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(burned_subtitles, "detect", must_not_be_called)
    env = {}
    main.parts_burned_subtitles("video.mp4", env)
    assert env == {}
    out = capsys.readouterr().out
    assert "📝 Parts: subtitle check unavailable — adding captions." in out
    assert "no burned-in subtitles" not in out


def test_parts_burned_subtitles_without_key_says_so(monkeypatch, capsys):
    import burned_subtitles
    monkeypatch.setattr(burned_subtitles, "ENABLED", True)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(burned_subtitles, "detect", must_not_be_called)
    env = {}
    main.parts_burned_subtitles("video.mp4", env)
    assert env == {}
    assert "📝 Parts: subtitle check unavailable — adding captions." in capsys.readouterr().out


def test_parts_burned_subtitles_skipped_when_captions_off(monkeypatch):
    import burned_subtitles
    monkeypatch.setattr(burned_subtitles, "detect", must_not_be_called)
    env = {"AUTO_CAPTIONS": "0"}
    main.parts_burned_subtitles("video.mp4", env)
    assert env == {"AUTO_CAPTIONS": "0"}


def test_parts_flag_parses():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    help_text = subprocess.run([sys.executable, "main.py", "--help"], cwd=repo,
                               capture_output=True, text=True, timeout=300).stdout
    assert "--parts" in help_text
