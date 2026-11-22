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


def test_apply_parts_env_sets_required_flags():
    env = {"AUTO_HOOK": "0", "PRO_MODE": "suggest"}
    main.apply_parts_env(env)
    assert env == {"AUTO_HOOK": "1", "HOOK_GROUNDING": "0", "PRO_MODE": "off",
                   "AUTO_LAYOUT": "0"}


def test_parts_force_strategy_vertical_source_is_none():
    assert main.parts_force_strategy(1080, 1920) is None
    assert main.parts_force_strategy(1920, 1080) == "WIDE"


def test_build_parts_without_transcript_makes_parts_and_no_gemini_call(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_run_gemini_stage", must_not_be_called)
    out = main.build_parts(None, 139.0, str(tmp_path), 60)
    assert [c["viral_hook_text"] for c in out["shorts"]] == ["1. Part", "2. Part"]
    assert out["shorts"][-1]["end"] == 139.0
    assert "cost_analysis" in out


def _write_scene_checkpoint(directory, duration, fps, cut_frame):
    n = int(duration * fps)
    probs = [0.0] * n
    probs[cut_frame] = 0.9
    payload = scene_detection.scenes_payload("source.mp4", duration, probs, fps)
    with open(os.path.join(directory, scene_detection.SCENES_CHECKPOINT), "w") as f:
        json.dump(payload, f)


def test_parts_scene_cuts_reads_checkpoint(tmp_path):
    _write_scene_checkpoint(str(tmp_path), 150.0, 10, 580)
    assert main.parts_scene_cuts(str(tmp_path)) == [pytest.approx(58.0)]


def test_parts_scene_cuts_missing_or_broken_is_empty(tmp_path):
    assert main.parts_scene_cuts(str(tmp_path)) == []
    with open(os.path.join(str(tmp_path), scene_detection.SCENES_CHECKPOINT), "w") as f:
        f.write("{not json")
    assert main.parts_scene_cuts(str(tmp_path)) == []


def test_build_parts_uses_scene_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_run_gemini_stage", must_not_be_called)
    _write_scene_checkpoint(str(tmp_path), 150.0, 10, 580)
    out = main.build_parts(None, 150.0, str(tmp_path), 60)
    assert out["shorts"][0]["end"] == pytest.approx(58.0)


def test_build_parts_warns_when_cap_truncates(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(main, "_run_gemini_stage", must_not_be_called)
    monkeypatch.setenv("CLIP_MAX_PER_JOB", "2")
    out = main.build_parts(None, 600.0, str(tmp_path), 60)
    assert len(out["shorts"]) == 2
    assert out["shorts"][-1]["end"] < 600.0
    assert "⚠️" in capsys.readouterr().out


def test_parts_burned_subtitles_found_turns_captions_off(monkeypatch, capsys):
    import burned_subtitles
    monkeypatch.setattr(burned_subtitles, "detect", lambda path: 0.8)
    env = {}
    main.parts_burned_subtitles("video.mp4", env)
    assert env == {"AUTO_CAPTIONS": "0"}
    assert ("📝 Parts: source has burned-in subtitles — keeping them, no captions added."
            in capsys.readouterr().out)


def test_parts_burned_subtitles_not_found_keeps_captions(monkeypatch, capsys):
    import burned_subtitles
    monkeypatch.setattr(burned_subtitles, "detect", lambda path: None)
    env = {}
    main.parts_burned_subtitles("video.mp4", env)
    assert env == {}
    assert "📝 Parts: no burned-in subtitles — adding captions." in capsys.readouterr().out


def test_parts_burned_subtitles_error_keeps_captions(monkeypatch, capsys):
    import burned_subtitles

    def boom(path):
        raise RuntimeError("vision down")
    monkeypatch.setattr(burned_subtitles, "detect", boom)
    env = {}
    main.parts_burned_subtitles("video.mp4", env)
    assert env == {}
    assert "📝 Parts: no burned-in subtitles — adding captions." in capsys.readouterr().out


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
