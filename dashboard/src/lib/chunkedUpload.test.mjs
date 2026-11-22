// Run: node --test src/lib/chunkedUpload.test.mjs (no test framework, no deps).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { uploadInChunks } from './chunkedUpload.js';

const makeFile = (size, name = 'video.mp4') => {
  const file = new Blob([new Uint8Array(size)]);
  file.name = name;
  return file;
};

const json = (status, body) => new Response(JSON.stringify(body), {
  status, headers: { 'Content-Type': 'application/json' },
});

const noSleep = async () => {};

// A fake server: POST /api/uploads hands out "u1"; every PUT goes through
// `onPut(call)`, which returns a Response (or throws). Every call is recorded.
function fakeServer(onPut) {
  const calls = [];
  const fetchFn = async (path, init = {}) => {
    const call = { path, init };
    calls.push(call);
    if (path === '/api/uploads') return json(200, { upload_id: 'u1', upload_url: 'http://internal:8000/api/uploads/u1' });
    const url = new URL(path, 'http://x');
    call.offset = Number(url.searchParams.get('offset'));
    call.size = url.searchParams.get('size');
    call.bytes = init.body ? init.body.size : 0;
    return onPut(call, calls.filter((c) => c.path !== '/api/uploads'));
  };
  return { fetchFn, calls, puts: () => calls.filter((c) => c.init.method === 'PUT') };
}

// A well-behaved PUT: accepts the chunk and reports what is now on disk.
const okPut = (call) => json(200, { upload_id: 'u1', received: call.offset + call.bytes, complete: call.size != null });

test('splits into chunks with offset and final size', async () => {
  const srv = fakeServer(okPut);
  const id = await uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep });
  assert.equal(id, 'u1');
  const create = srv.calls[0];
  assert.equal(create.path, '/api/uploads');
  assert.equal(create.init.method, 'POST');
  assert.deepEqual(JSON.parse(create.init.body), { filename: 'video.mp4' });
  const puts = srv.puts();
  assert.deepEqual(puts.map((c) => c.offset), [0, 50, 100]);
  assert.deepEqual(puts.map((c) => c.bytes), [50, 50, 20]);
  assert.deepEqual(puts.map((c) => c.size), [null, null, '120']);
  assert.ok(puts.every((c) => c.init.method === 'PUT' && c.path.startsWith('/api/uploads/u1?')));
});

test('retries a failed chunk then succeeds', async () => {
  let failed = false;
  const srv = fakeServer((call) => {
    if (call.offset === 50 && !failed) { failed = true; return json(502, { detail: 'bad gateway' }); }
    return okPut(call);
  });
  await uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep });
  assert.deepEqual(srv.puts().map((c) => c.offset), [0, 50, 50, 100]);
});

test('resyncs on 409 expected offset', async () => {
  // The server already holds 50 bytes the client thinks it still has to send
  // (a response lost after the write): the second attempt at 0 is told to go on from 50.
  let first = true;
  const srv = fakeServer((call) => {
    if (call.offset === 0 && first) { first = false; return json(502, { detail: 'lost' }); }
    if (call.offset === 0) return json(409, { detail: 'expected offset 50' });
    return okPut(call);
  });
  await uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep });
  assert.deepEqual(srv.puts().map((c) => c.offset), [0, 0, 50, 100]);
});

test('gives up after retries', async () => {
  const srv = fakeServer(() => json(502, { detail: 'bad gateway' }));
  await assert.rejects(
    uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep }),
    /Yükleme başarısız: bad gateway/,
  );
  // One try plus three retries, all on the first chunk.
  assert.deepEqual(srv.puts().map((c) => c.offset), [0, 0, 0, 0]);
});

test('reports progress', async () => {
  const seen = [];
  const srv = fakeServer(okPut);
  await uploadInChunks(makeFile(120), {
    fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep, onProgress: (p) => seen.push(p),
  });
  assert.deepEqual(seen, [50 / 120, 100 / 120, 1]);
  assert.equal(seen.at(-1), 1);
});

