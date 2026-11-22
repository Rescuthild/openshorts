"""Parts mode: the sequential splitter cuts a whole video into ~P-second parts,
snapping each cut to a sentence end, else a pause, else a scene cut — never
inside a word (spec 2026-11-23-parts-mode-design.md §3)."""
import pytest

parts = pytest.importorskip("parts")


def _run(start, n, word=0.75, gap=0.25):
    """``n`` back-to-back words from ``start``: each ``word`` s long, ``gap`` s apart."""
    step = word + gap
    return [(start + i * step, start + i * step + word, "w") for i in range(n)]


def _first_cut(*args, **kwargs):
    return parts.plan_parts(*args, **kwargs)[0][1]


# Word i spans (i + 0.25, i + 1.0): every word ends on a whole second and the
# 0.25 s pauses between them are below the 0.3 s pause threshold.
SPEECH_120 = _run(0.25, 120)


def test_sentence_end_beats_scene_cut():
    # Sentence ends at 57.0 (pause 57.0-57.25); a scene cut at 61.4 lands mid-word
    # and is closer to the 60 s target, but sentence integrity wins.
    cut = _first_cut(120.0, 60, SPEECH_120, sentence_ends=[57.0], scene_cuts=[61.4])
    assert cut == pytest.approx(57.0 + 0.25 / 2)


def test_scene_cut_inside_pause_after_sentence_end_is_used():
    # Sentence ends at 59.2, then a 59.2-59.9 pause holding a scene cut at 59.6.
    words = _run(0.45, 59) + _run(59.9, 60)
    assert words[58][1] == pytest.approx(59.2)
    cut = _first_cut(120.0, 60, words, sentence_ends=[59.2], scene_cuts=[59.6])
    assert cut == pytest.approx(59.6)


def test_nearest_sentence_end_to_target_wins():
    cut = _first_cut(120.0, 60, SPEECH_120, sentence_ends=[56.0, 61.0], scene_cuts=[])
    assert cut == pytest.approx(61.125)


def test_sentence_end_inside_a_word_is_rejected():
    # 60.5 falls inside the word (60.25, 61.0): not a usable cut; 63.0 is.
    cut = _first_cut(120.0, 60, SPEECH_120, sentence_ends=[60.5, 63.0], scene_cuts=[])
    assert cut == pytest.approx(63.125)


def test_window_widens_to_ten_seconds():
    # No sentence end in [55, 65]; one at 52.0 inside [50, 70].
    cut = _first_cut(120.0, 60, SPEECH_120, sentence_ends=[52.0, 49.0, 71.0],
                     scene_cuts=[])
    assert cut == pytest.approx(52.0 + 0.25 / 2)


def _two_pauses():
    words = list(SPEECH_120)
    words[57] = (57.4, 58.0, "w")   # 57.0-57.4 pause (0.4 s)
    words[62] = (62.9, 63.0, "w")   # 62.0-62.9 pause (0.9 s)
    return words


def test_largest_pause_when_no_sentence_end():
    # Sentence ends exist, but outside [50, 70].
    cut = _first_cut(120.0, 60, _two_pauses(), sentence_ends=[45.0, 75.0],
                     scene_cuts=[])
    assert cut == pytest.approx(62.45)


def test_scene_cut_inside_largest_pause_is_used():
    cut = _first_cut(120.0, 60, _two_pauses(), sentence_ends=[],
                     scene_cuts=[61.5, 62.7])
    assert cut == pytest.approx(62.7)


def test_silent_window_uses_nearest_scene_cut_else_target():
    # No speech between 50 and 70.
    words = [w for w in SPEECH_120 if w[1] <= 50.0 or w[0] >= 70.0]
    assert _first_cut(120.0, 60, words, [], scene_cuts=[58.0, 66.0]) == pytest.approx(58.0)
    assert _first_cut(120.0, 60, words, [], scene_cuts=[]) == pytest.approx(60.0)


def test_continuous_speech_cuts_on_word_boundary():
    # 0.1 s gaps (below the threshold), no sentence end, a mid-word scene cut.
    words = _run(0.05, 120, word=0.9, gap=0.1)
    cut = _first_cut(120.0, 60, words, [], scene_cuts=[60.5])
    assert any(cut == pytest.approx(e) for (_, e, _) in words)
    assert not any(s < cut < e for (s, e, _) in words)
    assert cut == pytest.approx(59.95)


