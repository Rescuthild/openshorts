# Part modu ("1. Part", "2. Part" …) ve parçalı yükleme — tasarım

Tarih: 2026-11-23 · Branch: `feat/parts-mode` (base: `deploy/gpu-coolify`, fork `Rescuthild/openshorts`)

## Amaç ve neden

Bir arkadaşımız OpenShorts'u (shorts.saviordigital.com, gpu-coolify'da self-host) film/dizi
sahneleri ve "how to craft / how to make" gibi ~10 dakikalık **yatay** videolardan seri
shorts üretmek için kullanacak. Mevcut akış Gemini'ye "en viral anları" seçtiriyor; o
isteği karşılamıyor. İstenen: videonun **tamamı, sırayla**, ~1 dakikalık dikey parçalara
bölünsün; her parçanın başında bir hook ve "N. Part" yazsın, parça boyunca videonun kendi
dilinde altyazı olsun. İzleyici TikTok/Shorts'ta seriyi sırayla takip eder.

Video ya **bilgisayardan dosya** olarak ya da **YouTube linki** ile gelecek. Dosyalar
150–500 MB; Cloudflare (ücretsiz plan) 100 MB'tan büyük istek gövdesini reddediyor ve
dashboard bugün dosyayı tek multipart POST ile gönderiyor → büyük dosya yüklenemiyor.
Bu yüzden **parçalı yükleme** bu işin parçası.

## Hedefler

1. Dashboard'da "Part part böl" seçeneği + parça süresi (varsayılan 60 sn).
2. Video baştan sona ardışık parçalara bölünür; kesimler doğal bir noktaya (sahne geçişi
   ya da konuşma arası) oturur, cümle ortasında kesilmez.
3. Her parça 1080x1920, **WIDE** yerleşim: 16:9 karenin tamamı tam genişlikte ortada,
   üst/alt boşluk aynı görüntünün bulanık hâli. Yüz takibi/kırpma yok.
4. İlk ~5 sn: üstte "N. Part" satırı + altında o parçaya özel kısa hook (videonun dilinde,
   emojili, en fazla ~10 kelime). Tüm parçaların hook'ları **tek Gemini isteğinde**.
5. Parça boyunca karaoke altyazı, videonun kendi dilinde (Whisper transkripti; çeviri yok).
6. Parçalar normal klip galerisinde 1, 2, 3… sırasıyla, "N. Part" etiketiyle görünür;
   mevcut altyazı/hook düzenleyici ve "hepsini indir" çalışır.
7. Dashboard'dan 2 GB'a kadar dosya Cloudflare üzerinden yüklenebilir (parçalı yükleme).

## Hedef olmayanlar

