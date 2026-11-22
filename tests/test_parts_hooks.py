"""Parts mode hooks: one Gemini request writes every part's hook (spec
2026-11-23-parts-mode-design.md §3.5, §4).

A failed or blocked request must never fail the job: the parts still ship,
labelled "N. Part" without a hook.
"""
import pytest

import parts

main = pytest.importorskip("main")


def _segment(start, words):
    ws = [{"word": f" {w}", "start": start + i, "end": start + i + 0.8}
          for i, w in enumerate(words)]
    return {"start": start, "end": start + len(words), "text": " ".join(words),
            "words": ws}


TRANSCRIPT = {
    "language": "tr",
    "segments": [_segment(0.0, ["birinci", "parça", "konuşması."]),
                 _segment(60.0, ["ikinci", "parça", "burada."])],
}

EMPTY_HOOK = {"viral_hook_text": "", "title": "", "description": ""}


def raising(exc):
    def fake(client, model, prompt, schema):
        raise exc
    return fake


def must_not_be_called(client, model, prompt, schema):
    pytest.fail("parts mode must not call Gemini without any spoken words")


@pytest.fixture(autouse=True)
def _gemini_env(monkeypatch):
    """A Gemini key and a client that never touches the network."""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(main.genai, "Client", lambda **kw: object())


def test_get_part_hooks_makes_exactly_one_call(monkeypatch):
    calls = []

    def fake(client, model, prompt, schema):
        calls.append(schema)
        return {"parts": [{"part": 1, "viral_hook_text": "Bak 😳", "title": "T1"},
                          {"part": 2, "viral_hook_text": "Devamı 🔥", "title": "T2"}]}, {}
    monkeypatch.setattr(main, "_run_gemini_stage", fake)
    hooks, _ = main.get_part_hooks(TRANSCRIPT, [(0, 60), (60, 120)])
    assert calls == [main.gemini_worker.PartHooksResponse]
    assert [h["viral_hook_text"] for h in hooks] == ["Bak 😳", "Devamı 🔥"]


def test_get_part_hooks_prompt_carries_language_and_part_texts(monkeypatch):
    prompts = []

    def fake(client, model, prompt, schema):
        prompts.append(prompt)
        return {"parts": []}, None
    monkeypatch.setattr(main, "_run_gemini_stage", fake)
    main.get_part_hooks(TRANSCRIPT, [(0, 60), (60, 120)])
    assert len(prompts) == 1
    assert 'language is "tr"' in prompts[0]
    assert "birinci" in prompts[0] and "ikinci" in prompts[0]


def test_get_part_hooks_survives_gemini_failure(monkeypatch):
    monkeypatch.setattr(main, "_run_gemini_stage", raising(RuntimeError("503")))
    hooks, cost = main.get_part_hooks(TRANSCRIPT, [(0, 60)])
    assert hooks == [EMPTY_HOOK]
    assert cost["total_cost"] == 0


def test_get_part_hooks_survives_policy_block(monkeypatch):
    monkeypatch.setattr(main, "_run_gemini_stage",
                        raising(main.gemini_worker.GeminiBlockedError("PROHIBITED_CONTENT")))
    hooks, _ = main.get_part_hooks(TRANSCRIPT, [(0, 60), (60, 120)])
    assert hooks == [EMPTY_HOOK, EMPTY_HOOK]


def test_get_part_hooks_without_words_skips_gemini(monkeypatch):
    monkeypatch.setattr(main, "_run_gemini_stage", must_not_be_called)
    hooks, cost = main.get_part_hooks(None, [(0, 60), (60, 120)])
    assert len(hooks) == 2 and all(h["viral_hook_text"] == "" for h in hooks)
    assert cost["total_cost"] == 0


def test_get_part_hooks_reports_cost_of_the_call(monkeypatch):
    def fake(client, model, prompt, schema):
        return {"parts": []}, {"input_tokens": 100, "output_tokens": 20,
                               "total_cost": 0.001, "model": model}
    monkeypatch.setattr(main, "_run_gemini_stage", fake)
    _, cost = main.get_part_hooks(TRANSCRIPT, [(0, 60)])
    assert cost["input_tokens"] == 100 and cost["output_tokens"] == 20
    assert cost["total_cost"] == pytest.approx(0.001)


def test_part_hooks_maps_by_number_and_strips_part_prefix():
    raw = {"parts": [{"part": 2, "viral_hook_text": "2. Part: Sonra 🔥", "title": "b"},
                     {"part": 9, "viral_hook_text": "fazla", "title": "x"}]}
    h = parts.normalize_hooks(raw, 2)
    assert h[0]["viral_hook_text"] == "" and h[1]["viral_hook_text"] == "Sonra 🔥"
    assert h[1]["title"] == "b" and h[1]["description"] == ""


