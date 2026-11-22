// Run: node --test src/lib/advancedParams.test.mjs (no test framework, no deps).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { clampPartSeconds, sentAdvancedParams } from './advancedParams.js';

// The mapping App.jsx used before parts mode, verbatim: a request without
// parts mode must come out byte-identical to it.
const before = (data) => {
  const advanced = {
    target_clips: data.targetClips || null,
    clip_min_seconds: data.clipMinSeconds || null,
    clip_max_seconds: data.clipMaxSeconds || null,
    auto_hook: data.autoHook ? '1' : '0',
    auto_hook_style: data.autoHook ? (data.autoHookStyle || 'pill') : null,
    layouts: data.layout && data.layout !== 'auto' ? data.layout : null,
    max_minutes: data.maxMinutes || null,
    source_start: data.sourceStart ?? null,
    source_end: data.sourceEnd ?? null,
  };
  return Object.fromEntries(Object.entries(advanced).filter(([, v]) => v != null));
};

const nonParts = [
  {},
  { autoHook: true, autoHookStyle: 'pill', layout: 'auto' },
  { autoHook: false, autoHookStyle: 'dark', layout: 'split' },
  { targetClips: '5', clipMinSeconds: '20', clipMaxSeconds: '45', autoHook: true, autoHookStyle: 'yellow', layout: 'none', sourceStart: 0, sourceEnd: 300 },
  { autoHook: true, layout: 'screencast', maxMinutes: 10, sourceStart: null, sourceEnd: null },
  { partsMode: false, partSeconds: 90, targetClips: '3', autoHook: true, autoHookStyle: 'red', layout: 'split' },
];

test('without parts mode the request is byte-identical to the old mapping', () => {
  for (const data of nonParts) {
    const out = sentAdvancedParams(data);
    assert.equal(JSON.stringify(out), JSON.stringify(before(data)), JSON.stringify(data));
    assert.equal('mode' in out, false);
    assert.equal('part_seconds' in out, false);
  }
});

test('parts mode sends mode and part_seconds and drops clip count, lengths and layout', () => {
  const out = sentAdvancedParams({
    partsMode: true, partSeconds: '90',
    targetClips: '5', clipMinSeconds: '20', clipMaxSeconds: '45',
    autoHook: true, autoHookStyle: 'dark', layout: 'split',
    sourceStart: 10, sourceEnd: 400,
  });
  assert.deepEqual(out, {
    mode: 'parts', part_seconds: 90,
    auto_hook: '1', auto_hook_style: 'dark',
    source_start: 10, source_end: 400,
  });
});

test('parts mode forces the hook on even when the user had turned it off', () => {
  const out = sentAdvancedParams({ partsMode: true, partSeconds: 60, autoHook: false, autoHookStyle: 'outline' });
  assert.equal(out.auto_hook, '1');
  assert.equal(out.auto_hook_style, 'outline');
  assert.equal(sentAdvancedParams({ partsMode: true, autoHook: false }).auto_hook_style, 'pill');
});

test('parts mode keeps max_minutes from the quota wall', () => {
  assert.equal(sentAdvancedParams({ partsMode: true, maxMinutes: 10 }).max_minutes, 10);
});

test('part length: blank means 60, clamped to 30..180, whole seconds', () => {
  assert.equal(clampPartSeconds(''), 60);
  assert.equal(clampPartSeconds(undefined), 60);
  assert.equal(clampPartSeconds(null), 60);
  assert.equal(clampPartSeconds('abc'), 60);
  assert.equal(clampPartSeconds('10'), 30);
  assert.equal(clampPartSeconds('0'), 30);
  assert.equal(clampPartSeconds('-5'), 30);
  assert.equal(clampPartSeconds('500'), 180);
  assert.equal(clampPartSeconds('90.6'), 91);
  assert.equal(clampPartSeconds(' 45 '), 45);
  assert.equal(clampPartSeconds(120), 120);
  assert.equal(sentAdvancedParams({ partsMode: true, partSeconds: '' }).part_seconds, 60);
  assert.equal(sentAdvancedParams({ partsMode: true }).part_seconds, 60);
  assert.equal(sentAdvancedParams({ partsMode: true, partSeconds: 1000 }).part_seconds, 180);
});
