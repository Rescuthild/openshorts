# Part modu + parçalı yükleme — Uygulama Planı

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** OpenShorts'a, videonun tamamını sırayla ~60 sn'lik dikey "N. Part" parçalarına bölen bir mod ve Cloudflare'in 100 MB sınırını aşan parçalı dosya yüklemesi eklemek.

**Architecture:** main.py'ye `--parts SECONDS` bayrağı; Gemini'nin an seçimi yerine saf fonksiyonlardan oluşan `parts.py` bölücüsü, tek Gemini isteğiyle hook'lar, mevcut render/altyazı/hook hattı `force_strategy='WIDE'` ile. app.py `/api/process` `mode`/`part_seconds` alanlarını bayrağa çevirir; `PUT /api/uploads/{id}` `offset`/`size` ile parça parça yazar. Dashboard tüm dosyaları 50 MB parçalarla yükler ve "Part part böl" seçeneği sunar.

**Tech Stack:** Python 3.11, FastAPI, pydantic, google-genai, pytest, httpx; React + Vite; `node:test`.

**Spec:** `docs/superpowers/specs/2026-11-23-parts-mode-design.md`

## Global Constraints

- Repo: `/Users/kadri/.superset/projects/openshorts`, branch `feat/parts-mode` (base `deploy/gpu-coolify`). PR'lar **fork içinde**: `gh pr create --repo Rescuthild/openshorts --base deploy/gpu-coolify`. Upstream'e (`mutonby/openshorts`) asla PR/push yok.
- Mac'te Docker/test/derleme **yok**. Tüm testler gpu-coolify'da (`root@100.104.65.27`). Üretimdeki `/opt/openshorts` ve çalışan container'lara **dokunulmaz** (Task 8 hariç); testler `/opt/openshorts-dev` kopyasında.
- Komutlar (her test adımında bu üçü kullanılır):
  - **SYNC:** `rsync -az --delete --exclude .git --exclude node_modules /Users/kadri/.superset/projects/openshorts/ root@100.104.65.27:/opt/openshorts-dev/`
  - **PYTEST `<args>`:** `ssh root@100.104.65.27 'docker run --rm -u root -v /opt/openshorts-dev:/app -w /app openshorts-test python -m pytest -q <args>'`
  - **NODE `<cmd>`:** `ssh root@100.104.65.27 'docker run --rm -v /opt/openshorts-dev/dashboard:/d -v openshorts-dash-nm:/d/node_modules -w /d node:22-alpine sh -c "<cmd>"'`
