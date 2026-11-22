"""Parts mode: cut a whole video into sequential ~P-second parts.

Pure functions, no dependencies (spec 2026-11-23-parts-mode-design.md §3). Each
cut lands one target length after the previous cut and snaps, in order of
preference, to: a sentence end within ±5 s, a sentence end within ±10 s, the
largest ≥ 0.3 s pause within ±5 s, the nearest scene cut (or the target itself)
when nobody speaks within ±5 s, and finally the nearest word end. A cut never
falls inside a word, and no part is longer than 180 s.
"""
import json
import re
from typing import Sequence

WINDOW = 5.0            # first search window around the target, seconds
WIDE_WINDOW = 10.0      # widened window for sentence ends
MIN_PAUSE = 0.3         # shortest pause between words worth cutting in
TAIL_MERGE = 20.0       # a last part shorter than this joins the previous one
MAX_PART = 180.0        # hard per-part limit (YouTube Shorts)
MIN_PART_SECONDS = 30
MAX_PART_SECONDS = 180

_EPS = 1e-6
_SENTENCE_PUNCT = (".", "!", "?", "…")
_CLOSERS = "\"'”’»)]"


def words_from_transcript(transcript: dict | None) -> list[tuple[float, float, str]]:
    """Every segment's words as ``(start, end, word)``, sorted by start."""
    out = []
    for seg in (transcript or {}).get("segments") or []:
        for w in seg.get("words") or []:
            if w.get("start") is None or w.get("end") is None:
                continue
            out.append((float(w["start"]), float(w["end"]), w.get("word", "")))
    out.sort(key=lambda w: (w[0], w[1]))
    return out


def sentence_end_times(transcript: dict | None) -> list[float]:
    """Segment ends plus the ends of words finishing with ``. ! ? …``;
    sorted, without repeats."""
    ends = set()
    for seg in (transcript or {}).get("segments") or []:
        if seg.get("end") is not None:
            ends.add(float(seg["end"]))
        for w in seg.get("words") or []:
            text = (w.get("word") or "").strip().rstrip(_CLOSERS)
            if text.endswith(_SENTENCE_PUNCT) and w.get("end") is not None:
                ends.add(float(w["end"]))
    return sorted(ends)


def cut_times_from_probs(probs: Sequence[float], fps: float,
                         threshold: float = 0.5) -> list[float]:
    """Time (``i / fps``) of every frame where the shot-boundary probability
    rises above ``threshold``; frame 0 is never a cut (TransNetV2's
    ``predictions_to_scenes`` convention)."""
    return [i / fps for i in range(1, len(probs))
            if probs[i] > threshold and not probs[i - 1] > threshold]


def _pauses(words):
    """Silent intervals ``(start, end)`` before, between and after the words;
    the last one is open-ended."""
    out, prev_end = [], 0.0
    for s, e, _ in words:
        if s > prev_end:
            out.append((prev_end, s))
        prev_end = max(prev_end, e)
    out.append((prev_end, float("inf")))
    return out


def _nearest(values, target):
    return min(values, key=lambda v: (abs(v - target), v)) if values else None


def _cut_in(lo, hi, scene_cuts, target):
    """A scene cut inside ``[lo, hi]`` (nearest the target), else the middle."""
    inside = [c for c in scene_cuts if lo - _EPS <= c <= hi + _EPS]
    return _nearest(inside, target) if inside else (lo + hi) / 2


