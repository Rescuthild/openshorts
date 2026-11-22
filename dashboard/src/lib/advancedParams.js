// The advanced generation controls of a /api/process request, from the data
// MediaInput hands up. A null value means "not sent": callers drop nulls, so a
// request that set nothing stays byte-identical to the pre-feature one.

export const PART_SECONDS_MIN = 30;
export const PART_SECONDS_MAX = 180;
export const PART_SECONDS_DEFAULT = 60;

// Part length typed by the user -> a whole number of seconds in 30..180.
// Blank or unreadable means the default (60).
export function clampPartSeconds(value) {
  const n = typeof value === 'number' ? value : parseFloat(String(value ?? '').trim());
  if (!Number.isFinite(n)) return PART_SECONDS_DEFAULT;
  return Math.min(PART_SECONDS_MAX, Math.max(PART_SECONDS_MIN, Math.round(n)));
}

export function advancedParams(data) {
  // Parts mode cuts the whole video in order: the server ignores clip
  // count/length and layout and always burns the hook, so none of those travel.
  const parts = !!data.partsMode;
  const hook = parts || !!data.autoHook;
  return {
    mode: parts ? 'parts' : null,
    part_seconds: parts ? clampPartSeconds(data.partSeconds) : null,
    target_clips: parts ? null : (data.targetClips || null),
    clip_min_seconds: parts ? null : (data.clipMinSeconds || null),
    clip_max_seconds: parts ? null : (data.clipMaxSeconds || null),
    // Sent explicitly both ways: absent means off for raw API callers,
    // but the dashboard always states the user's choice.
    auto_hook: hook ? '1' : '0',
    auto_hook_style: hook ? (data.autoHookStyle || 'pill') : null,
    // 'auto' is the server default, so only a deliberate choice travels.
    layouts: !parts && data.layout && data.layout !== 'auto' ? data.layout : null,
    // Set when the user took the quota wall's "clip the first N minutes"
    // offer: the server reserves N minutes and cuts the source to them.
    max_minutes: data.maxMinutes || null,
    // The stretch picked under the cost line (seconds); absent = whole video.
    source_start: data.sourceStart ?? null,
    source_end: data.sourceEnd ?? null,
  };
}

// The non-null entries, ready to spread into a JSON body.
export function sentAdvancedParams(data) {
  return Object.fromEntries(Object.entries(advancedParams(data)).filter(([, v]) => v != null));
}