- Yapay zekânın "iyi" parçaları seçmesi/atlaması — her saniye bir parçaya girer.
- Altyazı/hook çevirisi (video hangi dildeyse o dil).
- Parça başına farklı yerleşim (TRACK/SPLIT vb.) — hepsi WIDE.
- Tarayıcı kapanıp açıldıktan sonra yüklemeye kaldığı yerden devam.
- Cloud/billing modunun kota ve filigran kuralları (self-host'ta kapalı; dokunulmaz).
- Otomatik sosyal medya paylaşımı.

## Anti-hedefler (çalışsa bile başarısızlık sayılır)

- Parçaların cümle ortasında kesilmesi.
- Gemini 503/kota hatası yüzünden **hiç parça çıkmaması** — hook üretilemezse parçalar
  yine çıkmalı (yalnızca "N. Part" yazısıyla).
- Video başına **ikiden fazla** Gemini isteği harcanması (ücretsiz katman: 3.5 Flash Lite
  15 RPM / 500 RPD). İzin verilen iki istek: hook'lar (§4) ve gömülü altyazı tespiti (§5).
  Layout picker, hook grounding, early-audio seçimi gibi ek çağrılar bu modda çalışmamalı.
- Görüntünün yanlardan kırpılması (film/el işi videosunda kenardaki aksiyon kaybolur).
- Normal klip modunun davranışının değişmesi.

## Kısıtlar

- Kod fork'ta (`Rescuthild/openshorts`), upstream'den düzenli `git merge upstream/main`
  alınacak → değişiklikler mümkün olduğunca **ekleme** şeklinde, mevcut fonksiyonların
  içine dar dallar halinde.
- Testler **pytest** (fork'un kendi düzeni; `tests/`), **gpu-coolify'da** koşar (Mac'te
  Docker/yerel servis yok).
- İş ayarları `cmd`'de bayrak olarak taşınmalı: resume manifest'i env'i saklamıyor
  (app.py:2378-2411); `--parts` bayrağı remote worker'a gönderimi de kendiliğinden kapatır
  (remote_worker.py:126-129) — istenen davranış.
- Cloudflare: tek istek gövdesi < 100 MB.
- Sunucuda tek iş slotu (`MAX_CONCURRENT_JOBS=1`), GPU whisperx ile paylaşımlı.

## Yaklaşımlar

| | Yaklaşım | Artı | Eksi |
|---|---|---|---|
| **A (seçildi)** | OpenShorts'a yeni mod: main.py'de `get_viral_clips` yerine deterministik bölücü, gerisi mevcut render/altyazı/hook hattı | İşin çoğu hazır (WIDE yerleşim, altyazı, çok satırlı hook, galeri, düzenleyiciler); arkadaş tek arayüz kullanır | Upstream merge'lerinde çatışma riski; main.py büyük |
| B | Ayrı küçük servis + sayfa (ffmpeg ile böl, altyazı/yazı bas) | OpenShorts'a dokunmaz | Whisper, altyazı stili, hook çizimi baştan; iki ayrı site; düzenleme araçları yok |
| — | Mevcut klip modunu ayarlarla kullanmak | Kod yok | Yapay zekâ an seçer, videonun tamamını sırayla kapsamaz → isteği karşılamaz |

Kullanıcı **A**'yı seçti. Yükleme için: parçalı yükleme (seçildi) / Cloudflare'siz ayrı
yükleme alt alan adı (sunucu IP'sini açar, ayrı TLS+auth gerekir — reddedildi) / sadece
YouTube linki veya Tailscale (arkadaşa yük — reddedildi).

## Tasarım

### 1. API ve iş başlatma (app.py)

- `/api/process`'e iki alan (form + JSON gövdesi, mevcut çift okuma düzenine uygun):
  `mode` (`"clips"` varsayılan | `"parts"`) ve `part_seconds` (tam sayı, 30–180,
  varsayılan 60; `_gen_control` ile doğrulanır, aralık dışı → 400).
- `mode=="parts"` iken:
  - `cmd += ["--parts", str(part_seconds)]`.
  - `env["AUTO_HOOK"] = "1"` zorlanır (hook/Part yazısı bu modun parçası).
  - `env["HOOK_GROUNDING"] = "0"`, `env["PRO_MODE"] = "off"` (ek Gemini/yüz çağrısı yok).
    *(Not: env resume'da kaybolur; bu yüzden main.py `--parts` görünce bunları kendisi de
    uygular — bkz. §2. Env ataması sadece ilk çalıştırma için yedek.)*
  - `layouts` yok sayılır; `AUTO_LAYOUT` / `SPLIT_LAYOUT` vb. açılmaz.
  - `target_clips`, `clip_min_seconds`, `clip_max_seconds` yok sayılır.
  - `captions` alanı aynen saygı görür (varsayılan açık).
- Normal mod (`mode` yok ya da `"clips"`): davranış birebir aynı.

### 2. main.py — `--parts SECONDS`

- argparse'a `--parts` (int). Verildiğinde iş başında:
  `os.environ.setdefault` değil **doğrudan** `AUTO_HOOK=1`, `HOOK_GROUNDING=0`,
  `PRO_MODE=off`, `AUTO_LAYOUT=0` (resume'da da doğru çalışsın diye main.py kendi ayarlar).
- **Early-audio yolu kapalı:** `use_early` koşuluna `and not args.parts` (URL indirilirken
  `get_viral_clips` çağrılmasın, main.py:3069).
- **Bölme noktası:** main.py:3254-3263'teki seçim dalına `elif args.parts:` eklenir →
  `clips_data = build_parts(transcript, duration, input_video, args.parts)`; dönüş mevcut
  sözleşme: `{"shorts": [...], "cost_analysis": {...}}`. 3265 sonrası (metadata yazımı,
  `_process_one_clip`, `CLIP_READY`, `_finish_job`) aynen kullanılır.
- Vision yolu (`speech_is_sparse` / ses yok): parts modunda **atlanır**. main.py:3248'deki
  `speech_is_sparse` → `transcript = None` ataması da parts modunda **yapılmaz**: az
  konuşmalı "how to craft" videolarında var olan cümleler altyazılanır ve hook prompt'una
  girer. Hiç konuşma olmayan parçalar için Gemini'ye metin gönderilmez; tüm videoda hiç
  kelime yoksa hook isteği **hiç yapılmaz** ve parçalar sadece "N. Part" ile çıkar.
- `layout_picker`, `burned_subtitles` gibi modüller env'i import anında okuyor; bu yüzden
  parts modu bu çağrıları env üzerinden değil, `args.parts` kontrolüyle doğrudan atlar.

### 3. Bölücü — `build_parts` (yeni modül `parts.py`, saf fonksiyonlar test edilebilir)

Girdi: süre `D`, hedef `P` (sn), kelime zaman damgaları (transkriptten), sahne kesimleri.

1. **Sahne kesimleri:** `scene_detection.source_probabilities(input_video)` tüm kaynakta
   bir kez çalışır; sonuç `prepare_scenes` yolundaki gibi `.scene_probs.json` olarak
   yazılır ki her parçanın render'ı TransNetV2'yi tekrar koşmasın (main.py:2261-2265,
   `use_prepared` main.py:3190'dan **önce**). Kesim zamanları
   `predictions_to_scenes` ile çıkarılır. Hata olursa sahne kesimi olmadan devam edilir.
2. Hedefler bir önceki kesimden `P` sn sonrası (`t_k = önceki_kesim + P`, `t_k < D`).
   **Cümle bütünlüğü sahne kesiminden önce gelir** (film/dizide plan, cümle ortasında
   sık değişir). *Cümle sonu* = Whisper segment sonu **ya da** `. ! ? …` ile biten
   kelimenin `end`'i. Kesim noktası olarak kullanılan an, iki kelime arasındaki boşluğun
   içinde olmalı (kelimenin içine düşemez). Öncelik sırası:
   1. `[t_k−5, t_k+5]`'te bir **cümle sonu**; ardından gelen boşlukta bir sahne kesimi
      varsa kesim o sahne kesimine, yoksa boşluğun ortasına konur. Birden çok aday
      varsa hedefe en yakını.
   2. Yoksa pencere `[t_k−10, t_k+10]`'a genişler, aynı kural.
   3. Yoksa `[t_k−5, t_k+5]`'te ≥ 0.3 sn'lik en büyük **kelime arası boşluğun** ortası
      (boşlukta sahne kesimi varsa o).
   4. Yoksa `[t_k−5, t_k+5]`'te konuşma yoksa (müzik/sessiz bölüm): hedefe en yakın
      **sahne kesimi**, o da yoksa `t_k`.
   5. Hiçbiri yoksa (pencere boyunca kesintisiz konuşma): hedefe en yakın kelime sınırı.
   *(Builder'ın kararı değil, tasarım kararı: parça uzunlukları bu yüzden `P±10` sn
   oynayabilir; cümle bütünlüğü sabit süreden önemli.)*
3. **Kuyruk ve 180 sn sınırı:** hiçbir parça **180 sn**'yi geçemez (YouTube Shorts
   sınırı; sert sınır). Son parça `< 20 sn` kalırsa, birleşik uzunluk ≤ 180 sn ise bir
   öncekine eklenir; değilse kısa son parça olduğu gibi kalır. Video `P+20` sn'den
   kısaysa (ve ≤ 180 sn) tek parça.
4. **Üst sınır:** parça sayısı `CLIP_MAX_PER_JOB` (60) ile sınırlanır; aşan kısım
   üretilmez ve log'a uyarı yazılır (60 dk'dan uzun videolar için; bu işin hedefi değil).
5. Her parça dict'i (mevcut klip şemasıyla uyumlu):
   - `start`, `end` (float sn), `part_number` (1'den başlar)
   - `viral_hook_text`: `f"{n}. Part\n{hook}"` — hook boşsa sadece `f"{n}. Part"`
   - `video_title_for_youtube_short`: `f"{n}. Part – {title}"` (title boşsa `f"{n}. Part"`)
   - `video_description_for_tiktok` / `_for_instagram`: Gemini'den gelen açıklama (boş olabilir)
   - `predicted_score` **yok** (galeri sırası indeks sırası olsun; App.jsx:380-391)

### 4. Hook'lar — tek Gemini isteği

- gemini_worker.py'ye şema:
  `PartHook(part: int, viral_hook_text: str, title: str, description: str)`,
  `PartHooksResponse(parts: List[PartHook])`.
- Prompt: transkript dili (`transcript["language"]`), her parçanın numarası ve o aralıktaki
  konuşma metni (parça başına ~1500 karakterle kırpılır). İstek: her parça için videonun
  dilinde ≤10 kelime, 1–2 emojili hook; kısa başlık; kısa açıklama. Hook "N. Part"
  içermesin (onu kod ekler). Seri olduğu belli olsun ama her hook kendi parçasının
  içeriğine dayansın.
- `_run_gemini_stage(client, model, prompt, PartHooksResponse)` — mevcut retry + fallback
  model zinciri + timeout aynen geçerli. İstemci kurulumu `get_viral_clips`'teki gibi.
- **Hata → bozulma yok:** istek tamamen başarısız olursa (`GeminiBlockedError` dahil) ya da
  bazı parçalar eksik dönerse, ilgili hook/başlık boş bırakılır, iş devam eder; log'a
  uyarı. `cost_analysis` mevcut biçimde doldurulur (başarısızsa sıfır).
- `LLM_BASE_URL` ayarlıysa mevcut `llm_backend` yolu aynen çalışır (ek iş yok).

### 5. Render

`_process_one_clip` içinde, `args.parts` iken:
- `render_clip(..., force_strategy='WIDE', subtitle_band=...)`. **GENERAL değil WIDE**:
  GENERAL karenin yüksekliğin %42'sini kaplayacak şekilde ölçekleyip yanlardan ~%24
  kırpıyor ve SPLIT'e dönüşebiliyor (reframe_v2.py:175, 711); WIDE tam genişlik 16:9'u
  bulanık zemin üstüne koyuyor ve içerik bandını sidecar'a yazıyor (reframe_v2.py:883-886).
  1920x1080 kaynakta görüntü y≈656–1264.
- `hook_grounding.reground` ve `pro_clip` atlanır.
- Altyazı ve hook mevcut tek-geçiş yoluyla (`AUTO_HOOK=1` dalı, main.py:3347-3359):
  altyazı alt bulanık bantta (~y 1630) kalıyor, hook `position="auto"` ile üst bulanık
  banda yerleşiyor (hooks.py:446-469). Çok satırlı metin `\n` ile destekleniyor
  (hooks.py:312), `pill` stili satır başına ayrı kutu çiziyor.
- Hook süresi: `AUTO_HOOK_SECONDS` (varsayılan 5) — değişiklik yok.
- **Gömülü altyazı (kullanıcı kararı, 2026-11-23):** kaynaklar genelde altyazısız, ama
  altyazılı gelirse (ör. Türkçe altyazılı yabancı film) **kaynağın altyazısı korunur, biz
  altyazı basmayız.** Uygulama: parts modunda `burned_subtitles.detect()` (1 Gemini vision
  isteği) çalışır; bant bulunursa `source_subtitle_band` kırpması **uygulanmaz** ve o işin
  tüm parçalarında `auto_caption_clip` atlanır (hook ve "N. Part" yine basılır; WIDE
  yerleşimde kaynak altyazı görüntünün içinde kalır). Bant bulunamazsa ya da tespit hata
  verirse: bizim altyazımız basılır (bugünkü "hata = altyazı yok" davranışıyla uyumlu).
  Log'a hangi yolun seçildiği yazılır.
- Dosya adları mevcut desen: `{video_title}_clip_{i+1}.mp4` (app.py'nin dosya bulma
  mantığı buna bağlı; değiştirilmez).

### 6. Parçalı yükleme

Backend (`PUT /api/uploads/{id}`, app.py:5963):
- `?offset=N` (ve son parçada `&size=TOPLAM`) desteklenir. `offset` yoksa bugünkü
  "tüm gövde, dosyayı değiştir" davranışı aynen kalır (curl/MCP çağıranlar bozulmaz).
- `offset == 0` → `"wb"`, değilse `"ab"`; `offset` diskteki bayt sayısına eşit değilse
  `409 {"detail": "expected offset <n>"}` (remote_worker'ın chunk yüklemesiyle aynı
  sözleşme, app.py:5653-5701).
- Toplam boyut `MAX_FILE_SIZE_MB` (2048) ile sınırlı; aşınca 413.
- `complete=True` ve süre kontrolü yalnızca `size` gönderilen ve
  `received == size` olan son parçada. Yanıt: `{"received": <bayt>, "complete": bool, ...}`.

Dashboard (App.jsx gönderim yolu, ~1300-1330):
- **Tüm dosya yüklemeleri** (iki mod için de) yeni yolu kullanır:
  `POST /api/uploads` → dosyayı `file.slice()` ile **50 MB**'lık parçalar halinde sırayla
  `PUT ?offset=…` (son parçada `&size=…`) → `POST /api/process` (JSON, `upload_id`,
  `X-Gemini-Key` başlığı ve diğer alanlar).
- Upload URL'i dashboard kendi `getApiUrl` ile kurar (backend'in döndürdüğü `upload_url`
  proxy arkasında iç adres olabiliyor).
- Her parça hata alırsa 3 kez yeniden denenir (409 gelirse sunucunun beklediği offset'ten
  devam edilir). İlerleme yüzde olarak mevcut yükleme durumu alanında gösterilir.
- YouTube linki yolu değişmez.

### 7. Dashboard arayüzü

- `MediaInput.jsx` seçenekler bölümüne: **"Part part böl"** onay kutusu + "Parça süresi
  (sn)" sayı alanı (30–180, varsayılan 60). Açıkken klip sayısı / min / max alanları ve
  yerleşim seçimi gizlenir; hook otomatik açık gösterilir (kapatılamaz); altyazı seçeneği
  kalır. Değerler `advanced` içinde `mode`, `partSeconds` → App.jsx'te `mode`,
  `part_seconds`.
- `ResultCard.jsx`: `clip.part_number` varsa başlık "Clip N" yerine **"N. Part"**
  (ResultCard.jsx:912-914); indirme adı `part-N.mp4` (179). Viral rozeti zaten
  `predicted_score` yokken çıkmıyor.
- Arayüz metinleri Türkçe (bu fork'un kullanıcıları Türk). Mevcut İngilizce metinlere
  dokunulmaz.

### 8. Testler (pytest, gpu-coolify'da)

- `parts.py` saf fonksiyonları: hedef/pencere hesabı, sahne kesimi önceliği, kelime içine
  düşen sahne kesiminin reddi, sessizlik boşluğu seçimi, kelime sınırına düşme, kuyruk
  birleştirme (19 sn → birleşir, 20 sn → ayrı; birleşik > 180 sn → ayrı kalır), cümle
  sonunun sahne kesiminden önce gelmesi, ±10 sn'ye genişleme, kısa video tek parça, 60 parça sınırı,
  hook boşken `viral_hook_text == "N. Part"`.
- Hook çağrısı: `main._run_gemini_stage` monkeypatch (tests/test_clip_floor.py deseni) —
  başarılı, eksik parça, istisna durumları; tek çağrı yapıldığı doğrulanır.
- `/api/process`: `mode=parts` → `cmd`'de `--parts 60`, env'de `AUTO_HOOK=1`;
  `part_seconds` 29/181 → 400; `mode` yokken cmd/env değişmedi
  (tests/test_agent_uploads.py deseni, `job_queue.put` yakalanır).
- Parçalı PUT: sıralı 3 parça → complete; yanlış offset → 409; offset=0 yeniden başlatır;
  offset'siz PUT eski davranış; toplam sınır aşımı → 413; son parça öncesi
  `/api/process` → 409.
- Uçtan uca (elle, sunucuda): ~10 dk'lık bir film sahnesi videosu dosya olarak ve bir
  YouTube linki ile; parça sayısı, kesimlerin cümle ortasına düşmemesi, hook/Part/altyazı
  yerleşimi, galeri sırası kontrol edilir.

### 9. Dağıtım

- Sunucu `/opt/openshorts` bu fork'un `deploy/gpu-coolify` branch'ine geçirilir
  (`origin` → fork, `upstream` → mutonby). Sunucudaki `.env`: `OPENSHORTS_BIND=100.104.65.27`.
- `feat/parts-mode` → PR → `deploy/gpu-coolify` (fork içinde; upstream'e PR açılmaz).
- Backend imajı yeniden build (`docker compose -f compose.server.yml build backend`),
  **kuyruk boşken** restart (BYOK anahtarı resume'da kaybolur).
- **Bu işin kapsamı dışında, ayrı iş:** frontend'in ortak `coolify` ağından çıkarılması
  (bugün aynı ağdaki her container `openshorts-frontend:5173`'e basic auth'suz
  ulaşabiliyor). Coolify'ın yönettiği `coolify-proxy` container'ına dokunmayı
  gerektiriyor ve Coolify onu yeniden oluşturunca ek ağ düşebilir; ayrı tasarlanacak.

## Builder'a bırakılanlar

- `parts.py` iç yapısı, fonksiyon adları, prompt'un tam metni.
- 50 MB parça boyutu ve 3 deneme gibi sayıların ince ayarı (100 MB sınırının altında kalmak şartıyla).
- Dashboard'da seçeneklerin tam görsel yerleşimi (mevcut gelişmiş panel stiline uygun).
- Sahne olasılıklarının dosyaya yazılma ayrıntısı (`prepare_scenes` yolunu yeniden kullanmak tercih).