def _choose_cut(t, prev, duration, words, pauses, sentence_ends, scene_cuts):
    """The cut for target ``t`` (spec §3.2, rules 1-5)."""
    hard_hi = min(prev + MAX_PART, duration)

    # Words that can matter anywhere in the widest window.
    near = [w for w in words if w[0] < t + WIDE_WINDOW and w[1] > t - WIDE_WINDOW]

    def inside_word(x):
        # Strictly inside: a boundary where two words touch is not inside.
        return any(s < x - _EPS and x + _EPS < e for s, e, _ in near)

    def valid(c):
        return prev < c < duration and c <= prev + MAX_PART + _EPS

    # 1-2. A sentence end within ±5 s, then ±10 s: cut in the pause after it
    # (up to the next word's start; zero-width when the words touch).
    for w in (WINDOW, WIDE_WINDOW):
        lo, hi = t - w, min(t + w, hard_hi)
        cands = []
        for se in sentence_ends:
            if not lo <= se <= hi or inside_word(se):
                continue
            nxt = min((s for s, _, _ in near if s >= se - _EPS), default=float("inf"))
            c = _cut_in(se, max(se, min(nxt, hi)), scene_cuts, t)
            if valid(c):
                cands.append(c)
        if cands:
            return _nearest(cands, t)

    lo, hi = t - WINDOW, min(t + WINDOW, hard_hi)
    speech = any(s < t + WINDOW and e > t - WINDOW for s, e, _ in words)

    # 4. Nobody speaks within ±5 s: the nearest scene cut, else the target.
    if not speech:
        c = _nearest([c for c in scene_cuts if lo <= c <= hi and valid(c)], t)
        return t if c is None else c

    # 3. The largest pause of at least 0.3 s within ±5 s.
    best = None
    for a, b in pauses:
        a, b = max(a, lo), min(b, hi)
        if b - a + _EPS < MIN_PAUSE:
            continue
        c = _cut_in(a, b, scene_cuts, t)
        if valid(c):
            key = (-(b - a), abs(c - t))
            if best is None or key < best[0]:
                best = (key, c)
    if best:
        return best[1]

    # 5. Continuous speech: the nearest word end.
    c = _nearest([e for _, e, _ in near
                  if t - WIDE_WINDOW <= e <= t + WIDE_WINDOW and valid(e)
                  and not inside_word(e)], t)
    return t if c is None else c


def plan_parts(duration: float, part_seconds: int,
               words: list[tuple[float, float, str]],
               sentence_ends: list[float], scene_cuts: list[float],
               max_parts: int = 60) -> list[tuple[float, float]]:
    """Contiguous ``(start, end)`` ranges from 0 to ``duration``, each about
    ``part_seconds`` long (spec §3). When the ``max_parts`` cap is hit the
    result is exactly ``max_parts`` ranges ending at the last cut; the rest of
    the video, however short, is not produced (no tail merge then)."""
    if not MIN_PART_SECONDS <= part_seconds <= MAX_PART_SECONDS:
        raise ValueError(f"part_seconds must be {MIN_PART_SECONDS}-{MAX_PART_SECONDS}, "
                         f"got {part_seconds}")
    duration = float(duration)
    if duration <= 0 or max_parts < 1:
        return []
    if duration < part_seconds + TAIL_MERGE and duration <= MAX_PART:
        return [(0.0, duration)]

    words = sorted(words, key=lambda w: (w[0], w[1]))
    pauses = _pauses(words)
    sentence_ends = sorted(sentence_ends)
    scene_cuts = sorted(scene_cuts)

    cuts, prev = [], 0.0
    while len(cuts) < max_parts:
        t = prev + part_seconds
        if t >= duration:
            break
        prev = _choose_cut(t, prev, duration, words, pauses, sentence_ends, scene_cuts)
        cuts.append(prev)

    if len(cuts) == max_parts:      # cap hit: drop everything after the last cut
        return list(zip([0.0] + cuts[:-1], cuts))

    # The loop ended because the next target passed the end of the video.
    ranges = list(zip([0.0] + cuts, cuts + [duration]))
    if (len(ranges) >= 2 and ranges[-1][1] - ranges[-1][0] < TAIL_MERGE
            and ranges[-1][1] - ranges[-2][0] <= MAX_PART):
        ranges[-2:] = [(ranges[-2][0], ranges[-1][1])]
    return ranges


# --- Hooks and clip dicts (spec §3.5, §4) ------------------------------------

PART_TEXT_CHARS = 1500  # transcript characters per part sent to the model

