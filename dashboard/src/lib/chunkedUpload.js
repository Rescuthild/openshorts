// Chunked upload of a local file: POST /api/uploads reserves a slot, then the
// file goes up in sequential PUT /api/uploads/{id}?offset=N chunks (the last
// one carries &size=TOTAL, which closes the upload). Cloudflare refuses any
// request body over 100 MB, so a single multipart POST cannot carry a 500 MB
// source; 50 MB chunks stay well under that.
//
// `fetchFn(path, init)` is injected (apiFetch in the app, a fake in the tests),
// so this module imports nothing from the app and runs under plain node.

const DEFAULT_CHUNK_SIZE = 50 * 1024 * 1024;

const defaultSleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

const uploadError = (detail) => new Error(`Yükleme başarısız: ${detail}`);

// The server's own explanation: FastAPI's {"detail": "..."}, else the body text.
async function readDetail(res) {
  const text = await res.text().catch(() => '');
  try {
    const d = JSON.parse(text).detail;
    if (typeof d === 'string' && d) return d;
  } catch (_) { /* not JSON */ }
  return text.trim().slice(0, 300) || `HTTP ${res.status}`;
}

// Worth sending again: the proxy or server had a bad moment, or another write
// on the slot was still running (409 "upload in progress"). 400 / 404 / 413
// and the like answer the same way every time.
const isRetryableStatus = (status) => status >= 500 || status === 408 || status === 429 || status === 409;

/**
 * Upload `file` in chunks and resolve with its upload_id, ready for
 * POST /api/process {upload_id}. Throws Error("Yükleme başarısız: <detail>")
 * when the server refuses or a chunk keeps failing past `retries`. An error
 * thrown by fetchFn other than a network failure (TypeError), e.g. apiFetch's
 * QuotaError, propagates unchanged.
 */
export async function uploadInChunks(file, {
  fetchFn,
  chunkSize = DEFAULT_CHUNK_SIZE,
  retries = 3,
  onProgress = () => {},
  sleep = defaultSleep,
} = {}) {
  const created = await fetchFn('/api/uploads', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename: file.name || 'video.mp4' }),
  });
  if (!created.ok) throw uploadError(await readDetail(created));
  const { upload_id: uploadId } = await created.json();
  if (!uploadId) throw uploadError('upload_id missing');

  try {
    return await sendChunks(file, uploadId, { fetchFn, chunkSize, retries, onProgress, sleep });
  } catch (e) {
    // Drop the half-filled slot: each open one counts against the per-user
    // cap until it expires. Best effort; the expiry sweep is the backstop.
    try {
      await fetchFn(`/api/uploads/${encodeURIComponent(uploadId)}`, { method: 'DELETE' });
    } catch (_) { /* ignore */ }
    throw e;
  }
}

async function sendChunks(file, uploadId, { fetchFn, chunkSize, retries, onProgress, sleep }) {
  const total = file.size;
  let offset = 0;
  let failures = 0; // failed attempts on the current chunk; a success resets it

  // A failed attempt: give up once the retries are spent, else (maybe) wait.
  const failed = async (detail, { wait = true } = {}) => {
    failures += 1;
    if (failures > retries) throw uploadError(detail);
    if (wait) await sleep(1000 * failures);
  };

  for (;;) {
    const end = Math.min(offset + chunkSize, total);
    const last = end >= total;
    const path = `/api/uploads/${encodeURIComponent(uploadId)}?offset=${offset}${last ? `&size=${total}` : ''}`;

    let res;
    try {
      res = await fetchFn(path, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: file.slice(offset, end),
      });
    } catch (e) {
      if (!(e instanceof TypeError)) throw e; // fetch reports network failures as TypeError
      await failed(e.message || 'network error');
      continue;
    }

    if (res.ok) {
      const body = await res.json().catch(() => ({}));
      offset = Number.isInteger(body.received) ? body.received : end;
      failures = 0;
      onProgress(total ? offset / total : 1);
      if (last) return uploadId;
      continue;
    }

    const detail = await readDetail(res);
    if (!isRetryableStatus(res.status)) throw uploadError(detail);

    // 409 "expected offset <n>": the server holds n bytes, go on from there.
    // Any other 409 ("upload in progress") resends the same offset after a wait.
    const expected = res.status === 409 ? /^expected offset (\d+)$/.exec(detail) : null;
    if (!expected) {
      await failed(detail);
      continue;
    }
    const n = Number(expected[1]);
    if (n > total) throw uploadError(detail);
    await failed(detail, { wait: false });
    offset = n;
    if (n === total) {
      // Only the last chunk brings the file to its full size, and that chunk
      // carried &size, so the server completed the upload: its answer was lost.
      onProgress(1);
      return uploadId;
    }
  }
}