test('409 upload in progress waits and resends the same offset', async () => {
  const delays = [];
  let busy = 2;
  const srv = fakeServer((call) => {
    if (call.offset === 50 && busy > 0) { busy -= 1; return json(409, { detail: 'upload in progress' }); }
    return okPut(call);
  });
  await uploadInChunks(makeFile(120), {
    fetchFn: srv.fetchFn, chunkSize: 50, sleep: async (ms) => { delays.push(ms); },
  });
  // Never restarts from 0 or sends NaN: same offset again, after a growing wait.
  assert.deepEqual(srv.puts().map((c) => c.offset), [0, 50, 50, 50, 100]);
  assert.equal(delays.length, 2);
  assert.ok(delays[0] > 0 && delays[1] > delays[0]);
});

test('413 is not retried', async () => {
  const srv = fakeServer((call) => (call.offset === 50
    ? json(413, { detail: 'File too large. Max size 2048MB' }) : okPut(call)));
  await assert.rejects(
    uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep }),
    /Yükleme başarısız: File too large\. Max size 2048MB/,
  );
  assert.deepEqual(srv.puts().map((c) => c.offset), [0, 50]);
});

test('404 expired slot is not retried', async () => {
  const srv = fakeServer(() => json(404, { detail: 'Unknown or expired upload_id' }));
  await assert.rejects(
    uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep }),
    /Yükleme başarısız: Unknown or expired upload_id/,
  );
  assert.equal(srv.puts().length, 1);
});

test('network exceptions are retried', async () => {
  let thrown = false;
  const srv = fakeServer((call) => {
    if (call.offset === 100 && !thrown) { thrown = true; throw new TypeError('Failed to fetch'); }
    return okPut(call);
  });
  const id = await uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep });
  assert.equal(id, 'u1');
  assert.deepEqual(srv.puts().map((c) => c.offset), [0, 50, 100, 100]);
});

test('a non-network error from fetchFn (e.g. QuotaError) propagates unchanged', async () => {
  class QuotaError extends Error {}
  const err = new QuotaError('quota_exceeded');
  const srv = fakeServer(() => { throw err; });
  await assert.rejects(
    uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep }),
    (e) => e === err,
  );
  assert.equal(srv.puts().length, 1);
});

test('a failed slot request fails with the server detail', async () => {
  const fetchFn = async () => json(429, { detail: 'You have 5 unused upload slots open' });
  await assert.rejects(
    uploadInChunks(makeFile(120), { fetchFn, chunkSize: 50, sleep: noSleep }),
    /Yükleme başarısız: You have 5 unused upload slots open/,
  );
});

test('409 expected offset equal to the file size means the server has it all', async () => {
  // The last chunk's response was lost after the server completed the file.
  let lost = true;
  const srv = fakeServer((call) => {
    if (call.offset === 100 && lost) { lost = false; return json(502, { detail: 'lost' }); }
    if (call.offset === 100) return json(409, { detail: 'expected offset 120' });
    return okPut(call);
  });
  const seen = [];
  const id = await uploadInChunks(makeFile(120), {
    fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep, onProgress: (p) => seen.push(p),
  });
  assert.equal(id, 'u1');
  assert.deepEqual(srv.puts().map((c) => c.offset), [0, 50, 100, 100]);
  assert.equal(seen.at(-1), 1);
});

test('default chunk size is 50 MB', async () => {
  const srv = fakeServer(okPut);
  const size = 50 * 1024 * 1024 + 10;
  await uploadInChunks(makeFile(size), { fetchFn: srv.fetchFn, sleep: noSleep });
  const puts = srv.puts();
  assert.deepEqual(puts.map((c) => c.offset), [0, 50 * 1024 * 1024]);
  assert.deepEqual(puts.map((c) => c.size), [null, String(size)]);
});

test('a failed upload drops its slot (best effort)', async () => {
  // Each open slot counts against the per-user cap until it expires.
  const srv = fakeServer((call) => {
    if (call.init.method === 'DELETE') throw new TypeError('Failed to fetch');
    return json(413, { detail: 'File too large. Max size 2048MB' });
  });
  await assert.rejects(
    uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep }),
    /Yükleme başarısız: File too large/,
  );
  const last = srv.calls.at(-1);
  assert.equal(last.init.method, 'DELETE');
  assert.equal(last.path, '/api/uploads/u1');
});

test('a successful upload does not drop its slot', async () => {
  const srv = fakeServer(okPut);
  await uploadInChunks(makeFile(120), { fetchFn: srv.fetchFn, chunkSize: 50, sleep: noSleep });
  assert.ok(srv.calls.every((c) => c.init.method !== 'DELETE'));
});