- Test framework: **pytest** (fork'un düzeni, `tests/`); `main = pytest.importorskip("main")`, `app_module = pytest.importorskip("app")` desenleri.
- `part_seconds`: tam sayı, **30–180**, varsayılan **60**. Hiçbir parça **180 sn**'yi geçemez.
- Kesim penceresi **±5 sn**, genişlemiş **±10 sn**; boşluk eşiği **0.3 sn**; kuyruk birleştirme eşiği **20 sn**; parça üst sınırı `CLIP_MAX_PER_JOB` (**60**).
- Hook metni: `f"{n}. Part\n{hook}"`; hook boşsa `f"{n}. Part"`. Başlık: `f"{n}. Part – {title}"`; title boşsa `f"{n}. Part"`.
- Parts modunda Gemini isteği video başına **en fazla 2**: hook'lar + gömülü altyazı tespiti. Layout picker, hook grounding, early-audio seçimi, vision seçimi **çalışmaz**.
- Parça yükleme boyutu **50 MB** (`50 * 1024 * 1024`), parça başına **3** deneme. `MAX_FILE_SIZE_MB` = 2048.
- Dosya adı deseni değişmez: `{video_title}_clip_{i+1}.mp4`.
- Normal klip modu (bayrak/alan yokken) davranışı **birebir aynı** kalır.
- Dashboard'a eklenen metinler Türkçe; mevcut İngilizce metinlere dokunulmaz.
- Commit mesajları `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` ile biter. `git add -A` yok; dosyalar tek tek.

## Review Focus

1. **Dikey (9:16) kaynak** parts modunda: WIDE zorlanırsa minicik görüntü çıkar → beklenen: `source_already_fits` ise `force_strategy=None` (mevcut passthrough). Test: Task 3 `test_parts_force_strategy_vertical_source_is_none`.
2. **Sesi olmayan / hiç konuşmasız kaynak:** transkript `None` → parçalar yine çıkmalı, Gemini çağrılmamalı, hook'lar sadece "N. Part". Test: Task 3 `test_build_parts_without_transcript_makes_parts_and_no_gemini_call`.
3. **Kesintisiz konuşma** (pencerede hiç boşluk/cümle sonu yok): kesim yine bir kelime sınırına düşmeli, kelimenin içine değil. Test: Task 1 `test_continuous_speech_cuts_on_word_boundary`.
4. **Gemini'nin yanlış/eksik/fazla parça numarası döndürmesi** ya da hook içine kendisinin "N. Part" yazması: numarayla eşlenmeli, fazlalar atılmalı, eksikler boş kalmalı, baştaki "N. Part" tekrarı temizlenmeli. Test: Task 2 `test_part_hooks_maps_by_number_and_strips_part_prefix`.
5. **Restart sonrası resume:** env (`AUTO_HOOK` vb.) manifest'te yok → main.py `--parts` görünce gerekli env'i kendisi kurmalı. Test: Task 3 `test_apply_parts_env_sets_required_flags`.

---

### Task 1: Saf bölücü `parts.py`

**Files:**
- Create: `parts.py`
- Test: `tests/test_parts_split.py`

**Interfaces:**
- Produces:
  - `words_from_transcript(transcript: dict | None) -> list[tuple[float, float, str]]` — tüm segmentlerin `words`'ü (start, end, word), başlangıca göre sıralı; `None`/boş → `[]`.
  - `sentence_end_times(transcript: dict | None) -> list[float]` — her segmentin `end`'i ∪ `.`, `!`, `?`, `…` ile biten kelimelerin `end`'i; sıralı, tekrarsız.
  - `cut_times_from_probs(probs: Sequence[float], fps: float, threshold: float = 0.5) -> list[float]` — olasılığın eşiği yukarı kestiği her karenin zamanı (`i / fps`); 0. kare hariç.
  - `plan_parts(duration: float, part_seconds: int, words: list[tuple[float, float, str]], sentence_ends: list[float], scene_cuts: list[float], max_parts: int = 60) -> list[tuple[float, float]]`

- [ ] **Step 0 (tek seferlik): test ortamını kur**

Run: `ssh root@100.104.65.27 'mkdir -p /opt/openshorts-dev && printf "FROM openshorts-backend\nUSER root\nRUN /opt/venv/bin/pip install -q pytest\n" | docker build -q -t openshorts-test -'` sonra **SYNC** ve **PYTEST** `tests/test_clip_render_order.py`
Expected: mevcut testler PASS (ortam çalışıyor).

- [ ] **Step 1: Failing testleri yaz** (`tests/test_parts_split.py`; kelimeler `(s, e, w)` tuple'ları, yardımcı `_words(spec)` ile üretilir)

```python
parts = pytest.importorskip("parts")

def test_sentence_end_beats_scene_cut():
    # 0-120 sn sürekli konuşma; 0.25 sn boşluklar; 57.0'da cümle sonu, 61.0'da kelime ortasında sahne kesimi
    r = parts.plan_parts(120.0, 60, words, sentence_ends=[57.0], scene_cuts=[61.0])
    assert r[0][1] == pytest.approx(57.0 + gap/2)   # cümle sonundan sonraki boşluğun ortası

def test_scene_cut_inside_pause_after_sentence_end_is_used():
    # 59.2'de cümle sonu, 59.2-59.9 boşluk, 59.6'da sahne kesimi
    assert parts.plan_parts(...)[0][1] == pytest.approx(59.6)

def test_window_widens_to_ten_seconds():
    # ±5'te cümle sonu yok, 52.0'da var
    assert parts.plan_parts(...)[0][1] == pytest.approx(52.0 + gap/2)

def test_largest_pause_when_no_sentence_end():
    # ±10'da cümle sonu yok; 57-57.4 (0.4) ve 62-62.9 (0.9) boşluk
    assert parts.plan_parts(...)[0][1] == pytest.approx(62.45)

def test_silent_window_uses_nearest_scene_cut_else_target():
    # 50-70 arası konuşma yok, sahne kesimleri [58.0, 66.0]
    assert parts.plan_parts(...)[0][1] == pytest.approx(58.0)
    # sahne kesimi de yok
    assert parts.plan_parts(..., scene_cuts=[])[0][1] == pytest.approx(60.0)

def test_continuous_speech_cuts_on_word_boundary():
    # 0.1 sn boşluklu kesintisiz konuşma (eşik altı), cümle sonu yok
    cut = parts.plan_parts(...)[0][1]
    assert any(cut == pytest.approx(e) for (_, e, _) in words)
    assert not any(s < cut < e for (s, e, _) in words)

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

def test_max_parts_cap():
    assert len(parts.plan_parts(7200.0, 60, [], [], [])) == 60

def test_parts_are_contiguous_and_cover_video():
    r = parts.plan_parts(600.0, 60, words, ends, cuts)
    assert r[0][0] == 0.0 and r[-1][1] == 600.0
    assert all(a[1] == b[0] for a, b in zip(r, r[1:]))

def test_sentence_end_times_from_segments_and_punctuation(): ...
def test_cut_times_from_probs_rising_edges():
    assert parts.cut_times_from_probs([0, .9, .9, .1, .7], 10.0) == [0.1, 0.4]
```

- [ ] **Step 2: Çalıştır, fail olduğunu gör** — **SYNC**, **PYTEST** `tests/test_parts_split.py` → `ModuleNotFoundError`/skip değil FAIL (importorskip skip verirse `parts.py` boş dosya olarak oluşturup tekrar çalıştır).

- [ ] **Step 3: `parts.py`'yi yaz.** `plan_parts` algoritması (spec §3):

```
prev = 0.0; cuts = []
while True:
    t = prev + P
    if t >= D: break
    cut = (choose_in(sentence_end, t, 5) or choose_in(sentence_end, t, 10)
           or largest_gap_mid(t, 5, min_gap=0.3) or (no_words_in(t-5, t+5) and nearest_scene_cut(t, 5))
           or (no_words_in(t-5, t+5) and t) or nearest_word_end(t))
    # "cümle sonu" adayı: sonrasındaki boşluk (sonraki kelimenin start'ına kadar);
    #   boşlukta sahne kesimi varsa o, yoksa boşluğun ortası; hedefe en yakın aday.
    # Aday her zaman prev + P - 10 ≤ cut ≤ prev + P + 10 ve cut - prev ≤ 180.
    cuts.append(cut); prev = cut
    if len(cuts) + 1 >= max_parts: break
ranges = zip([0]+cuts, cuts+[D])
if son aralık < 20 ve (son iki aralık toplamı) <= 180: son ikisini birleştir
```
(`max_parts` sınırında son aralık `D`'ye uzatılmaz; `cuts[-1]`'den sonraki kısım atılır ve dönüş tam `max_parts` öğe olur — `test_max_parts_cap`.)

- [ ] **Step 4: Testler PASS** — **SYNC**, **PYTEST** `tests/test_parts_split.py` → tümü PASS.

- [ ] **Step 5: Commit** — `git add parts.py tests/test_parts_split.py && git commit -m "feat(parts): sequential part splitter snapping to sentence ends"`

---

### Task 2: Tek Gemini isteğiyle part hook'ları + klip dict'leri

**Files:**
- Modify: `gemini_worker.py` (şemaların yanına)
- Modify: `parts.py`
- Modify: `main.py` (`get_viral_clips` yakınına yeni fonksiyon)
- Test: `tests/test_parts_hooks.py`

**Interfaces:**
- Consumes: Task 1 `plan_parts` dönüşü `list[tuple[float, float]]`.
- Produces:
  - `gemini_worker.PartHook(BaseModel)`: `part: int`, `viral_hook_text: str`, `title: str`, `description: str = ""`; `gemini_worker.PartHooksResponse(BaseModel)`: `parts: List[PartHook]`.
  - `parts.part_texts(transcript: dict | None, ranges: list[tuple[float, float]], max_chars: int = 1500) -> list[str]` — her aralıktaki kelimelerin metni, kırpılmış.
  - `parts.build_hooks_prompt(language: str, texts: list[str]) -> str`
  - `parts.normalize_hooks(raw: dict | None, n_parts: int) -> list[dict]` — `n_parts` uzunlukta; her öğe `{"viral_hook_text", "title", "description"}` (eksikler `""`); numaraya göre eşler, aralık dışı numaraları atar, hook başındaki `"\d+\.\s*Part\b[:\-–]?\s*"` kalıbını temizler.
  - `parts.build_part_clips(ranges: list[tuple[float, float]], hooks: list[dict]) -> list[dict]` — her öğe: `start`, `end`, `part_number`, `viral_hook_text`, `video_title_for_youtube_short`, `video_description_for_tiktok`, `video_description_for_instagram` (Global Constraints'teki biçimler); **`predicted_score` yok**.
  - `main.get_part_hooks(transcript: dict | None, ranges: list[tuple[float, float]]) -> tuple[list[dict], dict]` — `(normalize_hooks(...), cost_analysis)`; hiç kelime yoksa Gemini **çağrılmaz**; her istisnada (GeminiBlockedError dahil) boş hook'lar + uyarı log'u, asla raise etmez.

- [ ] **Step 1: Failing testler** (`tests/test_parts_hooks.py`; `main._run_gemini_stage` monkeypatch, `tests/test_clip_floor.py` deseni)

```python
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

def test_get_part_hooks_survives_gemini_failure(monkeypatch):
    monkeypatch.setattr(main, "_run_gemini_stage", raising(RuntimeError("503")))
    hooks, _ = main.get_part_hooks(TRANSCRIPT, [(0, 60)])
    assert hooks == [{"viral_hook_text": "", "title": "", "description": ""}]

def test_get_part_hooks_without_words_skips_gemini(monkeypatch):
    monkeypatch.setattr(main, "_run_gemini_stage", must_not_be_called)
    hooks, _ = main.get_part_hooks(None, [(0, 60), (60, 120)])
    assert len(hooks) == 2 and all(h["viral_hook_text"] == "" for h in hooks)

def test_part_hooks_maps_by_number_and_strips_part_prefix():
    raw = {"parts": [{"part": 2, "viral_hook_text": "2. Part: Sonra 🔥", "title": "b"},
                     {"part": 9, "viral_hook_text": "fazla", "title": "x"}]}
    h = parts.normalize_hooks(raw, 2)
    assert h[0]["viral_hook_text"] == "" and h[1]["viral_hook_text"] == "Sonra 🔥"

def test_build_part_clips_formats():
    c = parts.build_part_clips([(0.0, 58.5), (58.5, 120.0)],
                               [{"viral_hook_text": "Bak 😳", "title": "Kaşık", "description": "d"},
                                {"viral_hook_text": "", "title": "", "description": ""}])
    assert c[0]["viral_hook_text"] == "1. Part\nBak 😳"
    assert c[0]["video_title_for_youtube_short"] == "1. Part – Kaşık"
    assert c[1]["viral_hook_text"] == "2. Part"
    assert c[1]["video_title_for_youtube_short"] == "2. Part"
    assert c[1]["part_number"] == 2 and "predicted_score" not in c[0]

def test_prompt_mentions_language_and_every_part():
    p = parts.build_hooks_prompt("tr", ["birinci", "ikinci"])
    assert "tr" in p and "birinci" in p and "ikinci" in p
```

- [ ] **Step 2: FAIL doğrula** — **SYNC**, **PYTEST** `tests/test_parts_hooks.py`.
- [ ] **Step 3: Uygula.** Prompt (İngilizce yazılır, model talimatı): videonun dili `language`; her parça için o parçanın metnine dayanan ≤10 kelimelik, 1–2 emojili, **videonun dilinde** hook; kısa başlık; 1 cümle açıklama; hook'a "Part"/numara yazma; seri olduğu hissedilsin ama her hook kendi içeriğinden. İstemci `get_viral_clips`'teki gibi: `genai.Client(api_key=os.getenv("GEMINI_API_KEY"), http_options=gemini_http_options())`, model `os.environ.get("GEMINI_MODEL") or 'gemini-3.1-flash-lite'`; `LLM_BASE_URL` yolu `_run_gemini_stage` içinde zaten ele alınıyor.
- [ ] **Step 4: PASS** — **SYNC**, **PYTEST** `tests/test_parts_hooks.py tests/test_parts_split.py`.
- [ ] **Step 5: Commit** — `git add gemini_worker.py parts.py main.py tests/test_parts_hooks.py && git commit -m "feat(parts): one Gemini call writes every part's hook"`

---

### Task 3: main.py `--parts` akışı

**Files:**
- Modify: `main.py` — argparse (~2930-2945), `use_early` (~3069), burned-subtitle bloğu (~3154-3167), `use_prepared` öncesi (~3190), sparse-speech ataması (~3248), seçim dalı (~3254-3263), `_process_one_clip` (~3292-3383)
- Test: `tests/test_parts_main.py`

**Interfaces:**
- Consumes: Task 1 `plan_parts`, `words_from_transcript`, `sentence_end_times`, `cut_times_from_probs`; Task 2 `get_part_hooks`, `build_part_clips`.
- Produces:
  - `main.apply_parts_env(environ: MutableMapping[str, str] = os.environ) -> None` — `AUTO_HOOK="1"`, `HOOK_GROUNDING="0"`, `PRO_MODE="off"`, `AUTO_LAYOUT="0"` (üzerine yazar).
  - `main.parts_force_strategy(width: int, height: int) -> Optional[str]` — `source_already_fits(width, height, ASPECT_RATIO)` ise `None`, değilse `"WIDE"`.
  - `main.parts_scene_cuts(output_dir: str) -> list[float]` — `.scene_probs.json`'ı okuyup `decode_probabilities` + `cut_times_from_probs`; dosya yok/bozuk → `[]`.
  - `main.build_parts(transcript: dict | None, duration: float, output_dir: str, part_seconds: int) -> dict` — `{"shorts": build_part_clips(...), "cost_analysis": ...}`.
  - CLI: `--parts SECONDS` (int).

- [ ] **Step 1: Failing testler**

```python
def test_apply_parts_env_sets_required_flags():
    env = {"AUTO_HOOK": "0", "PRO_MODE": "suggest"}
    main.apply_parts_env(env)
    assert env == {"AUTO_HOOK": "1", "HOOK_GROUNDING": "0", "PRO_MODE": "off", "AUTO_LAYOUT": "0"}

def test_parts_force_strategy_vertical_source_is_none():
    assert main.parts_force_strategy(1080, 1920) is None
    assert main.parts_force_strategy(1920, 1080) == "WIDE"

def test_build_parts_without_transcript_makes_parts_and_no_gemini_call(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_run_gemini_stage", must_not_be_called)
    out = main.build_parts(None, 150.0, str(tmp_path), 60)
    assert [c["viral_hook_text"] for c in out["shorts"]] == ["1. Part", "2. Part"]
    assert out["shorts"][-1]["end"] == 150.0

def test_build_parts_uses_scene_checkpoint(monkeypatch, tmp_path):
    # tmp_path/.scene_probs.json: fps=10, 58.0 sn'de yükselen kenar; transcript yok
    assert main.build_parts(None, 150.0, str(tmp_path), 60)["shorts"][0]["end"] == pytest.approx(58.0)

def test_parts_flag_parses():
    # argparse kuruluşu modül seviyesinde değilse: subprocess ile `python main.py --help`
    assert "--parts" in help_text
```

- [ ] **Step 2: FAIL doğrula** — **SYNC**, **PYTEST** `tests/test_parts_main.py`.
- [ ] **Step 3: Fonksiyonları yaz ve akışa bağla.** `args.parts` iken:
  - argparse sonrası hemen `apply_parts_env()`.
  - `use_early` koşuluna `and not args.parts`.
  - Gömülü altyazı bloğu: parts modunda `AUTO_CAPTIONS` kontrolünden bağımsız `burned_subtitles.detect()` çalışır; bant bulunursa `source_subtitle_band = None`, `os.environ["AUTO_CAPTIONS"] = "0"` ve `print("📝 Parts: source has burned-in subtitles — keeping them, no captions added.")`; bulunmazsa/hata → bant `None`, altyazı açık, `print("📝 Parts: no burned-in subtitles — adding captions.")`. `AUTO_CAPTIONS=0` ise (kullanıcı kapattı) detect çağrılmaz.
  - Layout picker parts modunda çağrılmaz (`AUTO_LAYOUT=0` env'i import anında okunduysa da kodda `not args.parts` ile kesin atla).
  - `scene_detection.use_prepared(...)` **öncesinde** `prepare_scenes(input_video, output_dir)`.
  - `speech_is_sparse` → `transcript = None` ataması `not args.parts` iken.
  - Seçim dalı: `elif args.parts: clips_data = build_parts(transcript, duration, output_dir, args.parts)` — `early_clips` ve vision dallarından **önce**.
  - `_process_one_clip`: `render_clip(..., force_strategy=parts_strategy)` (parts değilse `None`; `parts_strategy` kaynak boyutundan bir kez hesaplanır); `hook_grounding` ve `pro_clip` parts modunda atlanır.
- [ ] **Step 4: PASS + regresyon** — **SYNC**, **PYTEST** `tests/test_parts_main.py tests/test_parts_hooks.py tests/test_parts_split.py tests/test_clip_floor.py tests/test_clip_render_order.py`.
- [ ] **Step 5: Commit** — `git add main.py tests/test_parts_main.py && git commit -m "feat(parts): --parts flag renders the whole video as WIDE parts"`

---

### Task 4: `/api/process` `mode` ve `part_seconds`

**Files:**
- Modify: `app.py` — `process_endpoint` imzası (~6132-6154), JSON gövdesi okuma (~6167-6187), `_gen_control` sonrası (~6317-6330), `cmd.extend(["-o", ...])` yakını (~6405)
- Test: `tests/test_parts_api.py`

**Interfaces:**
- Produces: form/JSON alanları `mode: "clips" | "parts"`, `part_seconds: int (30–180)`. `mode=="parts"` → `cmd` sonuna `["--parts", str(part_seconds or 60)]`, `env["AUTO_HOOK"]="1"`, `env["HOOK_GROUNDING"]="0"`, `env["PRO_MODE"]="off"`; `layouts`/`target_clips`/`clip_min_seconds`/`clip_max_seconds` yok sayılır (env'e yazılmaz). Bilinmeyen `mode` → 400.

- [ ] **Step 1: Failing testler** (`tests/test_agent_uploads.py` deseni: `OUTPUT_DIR`/`UPLOAD_DIR`/`_media_duration_seconds` monkeypatch, iş kuyruğa girmeden yakalanır; `cmd`/`env` `app_module.jobs[job_id]` üzerinden okunur)

```python
def test_parts_mode_adds_flag_and_hook_env(...):   # url ile, mode=parts
    assert job["cmd"][-2:] == ["--parts", "60"] and job["env"]["AUTO_HOOK"] == "1"
def test_part_seconds_passes_through(...):          # part_seconds=90 → "--parts", "90"
def test_part_seconds_out_of_range_is_400(...):     # 29 ve 181 → 400
def test_unknown_mode_is_400(...):                  # mode="foo" → 400
def test_parts_mode_ignores_clip_controls(...):     # target_clips=3 → "CLIP_TARGET_MIN" not in env
def test_clip_mode_unchanged(...):                  # mode yok → "--parts" not in cmd, AUTO_HOOK env'de yok (auto_hook gönderilmediyse)
```

- [ ] **Step 2: FAIL** — **SYNC**, **PYTEST** `tests/test_parts_api.py`.
- [ ] **Step 3: Uygula** (`_gen_control(part_seconds, "part_seconds", 30, 180, integer=True)`).
- [ ] **Step 4: PASS + regresyon** — **SYNC**, **PYTEST** `tests/test_parts_api.py tests/test_agent_uploads.py`.
- [ ] **Step 5: Commit** — `git add app.py tests/test_parts_api.py && git commit -m "feat(api): mode=parts and part_seconds on /api/process"`

---

### Task 5: Parçalı `PUT /api/uploads/{id}`

**Files:**
- Modify: `app.py` — `put_upload` (~5963-5992)
- Test: `tests/test_chunked_upload.py`

**Interfaces:**
- Produces: `PUT /api/uploads/{id}?offset=N[&size=TOTAL]`.
  - `offset` yok → bugünkü davranış birebir (tüm gövde, `"wb"`, complete + süre kontrolü).
  - `offset` var: `offset == 0` → `"wb"`; değilse `"ab"`. `offset != diskteki bayt` → `409 {"detail": "expected offset <n>"}`. Kümülatif > `MAX_FILE_SIZE_MB` → dosya silinir, 413. Boş gövde → 400.
  - Yanıt: `{"upload_id", "received": <toplam bayt>, "complete": bool}`; `size` verildi ve `received == size` → `complete=True`, süre kontrolü (okunamazsa 400 ve `complete=False`), yanıta `duration_seconds` eklenir. `size` verildi ama `received != size` → 400 `"size mismatch"`.
  - `slot["bytes"]` her parçada güncellenir.

- [ ] **Step 1: Failing testler**

```python
def test_three_chunks_complete(...):        # 0, 10, 20 offset'li 10'ar bayt; son parçada size=30 → complete True, received 30
def test_wrong_offset_409(...):             # 10 bayt sonra offset=5 → 409, "expected offset 10"
def test_offset_zero_restarts(...):         # 10 bayt, sonra offset=0 ile 4 bayt → received 4
def test_no_offset_is_legacy_put(...):      # mevcut davranış: complete True, duration_seconds var
def test_cumulative_cap_413(...):           # MAX_FILE_SIZE_MB monkeypatch ile küçük → aşınca 413 ve dosya yok
def test_process_before_last_chunk_409(...):# complete olmadan /api/process upload_id → 409
def test_size_mismatch_400(...):            # received 20, size=30 → 400
```

- [ ] **Step 2: FAIL** — **SYNC**, **PYTEST** `tests/test_chunked_upload.py`.
- [ ] **Step 3: Uygula.**
- [ ] **Step 4: PASS + regresyon** — **SYNC**, **PYTEST** `tests/test_chunked_upload.py tests/test_agent_uploads.py`.
- [ ] **Step 5: Commit** — `git add app.py tests/test_chunked_upload.py && git commit -m "feat(uploads): chunked PUT with offset/size under the proxy body limit"`

---

### Task 6: Dashboard parçalı yükleme

**Files:**
- Create: `dashboard/src/lib/chunkedUpload.js`
- Create: `dashboard/src/lib/chunkedUpload.test.mjs`
- Modify: `dashboard/src/App.jsx` (~1310-1330 dosya dalı)

**Interfaces:**
- Consumes: Task 5 sözleşmesi.
- Produces: `export async function uploadInChunks(file, { fetchFn, chunkSize = 50 * 1024 * 1024, retries = 3, onProgress = () => {} }) -> Promise<string /* upload_id */>`
  - `POST /api/uploads` (JSON `{filename: file.name}`) → `upload_id`.
  - Sırayla `PUT /api/uploads/{id}?offset=N` (son parçada `&size=file.size`), gövde `file.slice(N, N+chunkSize)`.
  - Hata/ağ istisnası → aynı parçayı en fazla `retries` kez tekrar; `409` yanıtında `detail`'deki beklenen offset'ten devam (sayılır deneme). Tükenirse `Error` fırlatır (mesaj Türkçe: `"Yükleme başarısız: <detay>"`).
  - Her başarılı parçadan sonra `onProgress(received / file.size)`.
  - `fetchFn(path, init)` App.jsx'te `apiFetch` (yol göreli; `getApiUrl` apiFetch içinde).

- [ ] **Step 1: Failing testler** (`node:test` + `assert`; sahte `fetchFn`; `file` = `new Blob([...])` + `name`)

```js
test('splits into chunks with offset and final size', ...)   // 120 bayt, chunkSize 50 → offset 0,50,100; sadece sonuncuda size=120
test('retries a failed chunk then succeeds', ...)            // 2. parça bir kez 502 → toplam 4 PUT
test('resyncs on 409 expected offset', ...)                  // 409 "expected offset 50" → offset=50'den devam
test('gives up after retries', ...)                           // hep 502 → rejects /Yükleme başarısız/
test('reports progress', ...)                                 // onProgress son çağrı 1
```

- [ ] **Step 2: FAIL** — **SYNC**, **NODE** `node --test src/lib/chunkedUpload.test.mjs`.
- [ ] **Step 3: Uygula** ve App.jsx dosya dalını değiştir: `formData` yerine `const uploadId = await uploadInChunks(data.payload, { fetchFn: apiFetch, onProgress: … })`, ardından `/api/process`'e JSON (`upload_id`, `acknowledged`, `output_format`, `advanced`) — URL dalıyla aynı gövde kuruluşu. İlerleme, mevcut işlem durum metninde `"Yükleniyor %NN"` olarak gösterilir.
- [ ] **Step 4: PASS + build** — **SYNC**, **NODE** `node --test src/lib/chunkedUpload.test.mjs && npm ci --no-audit --no-fund && npm run build` → testler PASS, build hatasız.
- [ ] **Step 5: Commit** — `git add dashboard/src/lib/chunkedUpload.js dashboard/src/lib/chunkedUpload.test.mjs dashboard/src/App.jsx && git commit -m "feat(dashboard): upload files in 50 MB chunks"`

---

### Task 7: Dashboard "Part part böl" arayüzü

**Files:**
- Modify: `dashboard/src/components/MediaInput.jsx` (state ~47-69, `handleSubmit` `advanced` ~123-146, gelişmiş panel ~309-409)
- Modify: `dashboard/src/App.jsx` (`advanced` eşlemesi ~1282-1298)
- Modify: `dashboard/src/components/ResultCard.jsx` (başlık ~912-914, indirme adı ~179)

**Interfaces:**
- Consumes: Task 4 alanları.
- Produces: `advanced.mode` (`'parts'` ya da gönderilmez), `advanced.part_seconds` (sayı). Klip dict'inde `part_number` (Task 2).

- [ ] **Step 1: MediaInput'a ekle:** onay kutusu etiketi **"Part part böl"**, açıklama **"Videonun tamamı sırayla parçalara bölünür (1. Part, 2. Part…)"**; açıkken sayı alanı **"Parça süresi (sn)"** (min 30, max 180, varsayılan 60). Açıkken klip sayısı/min/max ve yerleşim seçimi gizli; hook onay kutusu işaretli ve devre dışı, altına **"Part modunda hook her zaman açık"**.
- [ ] **Step 2: App.jsx eşlemesi:** `mode: data.partsMode ? 'parts' : null`, `part_seconds: data.partsMode ? (data.partSeconds || 60) : null`; parts modunda `target_clips`/`clip_min_seconds`/`clip_max_seconds`/`layouts` `null`, `auto_hook: '1'`.
- [ ] **Step 3: ResultCard:** `clip.part_number` varsa başlık `` `${clip.part_number}. Part` ``, indirme adı `` `part-${clip.part_number}.mp4` ``; yoksa mevcut metinler.
- [ ] **Step 4: Build** — **SYNC**, **NODE** `npm run build && node --test src/lib/chunkedUpload.test.mjs` → hatasız.
- [ ] **Step 5: Commit** — `git add dashboard/src/components/MediaInput.jsx dashboard/src/App.jsx dashboard/src/components/ResultCard.jsx && git commit -m "feat(dashboard): parts mode toggle and part labels"`

---

### Task 8: PR, sunucuya geçiş ve uçtan uca doğrulama

**Files:** yok (dağıtım).

- [ ] **Step 1: Tüm test takımı** — **SYNC**, **PYTEST** `tests/` → yeni testler PASS; mevcut testlerde yeni FAIL yok (önceden başarısız olanlar `deploy/gpu-coolify` üzerinde aynı komutla karşılaştırılır).
- [ ] **Step 2: Push + PR** — `git push origin feat/parts-mode` ve `gh pr create --repo Rescuthild/openshorts --base deploy/gpu-coolify --head feat/parts-mode`. Merge kullanıcı onayıyla.
- [ ] **Step 3: Sunucuyu fork'a geçir (kullanıcı onayından sonra, kuyruk boşken).** `/opt/openshorts`: `git remote set-url origin https://github.com/Rescuthild/openshorts.git`, `git remote add upstream https://github.com/mutonby/openshorts.git`, yerel yamalar `git stash`'e alınır, `git fetch origin && git checkout -B deploy/gpu-coolify origin/deploy/gpu-coolify`; `.env`'e `OPENSHORTS_BIND=100.104.65.27`. Kuyruk boş mu: backend container'ında `main.py` süreci yok (`/proc/*/cmdline` taraması).
- [ ] **Step 4: Build + restart** — `docker compose -f compose.server.yml build backend frontend && docker compose -f compose.server.yml up -d` (arka planda; build ~10–60 dk). Doğrula: `docker ps` sağlıklı, `curl -s -o /dev/null -w "%{http_code}" -u … https://shorts.saviordigital.com` 200 (basic auth'u kullanıcı yapar), `http://100.104.65.27:5175` açılıyor.
- [ ] **Step 5: Uçtan uca (kullanıcı tarayıcıdan, kendi Gemini anahtarıyla):** (a) ~10 dk'lık, 100 MB üstü bir dosya "Part part böl" ile; (b) bir YouTube linki. Log'larda kontrol: tek `PartHooksResponse` isteği + en fazla bir burned-subtitle isteği, `🎯 Framing override: every scene -> WIDE`, parça sayısı ≈ süre/60, hiçbir parça > 180 sn. Kullanıcıyla birlikte: kesimler cümle ortasında değil, "N. Part"+hook üst bulanık bantta, altyazı alt bantta, galeri 1-2-3 sıralı.