def test_tail_under_20s_merges():
    assert parts.plan_parts(139.0, 60, [], [], []) == [(0.0, 60.0), (60.0, 139.0)]


def test_tail_of_20s_stays():
    assert parts.plan_parts(140.0, 60, [], [], [])[-1] == (120.0, 140.0)


def test_merge_never_exceeds_180():
    r = parts.plan_parts(370.0, 180, [], [], [])
    assert r == [(0.0, 180.0), (180.0, 360.0), (360.0, 370.0)]
    assert all(e - s <= 180.0 for s, e in r)


def test_short_video_single_part():
    assert parts.plan_parts(75.0, 60, [], [], []) == [(0.0, 75.0)]


def test_short_video_single_part_even_with_early_sentence_end():
    # A cut at 52.125 would leave a 22.9 s tail; under P + 20 s it stays one part.
    words = _run(0.25, 75)
    assert parts.plan_parts(75.0, 60, words, [52.0], []) == [(0.0, 75.0)]


def test_max_parts_cap():
    r = parts.plan_parts(7200.0, 60, [], [], [])
    assert len(r) == 60
    assert r[-1] == (3540.0, 3600.0)   # the remainder past the 60th cut is dropped


def test_parts_are_contiguous_and_cover_video():
    words = _run(0.25, 600)
    ends = [float(k) for k in range(7, 601, 7)]
    cuts = [k + 0.5 for k in range(13, 600, 13)]   # all mid-word
    r = parts.plan_parts(600.0, 60, words, ends, cuts)
    assert r[0][0] == 0.0 and r[-1][1] == 600.0
    assert all(a[1] == b[0] for a, b in zip(r, r[1:]))
    assert all(e - s <= 180.0 for s, e in r)
    assert all(50.0 <= e - s <= 70.0 for s, e in r[:-1])
    for _, cut in r[:-1]:
        assert not any(s < cut < e for (s, e, _) in words)


def test_long_parts_never_exceed_180():
    # With P = 180 the +10 s window would allow 190 s parts; the cap wins.
    words = _run(0.25, 400)
    r = parts.plan_parts(400.0, 180, words, [187.0], [])
    assert all(e - s <= 180.0 for s, e in r)


@pytest.mark.parametrize("bad", [29, 181, 0])
def test_part_seconds_out_of_range_rejected(bad):
    with pytest.raises(ValueError):
        parts.plan_parts(600.0, bad, [], [], [])


def test_words_from_transcript_sorted_and_tolerant():
    transcript = {"segments": [
        {"start": 5.0, "end": 6.0, "words": [{"word": " b", "start": 5.0, "end": 5.5}]},
        {"start": 0.0, "end": 1.0, "words": [{"word": " a", "start": 0.0, "end": 0.4}]},
        {"start": 2.0, "end": 3.0},
    ]}
    assert parts.words_from_transcript(transcript) == [(0.0, 0.4, " a"), (5.0, 5.5, " b")]
    assert parts.words_from_transcript(None) == []
    assert parts.words_from_transcript({}) == []


def test_sentence_end_times_from_segments_and_punctuation():
    transcript = {"segments": [
        {"start": 0.0, "end": 4.0, "words": [
            {"word": " Hello", "start": 0.0, "end": 0.5},
            {"word": " world.", "start": 0.6, "end": 1.0},
            {"word": " Wait", "start": 1.2, "end": 1.5},
            {"word": " what?", "start": 1.6, "end": 2.0},
            {"word": " so", "start": 2.1, "end": 2.5},
            {"word": " done!", "start": 3.0, "end": 4.0},
        ]},
        {"start": 4.5, "end": 8.0, "words": [
            {"word": " well…", "start": 4.5, "end": 5.0},
            {"word": " and", "start": 5.2, "end": 6.0},
        ]},
    ]}
    assert parts.sentence_end_times(transcript) == [1.0, 2.0, 4.0, 5.0, 8.0]
    assert parts.sentence_end_times(None) == []


def test_cut_times_from_probs_rising_edges():
    assert parts.cut_times_from_probs([0, .9, .9, .1, .7], 10.0) == [0.1, 0.4]
    assert parts.cut_times_from_probs([.9, .1, .9], 10.0) == [0.2]   # frame 0 is no cut
    assert parts.cut_times_from_probs([], 25.0) == []