_HOOK_FIELDS = ("viral_hook_text", "title", "description")
_PART_PREFIX = re.compile(r"^\s*\d+\.\s*Part\b[:\-–]?\s*", re.IGNORECASE)

HOOKS_PROMPT_TEMPLATE = """You write hooks for a video that has been cut into {n} sequential parts \
published as a series ("1. Part", "2. Part", ...).
The video's language is "{language}". Write every hook, title and description in that \
language; do not translate the transcript.

For EVERY part below, return:
- viral_hook_text: at most 10 words with 1-2 emojis, grounded in what is said in THAT part. \
It should feel like an episode of a series, but each hook comes from its own part's content.
- title: a short title for that part.
- description: one sentence describing that part.

Never write "Part" or the part number in viral_hook_text or title; the code adds the \
"N. Part" label itself. Return one entry per part, using the part numbers given.

PARTS_JSON:
{parts_json}
"""


def part_texts(transcript: dict | None, ranges: list[tuple[float, float]],
               max_chars: int = PART_TEXT_CHARS) -> list[str]:
    """The spoken text of each ``(start, end)`` range (a word belongs to the
    range its start falls in), cut at a word boundary to ``max_chars``."""
    words = words_from_transcript(transcript)
    out = []
    for i, (start, end) in enumerate(ranges):
        last = i == len(ranges) - 1
        text = " ".join(w.strip() for s, _, w in words
                        if w.strip() and start <= s and (s < end or last and s <= end))
        if len(text) > max_chars:
            cut = text[:max_chars]
            text = cut.rsplit(" ", 1)[0] if " " in cut else cut
        out.append(text)
    return out


def build_hooks_prompt(language: str, texts: list[str]) -> str:
    """The single hooks request for every part's text (``texts[i]`` is part
    ``i + 1``)."""
    payload = [{"part": i + 1, "text": t or "(no speech)"} for i, t in enumerate(texts)]
    return HOOKS_PROMPT_TEMPLATE.format(
        n=len(texts), language=language or "unknown",
        parts_json=json.dumps(payload, ensure_ascii=False, indent=1))


def _clean(value) -> str:
    return str(value or "").strip()


def normalize_hooks(raw: dict | None, n_parts: int) -> list[dict]:
    """Exactly ``n_parts`` ``{"viral_hook_text", "title", "description"}``
    dicts, matched by the model's part number; missing parts stay empty,
    numbers outside ``1..n_parts`` are dropped and a leading "N. Part" the
    model wrote anyway is removed."""
    hooks = [dict.fromkeys(_HOOK_FIELDS, "") for _ in range(n_parts)]
    items = raw.get("parts") if isinstance(raw, dict) else None
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            n = int(item.get("part"))
        except (TypeError, ValueError):
            continue
        if not 1 <= n <= n_parts or n in seen:
            continue
        seen.add(n)
        hooks[n - 1] = {
            "viral_hook_text": _PART_PREFIX.sub("", _clean(item.get("viral_hook_text"))).strip(),
            "title": _PART_PREFIX.sub("", _clean(item.get("title"))).strip(),
            "description": _clean(item.get("description")),
        }
    return hooks


def build_part_clips(ranges: list[tuple[float, float]], hooks: list[dict]) -> list[dict]:
    """One clip dict per range in the existing clip schema, labelled
    "N. Part"; no ``predicted_score`` so the gallery keeps part order."""
    clips = []
    for i, (start, end) in enumerate(ranges):
        n = i + 1
        h = hooks[i] if i < len(hooks) else {}
        hook, title = _clean(h.get("viral_hook_text")), _clean(h.get("title"))
        description = _clean(h.get("description"))
        clips.append({
            "start": float(start),
            "end": float(end),
            "part_number": n,
            "viral_hook_text": f"{n}. Part\n{hook}" if hook else f"{n}. Part",
            "video_title_for_youtube_short": f"{n}. Part – {title}" if title else f"{n}. Part",
            "video_description_for_tiktok": description,
            "video_description_for_instagram": description,
        })
    return clips