def test_normalize_hooks_trims_and_tolerates_missing_payload():
    assert parts.normalize_hooks(None, 2) == [EMPTY_HOOK, EMPTY_HOOK]
    assert parts.normalize_hooks({}, 1) == [EMPTY_HOOK]
    h = parts.normalize_hooks({"parts": [{"part": 1, "viral_hook_text": "  Bak 😳 ",
                                          "title": " T ", "description": " d "}]}, 1)
    assert h == [{"viral_hook_text": "Bak 😳", "title": "T", "description": "d"}]


def test_build_part_clips_formats():
    c = parts.build_part_clips([(0.0, 58.5), (58.5, 120.0)],
                               [{"viral_hook_text": "Bak 😳", "title": "Kaşık", "description": "d"},
                                {"viral_hook_text": "", "title": "", "description": ""}])
    assert c[0]["viral_hook_text"] == "1. Part\nBak 😳"
    assert c[0]["video_title_for_youtube_short"] == "1. Part – Kaşık"
    assert c[0]["video_description_for_tiktok"] == "d"
    assert c[0]["video_description_for_instagram"] == "d"
    assert (c[0]["start"], c[0]["end"]) == (0.0, 58.5)
    assert c[1]["viral_hook_text"] == "2. Part"
    assert c[1]["video_title_for_youtube_short"] == "2. Part"
    assert c[1]["part_number"] == 2 and "predicted_score" not in c[0]


def test_part_texts_splits_words_by_range_and_truncates():
    texts = parts.part_texts(TRANSCRIPT, [(0, 60), (60, 120)])
    assert texts == ["birinci parça konuşması.", "ikinci parça burada."]
    assert parts.part_texts(None, [(0, 60)]) == [""]
    long = parts.part_texts(TRANSCRIPT, [(0, 60)], max_chars=10)[0]
    assert len(long) <= 10 and long.startswith("birinci")


def test_prompt_mentions_language_and_every_part():
    p = parts.build_hooks_prompt("tr", ["birinci", "ikinci"])
    assert 'language is "tr"' in p and "birinci" in p and "ikinci" in p


def _prompt_parts(prompt):
    import json
    return json.loads(prompt.split("PARTS_JSON:")[-1])


def test_prompt_leaves_silent_parts_out_and_keeps_numbering():
    # Spec §2: no text goes to the model for a part without speech.
    p = parts.build_hooks_prompt("tr", ["birinci", "", "üçüncü"])
    assert _prompt_parts(p) == [{"part": 1, "text": "birinci"},
                                {"part": 3, "text": "üçüncü"}]
    assert "no speech" not in p


SILENT_MIDDLE = {
    "language": "tr",
    "segments": [_segment(0.0, ["birinci", "parça."]),
                 _segment(130.0, ["üçüncü", "parça."])],
}
THREE = [(0, 60), (60, 120), (120, 180)]


def test_silent_part_gets_no_hook_even_if_the_model_writes_one(monkeypatch):
    prompts = []

    def fake(client, model, prompt, schema):
        prompts.append(prompt)
        return {"parts": [{"part": n, "viral_hook_text": f"Hook {n} 🔥", "title": f"T{n}",
                           "description": f"d{n}"} for n in (1, 2, 3)]}, None
    monkeypatch.setattr(main, "_run_gemini_stage", fake)
    hooks, _ = main.get_part_hooks(SILENT_MIDDLE, THREE)
    assert [n["part"] for n in _prompt_parts(prompts[0])] == [1, 3]
    assert hooks[1] == EMPTY_HOOK
    assert hooks[0]["viral_hook_text"] == "Hook 1 🔥" and hooks[2]["title"] == "T3"
    clips = parts.build_part_clips(THREE, hooks)
    assert clips[1]["viral_hook_text"] == "2. Part"
    assert clips[1]["video_title_for_youtube_short"] == "2. Part"
    assert clips[1]["video_description_for_tiktok"] == ""


def test_partial_response_leaves_the_missing_spoken_part_empty(monkeypatch, capsys):
    def fake(client, model, prompt, schema):
        return {"parts": [{"part": 1, "viral_hook_text": "Bak 😳", "title": "T1"}]}, None
    monkeypatch.setattr(main, "_run_gemini_stage", fake)
    hooks, _ = main.get_part_hooks(SILENT_MIDDLE, THREE)
    assert hooks[0]["viral_hook_text"] == "Bak 😳"
    assert hooks[1] == EMPTY_HOOK and hooks[2] == EMPTY_HOOK
    # Only the spoken part the model skipped counts as missing.
    assert "no hook for 1/3 part(s)" in capsys.readouterr().out
