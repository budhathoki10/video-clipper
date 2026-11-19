# Clipper: complete project documentation

This document explains what Clipper is, why it is built the way it is, how every part works, and how to
change it safely. It is written so that a developer or an AI coding agent (Codex, Claude Code) with no
prior context can pick the project up. Read `AGENTS.md` in the repo root first for the short rules.

---

## 1. What Clipper is

Clipper turns long YouTube videos and live streams into short, captioned clips that are ready to post
as YouTube Shorts or regular videos. It works for any kind of source (gaming, podcasts, sports,
interviews, vlogs, tutorials, comedy, news). Nothing in the product is specific to one topic.

The owner's goal: find the best moments of someone else's video automatically, make them look
professionally edited, estimate how likely each one is to perform, and post them to the owner's own
YouTube channel immediately or on a schedule, all without staying in front of the app.

### Core capabilities

| Capability | Summary |
|---|---|
| Paste a link | A finished video is downloaded and cut into its strongest moments. A stream that is live right now is watched until it ends. The app detects which it is. |
| Best-moment selection | Free audio and speech signals find candidates; the AI reads the whole transcript, proposes moments, then ranks them side by side. |
| Editing | Reframes to 9:16 / 16:9 / 1:1, word-by-word animated captions, an on-screen hook line, a source credit, loudness normalisation. |
| Audience panel | Up to 100 simulated viewers react to each clip; their answers become a 0-100 score. |
| Trends | Reads what is getting views on YouTube and feeds that into selection, titles and hooks. |
| Live streams | Records in short segments, scores each, and cuts a clip while the stream is still running. |
| Posting | Connect a YouTube channel once; post now or schedule; choose Short or regular video; view counts are read back. |
| Background operation | A separate worker process does all heavy work, so closing the browser never stops anything. |
| Settings | Clip length/count, format and quality, captions, AI engine and key, live sensitivity, posting defaults, trend searches. |

### What it deliberately does not do
- It does not promise views. Scores rank clips against each other; they are not a forecast.
- It does not read live chat. Live detection uses audio and speech only.
- The audience panel reads (transcript, title, hook); it does not watch the picture.
- It does not show users where clips are stored (see section 9).

---

## 2. Architecture at a glance

```
Browser  ──►  FastAPI (app.main)  ──►  MongoDB  ◄──  Worker (app.worker)
 (Next.js          │  serves the static dashboard                          │ runs jobs
  static export)   │  queues jobs, never does heavy work                   │ in threads
                   ▼                                                       ▼
            signed links  ◄── remote storage ◄── clips        yt-dlp · ffmpeg · faster-whisper · LLM · YouTube API
```

- **Two Python processes.** `app.main` (the dashboard API, started by uvicorn) and `app.worker` (a
  detached background process). They communicate only through the MongoDB database. The API starts the
  worker if it is not running (`ensure_worker`).
- **Jobs are rows in the `jobs` table.** The API inserts a row with `status='queued'`; the worker claims
  it. If the worker or the PC stops mid-job, on the next start every `running` job goes back to
  `queued` and resumes from its last finished step (each step writes its result to the job folder).
- **The dashboard is a static export** of a Next.js app (`frontend/out`). The Python API serves it at
  `/`, so production needs no Node process. In development the dashboard runs on `:3000` and calls the
  API on `:8765` (`NEXT_PUBLIC_API_URL` in `frontend/.env.development`).
- **No Redis, no external queue.** Jobs are documents in MongoDB; the worker takes one with an atomic
  update (`db.claim_job`), so two workers can never run the same job.
- **Google accounts protect the API.** Google OpenID Connect supplies a verified identity; the stable
  `sub` claim identifies the account. The browser receives an opaque 30-day HttpOnly session cookie;
  only its SHA-256 digest is stored. Anyone with a Google account can sign in; there are no roles.
  Jobs, clips and posts carry a `user_id` and each person sees only their own. Settings, AI keys and the
  YouTube channel are per person; API keys and YouTube tokens are encrypted with `APP_SECRET_KEY` before
  they reach the database. The one shared secret is the platform NVIDIA key. A person may have three
  videos in progress at a time; video length is not limited.
- **MongoDB is the only database.** `db.py` wraps it (documents have a string `id`; `kv` holds app
  settings, per-person settings and the worker heartbeat). The API and the worker are separate processes
  that share it. Tests use an in-memory stand-in (`mongomock`), or a real MongoDB when
  `CLIPPER_TEST_MONGODB_URI` is set.
- **The server keeps nothing.** Downloaded videos, working files (transcript, signals) and finished clips
  are in S3-compatible storage. While a job runs they are in a scratch folder; when it finishes
  `runner.persist_job_files` uploads them and clears the folder, and `restore_job_files` brings them back
  for "more clips", re-rendering and hooks.

- **Several computers, one app.** Any number of workers can share the database; each is named after its
  computer (`worker:<host>` heartbeat) and takes only the jobs it accepts (`CLIPPER_WORKER_ACCEPTS`: `all`,
  `links` or `files`). A job is claimed atomically and records which computer runs it (`worker`). Jobs of a
  computer whose heartbeat stopped for 90 s are returned to the queue (`requeue_orphans`). This lets a home PC
  take YouTube-link jobs that a hosting server cannot download; see docs/DEPLOY_RENDER.md.
- **Upload a video file.** Instead of a link, a person can upload a file (`POST /api/uploads`, then
  `PUT /api/uploads/<id>/chunk?offset=N` for parts of up to 16 MB, then `POST /api/uploads/<id>/complete`).
  The parts are written to a scratch folder under `CLIPPER_DATA_DIR/uploads`, checked with ffprobe, then moved
  into a job folder with a `meta.json` and `download.done`, so the job (`kind: video`, no `url`) skips the
  download and runs the normal pipeline. Unfinished uploads are removed after a day. The server checks free disk
  space before accepting a file and keeps no length limit.

### Technology
Python 3.11+ (developed on 3.13), FastAPI, MongoDB (pymongo), yt-dlp, ffmpeg (NVENC when available, x264
otherwise), faster-whisper (GPU first, CPU fallback), boto3 (S3-compatible storage), Google API client
(YouTube). Frontend: Next.js 16 (App Router, static export), React 19, Tailwind CSS 4, lucide-react.

> The frontend uses a recent Next.js with breaking changes. Read `frontend/AGENTS.md` and the guides in
> `frontend/node_modules/next/dist/docs/` before writing frontend code.

---

## 3. Repository layout

```
AGENTS.md                      short rules for coding agents (read first)
README.md                      user-facing quick start
docs/DOCUMENTATION.md          this file
scripts/start.ps1 stop.ps1 autostart.ps1     Windows helpers (background start/stop/start at sign-in)
backend/
  .env                         SECRETS (gitignored). .env.example lists the names
  requirements.txt  pytest.ini
  assets/fonts/                bundled caption fonts (Poppins ExtraBold, Anton, Bangers; SIL OFL)
  app/
    main.py                    dashboard API (routes, validation, serving files)
    worker.py                  background process: claims and runs jobs, heartbeat, notifications
    runner.py                  the steps for one pasted video; clip making; re-rendering
    live.py                    live-stream watcher
    llm.py                     one interface over the AI providers
    storage.py                 remote storage (S3-compatible) for finished clips
    youtube.py                 channel sign-in, upload, schedule, view counts
    config.py                  paths, default settings, secret handling
    db.py                      MongoDB access, indexes, settings storage
    keys.py                    encrypted per-person API keys and YouTube tokens
    pipeline/
      media.py                 yt-dlp + ffmpeg helpers (probe, download, audio, encoder choice)
      transcribe.py            word-level transcription
      signals.py               loudness / jump / speech-rate / "most replayed" scoring
      selection.py             AI moment selection, ranking, hook writing
      captions.py              ASS subtitle file (captions, hook card, credit)
      render.py                the ffmpeg render of one clip
      panel.py                 simulated audience panel
      trends.py                YouTube trend reading and per-topic profiles
  tests/test_e2e.py            the end-to-end and unit checks
frontend/
  app/                         pages: / (Home), clips, clip, live, posts, trends, settings
  components/                  ui.tsx (design system), shell.tsx, clip-card, job-row, caption-preview
  lib/api.ts                   typed API client, hooks, formatters
data/                          (gitignored) videos, database, tokens, test output
```

---

## 4. How a pasted video becomes clips

Entry: `POST /api/jobs {url, options}` creates a job with `kind='auto'`. The worker's `detect_kind`
asks yt-dlp whether the link is live now, then re-queues it as `video` or `live`.

### `video` jobs: `runner.process_video`
Each step writes its output into `data/jobs/<job_id>/` so a restart resumes instead of repeating.

1. **Details**: `media.fetch_info` returns title, channel, duration, thumbnail, YouTube's "most replayed"
   heatmap, category and tags (`meta.json`).
2. **Download**: `media.download` (yt-dlp, up to the chosen quality, `download.done` marks completion).
3. **Transcribe**: audio to 16 kHz mono WAV, then faster-whisper with word timestamps
   (`transcript.json`: `words` with `s`/`e` seconds, and sentence `segments`). The WAV is read in Python
   and passed in as samples, so the library's own decoder (PyAV) is never used.
4. **Signals**: `signals.analyze` scores every second from loudness, sudden loudness jumps, speaking
   speed and the heatmap (`signals.json`). `signals.candidates` takes the top peaks at least
   `max_sec` apart.
5. **Trends**: `trends.brief_for(meta)` returns advice for this kind of video (section 8).
6. **Selection** (`selection.select_clips`), in three passes:
   - *Content pass*: the transcript is read in 7-minute sections (30 s overlap); the AI proposes up to 3
     moments per section.
   - *Audio-peak pass*: the AI judges the windows around the loudest candidates, catching moments that
     read flat on paper.
   - *Ranking pass*: cut points are snapped to word boundaries, overlaps removed (better one kept), and
     the shortlist is judged side by side in one call so scores are comparable; the AI may also rewrite
     each hook. A small bonus (up to 6 points) is added where the audio also peaks. Results are saved
     in `selection.json`.
   - If the AI is unavailable, clips are cut around the audio peaks and a warning is stored.
7. **Render** (two clips at a time): `runner.make_clip` renders, stores the output (section 9), then
   runs the audience panel.
8. **Finish**: the job becomes `done`; the worker sends a Windows notification; the downloaded source is
   deleted when `storage.delete_source` is on and the clips exist.

### `more` jobs: `runner.process_more`
After a video is clipped, "More clips" (Home, on the video's row) finds further moments, for example to make a
Part 2 when the first clips did well. It reuses the saved `transcript.json`, `meta.json`, `signals.json` and
`trends.json`, so it needs no transcription. The ranges of the clips that already exist are passed to
`selection.select_clips(exclude=...)`, so nothing overlapping them is returned, and the content pass looks a
little deeper. If the AI is unavailable the job fails instead of falling back to audio peaks (that would repeat
the first pass). The source is downloaded again if it was removed. New clips are saved under the **same video**
(`clips.job_id` is the original job), optionally titled "- Part N" continuing from the clips already made. The
job runs on the heavy lane (one at a time with `video`), and its progress is attached to the video's row in
`GET /api/jobs` as `more`. Only one `more` job per video at a time.

### `live` jobs: `live.LiveSession`
- ffmpeg records the stream in `segment_sec` (default 20 s) MPEG-TS segments. YouTube serves live
  streams as separate video and audio feeds, so the recorder maps video from the first input and audio
  from the second (`stream_urls`).
- Each finished segment is transcribed and measured against the stream's own running baseline
  (robust z-scores of loudness and speaking rate, plus a bonus for excited phrases).
- When `live.sustain_segments` segments in a row (default 2) pass `live.sensitivity` and the cooldown
  allows, the segments around the strongest of them are concatenated, the AI trims them with the same
  selection code, and the clip is rendered while recording continues. One loud segment alone never
  starts a clip.
- Every clip (finished video, "more clips" and live) is widened by `clip.context_sec` (default 5) on each
  side after the AI picks it (`runner.add_context`), so a 20 s moment becomes a 30 s clip. A vertical
  clip that fit in a Short is never pushed past 3 minutes. For finished videos, candidate moments are
  ranked by a 15-second average of the score (`signals.SUSTAIN_SEC`), so a single bang does not outrank
  a lasting moment.
- Old segments are deleted as they age out, so disk use stays flat for any stream length. If the stream
  address expires, the recorder restarts (up to 40 times). Stopping a live job sets `stopping`; the
  watcher ends cleanly and keeps its clips.

### Rendering (`pipeline/render.py`)
- Output size is set by `aspect` and `quality` (the short side): 1080 gives 1080x1920 for 9:16.
- **Layout** `auto` (default): `pipeline/facecam.py` samples 10 frames of the clip and looks for the
  creator's face with OpenCV's YuNet model (`assets/models`, MIT). A small face that stays in one place
  near an edge is a **webcam**: its whole overlay box is found (inside it the room stays still across
  frames sampled up to 30 s around the clip; outside it the game moves), and a vertical clip is split:
  the whole webcam on top (28-45% of the height, in the webcam's own shape) and the middle of the action
  below, with the hook title just under the webcam. A small webcam is enlarged at most 3x (lanczos plus a
  light sharpen) and centred over a blurred copy of itself rather than stretched into blocks. A large steady face is a **speaker**: the crop follows
  it sideways. Faces in the content (they move, or sit mid-frame) are ignored. With no such face, `auto`
  is `blur` at zoom 1, so nothing is cut off. `blur`: the whole frame over a blurred, darkened copy of
  itself (optional zoom; above 1 it crops the sides). `crop`: fill the frame. A source already in the
  target shape is just scaled. Settings saved with the old default (`blur` at zoom 1.35, which cut corner
  webcams off) are treated as `auto` (`render.plan_layout`).
- Captions, hook and credit come from one ASS file burned in with the `ass` filter. ffmpeg runs with the
  clip folder as its working directory so filter arguments are bare names (Windows drive letters and
  spaces break filter strings). Bundled fonts are copied to `<clips>/_fonts` under a lock (clips render
  in parallel).
- Audio is normalised to -14 LUFS. Video uses NVENC if a test encode succeeds, else x264.

### Captions (`pipeline/captions.py`)
Word-by-word: the group of up to `words_per_line` words stays on screen; the word being spoken takes
the highlight colour and pops (starts 14% larger, settles in 90 ms). Commas and full stops are dropped;
`?` and `!` kept. Position, size, colours, font and capitals come from settings. The hook card sits at
the top; the source credit above it.

### Hooks
The on-screen hook is the line over the first seconds. `selection.HOOK_RULES` is the single source of
the writing rules (curiosity gap, specific, plain words, one emotion, never misleading, at most 6 words).
The AI writes a hook with every clip, the ranking pass may improve it, and
`POST /api/clips/{id}/hooks` returns 5 alternatives for a clip the user selects from.

### Audience panel (`pipeline/panel.py`)
20 archetypes × 5 moods give up to 100 personas, batched 25 per AI call. Each returns watch percent,
like, share and a comment. Score = 0.45·watch + 0.20·finished + 0.15·like + 0.20·share. Persona names
are topic-neutral. The summary (per-type watch time, sample comments) is stored in `clips.panel`.

---

## 5. Data model (MongoDB collections, `db.py`)

Tables are created by `db.init()`; new columns are added there with `ALTER TABLE` when missing.

- `jobs(id, kind, url, status, step, progress, title, channel, duration, thumbnail, error, options, payload, created_at, updated_at)`
  - `kind`: `auto`, `video`, `more`, `live`, `render`, `upload`, `trends`, `migrate`.
  - `status`: `queued`, `running`, `stopping`, `done`, `failed`, `cancelled`.
  - `options` holds the merged settings for this job; `payload` holds job-specific data
    (for live: `watched_sec`, `scores`, `threshold`; for others `warning`, `storage_warning`, ids).
- `clips(id, job_id, status, start, end, title, hook, description, hashtags, reason, ai_score, panel_score, panel, file, thumb, width, height, source_title, source_channel, source_url, options, error, created_at)`
  - `file`/`thumb` are storage references (section 9). `options` stores the render settings, the paths
    needed to re-render, the trend brief and the source category.
- `posts(id, clip_id, status, title, description, privacy, publish_at, youtube_id, views, error, created_at, updated_at, kind)`
  - `kind` is `short` or `video`.
- `kv(key, value)`: JSON values. Keys: `settings` (user overrides), `trends` and `trends:<slug>`
  (trend profiles), `worker` (heartbeat).
- `users(id, email, name, google_sub, avatar_url, created_at, last_login_at)`: Google accounts. Everyone
  is an ordinary user.
- `user_keys(_id "<user>:<name>", user_id, name, value, hint)`: a person's own API keys, encrypted.
- `youtube(_id <user>, user_id, token, channel)`: a person's connected channel; the token is encrypted.
- `sessions(token_hash, user_id, created_at, last_seen_at, expires_at)`: opaque browser sessions.
- `oauth_states(state_hash, code_verifier, redirect_uri, created_at, expires_at)`: one-time,
  ten-minute Google OAuth state and PKCE data used to prevent callback forgery.

Jobs and clips also carry `user_id` (the owner). A job has `files` (storage references to its saved
working files and downloaded video). `kv` keys also include `prefs:<user_id>` (a person's settings) and
`youtube-connect:<user_id>` (a pending channel sign-in).

---

## 6. Settings and configuration

### User settings (`config.DEFAULT_SETTINGS`, edited in the Settings page)
Stored as overrides in `kv['settings']`, merged over the defaults (`deep_merge`).

| Section | Keys |
|---|---|
| `clip` | `min_sec`, `max_sec`, `max_clips`, `min_score`, `context_sec` |
| `format` | `aspect` (9:16/16:9/1:1), `layout` (auto/blur/crop), `zoom`, `quality`, `fps`, `source_quality` |
| `captions` | `enabled`, `font`, `size`, `color`, `highlight`, `position`, `words_per_line`, `uppercase`, `language` |
| `overlay` | `hook_title`, `credit` |
| `ai` | `provider`, `models{provider: model}`, `panel_size`, `whisper_model` |
| `live` | `sensitivity`, `sustain_segments`, `segment_sec`, `lookback_sec`, `cooldown_sec`, `min_score`, `quality` |
| `trends` | `queries[]`, `match_topic`, `per_query`, `refresh_hours` |
| `posting` | `privacy`, `hashtags`, `credit` |

A new job takes the saved settings plus per-job overrides (`clip`, `format`, `captions`, `overlay`,
`live`, `max_minutes`) from the Home form. A clip keeps the settings it was rendered with
(`clips.options.settings`).

### Server-side configuration (never shown to users)
`config.DEFAULT_SETTINGS['storage']` (endpoint, region, bucket, prefix, `enabled`, `delete_source`) is
configuration, not a user setting. `GET/PUT /api/settings` hide and ignore it (`HIDDEN_SETTINGS`).

### Secrets: `backend/.env` (gitignored) or the host's environment

| Name | Purpose | Visible to users? |
|---|---|---|
| `MONGODB_URI` (`MONGODB_DB`) | The database connection string | **No**; never in a response or log |
| `APP_SECRET_KEY` | Encrypts people's saved API keys and YouTube tokens | **No** |
| `NVIDIA_API_KEY` | The shared key everyone uses by default for NVIDIA Nemotron Ultra | **No**; people only see "shared key included" |
| `GOOGLE_LOGIN_CLIENT_ID`, `GOOGLE_LOGIN_CLIENT_SECRET` | Google Web OAuth client: sign-in and each person's YouTube connection | **No** |
| `BLOMP_ACCESS_KEY_ID`, `BLOMP_SECRET_ACCESS_KEY`, `BLOMP_BUCKET` | Storage keys and bucket | **No**; not in any response, not editable in the UI |
| `PUBLIC_URL` | The address people open when hosted | n/a |

A person's own Anthropic, Gemini or NVIDIA key is entered in Settings and stored encrypted in the
database (`keys.py`); the API only reports whether it is set and its last four characters. Never write a
real key into source, tests, docs or any response. `config.SECRET_NAMES` lists every value the tests
check for leaks.

### Files under the data folder (`CLIPPER_DATA_DIR`, gitignored)
Scratch space only: `jobs/<id>/` (the video being processed, removed when the job finishes), `tmp/`,
`worker.log`, `test-run/`. Everything lasting is in MongoDB and the storage.

---

## 7. AI providers (`llm.py`)

`complete()` and `complete_json()` hide four providers behind one call: NVIDIA (default model
`nvidia/nemotron-3-ultra-550b-a55b` via NVIDIA's OpenAI-compatible endpoint), Anthropic, Gemini and
Ollama (local). The provider and model are the person's own setting and the key is theirs (`keys.effective`): their own
key, or for NVIDIA the shared one. Jobs run under `llm.as_user(job.user_id)`.
Retries with backoff on rate limits and server errors; 401/403 and 404 produce clear messages. JSON
answers are parsed tolerantly (fences, stray prose) with one retry. To add a provider: add a branch in
`llm._request`, an entry in `config.PROVIDER_SECRET` and `DEFAULT_SETTINGS['ai']['models']`, a
`config.PUBLIC_SECRETS` name if it needs a key, and an option in the Settings page.

---

## 8. Trends (`pipeline/trends.py`)

Uses yt-dlp's YouTube search (no API key). `refresh(queries, key)` collects up to 40 top videos, asks
the AI for a profile (summary, hook patterns, title templates, topics, best clip length, things to avoid),
and stores it under `key`. The general profile (`trends`) feeds the Trends page. When `trends.match_topic`
is on, `brief_for(meta)` also searches the source video's own tags and category and keeps a separate
profile per topic (`trends:<slug>`), refreshed when older than `refresh_hours`. The brief is passed to
selection, hook writing and the audience panel, and never raises: trends improve results but are optional.

---

## 9. Storage (server-side only)

Finished clips and thumbnails are uploaded to S3-compatible storage (Blomp, path-style addressing,
endpoint `https://storage.blomp.com`) and the local copies are deleted. **This is intentionally invisible
to users**: no settings, no provider names, no free-disk indicator, no storage paths in any response.

- **References.** `clips.file` / `clips.thumb` hold `s3:<key>` for stored files, or a path relative to
  `data/` for files kept locally (`storage.is_remote`). Key layout: `clipper/<job_id>/clips/<clip_id>.mp4`.
- **Write path.** `runner.store_outputs` uploads after a render. If the upload fails the clip stays on
  disk and still works, and a generic warning is stored on the job.
- **Read path.** The browser asks the API for `/api/clips/{id}/file|thumb|download`; for stored files the
  API answers `307` to a temporary signed link (6 hours; range requests work, so seeking plays). The
  bucket stays private.
- **Posting.** `worker.run_upload` uses `storage.local_copy`, a temporary download that is removed
  afterwards.
- **Deleting** a clip or video also deletes the stored objects (`storage.delete`, best effort).
- **Sources.** With `delete_source` on, the downloaded source is removed once its clips exist. Editing a
  clip's trim later downloads the source again (`runner.fetch_source_again`); clips cut from live
  recordings cannot be re-trimmed because their recording is removed.
- **Migration.** `storage.migrate_local_clips` moves clips created before storage was on. The worker
  queues it at start-up when local clips exist (`POST /api/storage/migrate` does the same on demand).
- **Admin check.** `POST /api/storage/test` writes, reads back through a signed link and deletes a tiny
  object (used by the tests).

Rules for any change here: never put a storage detail in a user-visible response or message; error text
from `storage._explain` is generic ("the storage service"). The test `test_storage_stays_server_side_and_is_not_shown_to_users`
enforces this.

---

## 10. YouTube (`youtube.py`)

- **Connect.** The owner of a copy of the app registers one Google "Desktop app" OAuth client and puts
  its ID and secret in `backend/.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. They are read
  only on the server and are never accepted or returned by an API endpoint. After that, anyone presses
  **Connect YouTube**: the backend
  runs `InstalledAppFlow.run_local_server` in a thread; a custom browser handler (`_OpenAndRemember`)
  opens the Google page and also stores its address so the dashboard can show a link if the page did not
  come to the front. The sign-in has a 10-minute timeout; `status()["connecting"]` is the thread lock.
- **Scopes:** `youtube.upload` and `youtube.readonly`.
- **Upload.** `youtube.upload` does a resumable upload. Scheduling sends `privacyStatus=private` plus
  `publishAt` (UTC), which YouTube requires. The category follows the source video's YouTube category.
- **Short or video** (`main.clip_kind`). YouTube decides: a vertical or square file of up to 3 minutes
  is a Short; anything else is a regular video. `POST /api/posts` accepts `kind` and refuses a
  mismatch with a clear message; the dialog then offers "Render as 9:16 Short" / "Render as 16:9 video".
  `#Shorts` is added to a Short's description and removed from a regular video's.
- **Views.** The worker refreshes view counts every 30 minutes.
- **Google limits (not app limits).** Until Google audits the Cloud project, API uploads stay private
  and daily uploads are quota-limited. A sign-in in "testing" mode expires after 7 days.
- Reposting someone else's footage can lose monetisation or draw copyright claims. The credit overlay and
  description credit are on by default; they reduce, not remove, that risk.

---

## 11. Background operation (`worker.py`, `scripts/`)

- The worker loop (every 1.5 s) writes a heartbeat, dispatches queued jobs, keeps Windows awake while
  work exists (`SetThreadExecutionState`), and refreshes view counts.
- Concurrency: one `video` job at a time (GPU), up to 3 light jobs (render, upload, trends, migrate),
  one thread per live watch.
- `is_alive()` requires a recent heartbeat **and** a running process id.
- On start: `running` jobs become `queued` (resume); `stopping` jobs become `done`.
- A Windows toast is sent when a job finishes (`worker.notify`, text passed through environment
  variables, never interpolated into the script).
- **Disk safety.** Cancelling a job really stops its download (`runner.cancel_aware` raises `Cancelled` from the
  download's progress callback) and removes the half-downloaded files (`runner.cleanup_partial`). The worker sweeps
  folders under `data/jobs` that belong to no job and were untouched for 10 minutes (`runner.sweep_orphans`, at start
  and hourly). Downloads and live recordings refuse to start with less than 5 GB free (`runner.require_disk`).
  Background: a cancelled-then-deleted job once left a 15 GB partial download behind because cancelling only changed
  the job's status. Large caches (npm, pip) live outside the project and can be cleared safely.
- `scripts/start.ps1` builds the dashboard if needed, starts uvicorn hidden, opens the browser;
  `stop.ps1` stops the server, worker and recorders; `autostart.ps1` registers a sign-in task.

---

## 12. HTTP API (all under `/api`, JSON)

| Method and path | Purpose |
|---|---|
| `GET /system`, `POST /system/start-worker`, `POST /ai/test` | Worker status and AI engine; start the worker; test the AI key |
| `GET /health`, `GET /auth/google`, `/auth/google/callback`, `POST /auth/logout`, `GET /auth/me` | Public health check and Google account session lifecycle |
| `GET /users` | Administrator-only account list, total users and active users |
| `GET /jobs`, `POST /jobs`, `GET /jobs/{id}` | List, create (paste a link), read with its clips |
| `POST /jobs/{id}/more` | Make more clips from a finished video (`count` 1-10, `label_part`); the request shows on the video's row as `more` |
| `POST /jobs/{id}/stop`, `POST /jobs/{id}/retry`, `DELETE /jobs/{id}` | Cancel or stop a live watch; retry; remove a video from the list (its clips, their files and the small working files are kept; only its big downloaded copy goes; a video with no clips is deleted outright) |
| `GET /clips`, `GET /clips/{id}`, `PATCH /clips/{id}`, `DELETE /clips/{id}` | Clips; PATCH edits title/hook/description/trim/format and can re-render |
| `POST /clips/{id}/hooks` | Five alternative on-screen hooks |
| `GET /clips/{id}/file`, `/thumb`, `/download` | Playback, thumbnail, download (redirect to a signed link when stored remotely) |
| `GET /settings`, `PUT /settings`, `PUT /secrets` | User settings (storage hidden) and replacing the AI keys |
| `GET /trends`, `POST /trends/refresh` | Trend profile; queue a refresh |
| `GET /youtube`, `POST /youtube/client`, `/connect`, `/disconnect` | Channel connection |
| `GET /posts`, `POST /posts`, `DELETE /posts/{id}` | Posts; create (now/schedule, `kind`, `privacy`); remove the record only |
| `POST /storage/test`, `POST /storage/migrate` | Server administration; not used by the UI |

Errors are `{ "detail": "plain-language message" }`. `clip_out` removes internal fields (`file`, `thumb`)
before sending a clip.

---

## 13. Frontend

- **Pages:** public landing and Google sign-in; Home (paste a link, per-video options, jobs, top clips), Clips (library with search,
  filters, sort, paging), Clip (preview, edit, hook suggestions, post/schedule, audience panel), Live
  (activity graph and clips per stream), Posts (table), Trends, Settings (tabs: Clips, Format, Captions
  with live preview, AI engine, Live streams, Posting, Trends).
- **State:** no state library. `useApi(path, interval)` in `lib/api.ts` fetches and polls while the tab
  is visible. All API types live in `lib/api.ts`.
- **Design system** (`app/globals.css` tokens, `components/ui.tsx`): one **light** theme. Page `#F5F7FB`;
  sidebar, cards, dropdowns, dialogs and toasts white; secondary surfaces `#EEF2F7`; borders `#E5EAF1`;
  one accent, electric blue `#3478F6` (hover `#2362D9`); navy text `#202B3C`, never pure black.
  Success `#16A36A`, warning `#D99118`, error `#DC4545` are for fills and icons; small text in those
  colours uses the darker `-ink` shades (`text-ok-ink`, `text-warn-ink`, `text-bad-ink`) so it stays readable.
  Secondary text uses `ink-2`/`ink-3` (slightly darker than `#718096`) to meet contrast. Depth comes from
  hairline borders plus very soft shadows (`shadow-soft`, `shadow-lift`, `shadow-pop`); radii 6/8/12/14;
  4 px grid; type scale 12/13/14/16/20/24; 120-200 ms motion (opacity and 2-4 px translate only);
  `prefers-reduced-motion` respected; native `<dialog>` for modals. Video thumbnails and the video preview
  sit on a navy frame so the footage stays the most prominent thing on screen. Every list has loading
  (skeleton), empty and error states; every control has hover, focus-visible, disabled and loading states.
  Do not reintroduce a dark theme, gradients, glass or emoji.
- **Copy rules:** plain verbs, sentence case, say what happens ("Save changes", "Post now"); errors say
  what broke and how to fix it; never name the storage provider; keys are never displayed.
- **Static export caveats:** pages that read the query string (`/clip/?id=…`, `/clips/?job=…`) are
  wrapped in `Suspense`; links use trailing slashes.

---

## 14. Testing

`backend/tests/test_e2e.py` (run from `backend`):

```
.venv\Scripts\python -m pytest                    # everything, about 8 minutes
.venv\Scripts\python -m pytest -m "not network"   # offline checks, about 20 seconds
```

Tests use their own data folder (`data/test-run`, via `CLIPPER_DATA_DIR`) and never touch real clips.

- **Offline:** caption grouping and animation, output sizes, signal peak detection, rendering in three
  layouts, the API never returns any secret, no secret in any source file, bad input rejected, job
  queue/cancel, the upload request (private/scheduled, UTC time, title length) against a stand-in for
  YouTube, post kind rules, failed-upload reporting, client-file validation, Windows toast, worker
  heartbeat, signed-link serving, store-or-keep-local, hook cleanup, storage hidden from users.
- **Network (`-m network`):** the AI answers; the full pipeline on a real video
  (`CLIPPER_TEST_URL`, default the owner's sample; `CLIPPER_TEST_MINUTES`, default 6) ending with clips
  in remote storage and a downloaded copy in `data/test-run/e2e-clips`; a storage round trip with range
  requests and deletion.
- **Not automated:** a real upload to the owner's channel (needs their Google sign-in) was done by hand;
  the Windows toast's visibility on screen needs a human.

When you change behaviour, change or add a test in the same commit.

---

## 15. Running and developing

```
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env         # add keys
cd ..
scripts\start.ps1              # http://127.0.0.1:8765, runs hidden in the background
```

Needs ffmpeg and ffprobe on PATH, Python 3.11+, Node 20+. An NVIDIA GPU makes transcription and
rendering much faster (falls back to CPU; set `CLIPPER_FORCE_CPU=1` to force it).

Frontend development: `cd frontend; npm install; npm run dev` (API must be running on 8765); after
changes run `npm run build` (output `frontend/out`) so the API serves the new dashboard.

Windows notes: ffmpeg filter paths are kept relative (run with `cwd`); subprocesses use
`CREATE_NO_WINDOW`; never pass user text into a PowerShell script body, use environment variables.

---

## 16. Design decisions worth knowing

| Decision | Why |
|---|---|
| Separate worker process | Closing the browser or API must not stop work; jobs resume after a crash or reboot |
| MongoDB, no Redis | One shared database for the API and the worker; accounts, jobs and settings survive any server |
| Static dashboard served by the API | One address, no Node in production |
| Free signals before the AI | Cheap, and they point the AI at the right parts of a long video |
| Rank candidates together | Scores from separate calls are not comparable |
| Signed links, private bucket | Clips are previewable without making them public |
| Storage hidden from users | Owner's requirement: users should not see or manage infrastructure |
| yt-dlp search for trends | No extra API key or quota |
| Short/video enforced, not just labelled | YouTube has no flag; the file's shape decides |
| Honest scores | Panel and AI scores rank clips; they never promise views |

---

## 17. Known limitations and ideas

- Live chat is not used as a signal.
- The panel does not see the picture; a vision-capable model could judge frames.
- Face following is per clip, not per moment: the webcam or speaker position is fixed for the whole clip,
  and a webcam that moves mid-clip is not followed. If the room behind the streamer moves, or the game
  next to the webcam stays still, an edge can be missed; that side then uses the usual 16:9 webcam shape.
- Clips cut from live recordings cannot be re-trimmed (recording removed after clipping).
- One `video` job runs at a time.
- Windows only for the helper scripts and notifications (the Python code is mostly portable).
- Sign-in token lifetime and upload quota are controlled by Google (section 10).

---

## 18. Checklist before finishing any change

1. Secrets: nothing secret in code, tests, docs, logs, responses or the frontend bundle.
2. Storage: nothing in a user-visible place names or reveals storage.
3. UI follows the design system and has loading, empty and error states; works at phone width.
4. Offline tests pass; run the full suite when pipeline, storage or posting changed.
5. Do not commit unless the owner asks; never add attribution lines to commits or PRs.


---

## 19. iPhone / home-screen app (PWA)

The dashboard is a progressive web app. Nothing native is built: iOS Safari's "Add to Home Screen" is the
install step.

- `frontend/public/manifest.webmanifest`: name, start URL `/dashboard/`, standalone display, colours and icons.
- Icons in `frontend/public/`: `apple-touch-icon.png` (180), `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`.
  A vertical frame with a play triangle; deliberately not the YouTube mark.
- `frontend/app/layout.tsx` metadata: `appleWebApp` (full screen, title "Clipper"), `apple-touch-icon`, manifest
  link, and viewport `viewport-fit=cover` so the layout can use the safe areas (home indicator).
- `frontend/app/globals.css`: safe-area padding, no tap highlight or double-tap-zoom delay, and for touch screens
  (`pointer: coarse`) 44 px controls and **16 px text in fields** (iOS zooms into any field below 16 px).
  Unlayered CSS beats `@layer` rules, so touch overrides that must lose to a layered rule (for example
  `textarea.field { height: auto }`) have to be repeated inside the media block.
- There is deliberately **no service worker**: the app is useless without the engine, so offline caching would only
  show stale pages.

### Where the engine runs (the part that decides whether the laptop can be off)
The phone only displays the dashboard; the worker, ffmpeg, yt-dlp and transcription run on the host machine.

| Option | Laptop must be on? | Notes |
|---|---|---|
| Same Wi-Fi (bind to `0.0.0.0`, open the laptop's address) | Yes, and nearby | Quick test. Needs a firewall rule. Plain HTTP, so no secure cookies or Google sign-in redirect |
| Secure tunnel to the laptop (Tailscale or Cloudflare Tunnel) | Yes, but can be anywhere | HTTPS and a stable address; works from mobile data |
| Always-on server (cloud VM) | **No** | Needs HTTPS and a domain, the Google redirect address for that domain, a web-based YouTube sign-in (the current YouTube connect uses a loopback Desktop flow that cannot work on a remote server), and a plan for transcription without a graphics card (CPU is slower; a hosted transcription API is the alternative) |


---

## 20. Native iOS app (Capacitor)

The same dashboard, packaged as an iOS app. No Vite conversion is needed: Capacitor wraps any static build, and
`frontend/out` (the Next.js static export) is its web directory.

**Done**
- `frontend/capacitor.config.ts` (app id `app.clipper.mobile`, name Clipper, `webDir: out`, pages served from `https://localhost`).
- `frontend/ios/` is the generated Xcode project, with the app icon (1024 px RGB, no alpha) installed.
- Plugins: `@capacitor/app` and `@capacitor/browser` (for the sign-in flow below).
- `npm run native:sync` (in `frontend/`) rebuilds the dashboard and copies it into the iOS project.

**Not done, and why it matters**
1. **A hosted server.** The packaged app has no engine; it must call one over HTTPS. Set `NEXT_PUBLIC_API_URL` to that
   address when running `native:sync`, and add the app's origin (`https://localhost`) to the server's CORS list
   (`main.py`, currently only the dev ports).
2. **Sign-in.** The session cookie is `SameSite=Lax`, which iOS will not send from the app to a different address,
   and Google refuses sign-in inside an embedded web view. Needs a native flow: open `/api/auth/google` in the system
   browser (`@capacitor/browser`), return to the app through a custom URL scheme with a one-time token, keep a
   session token in secure storage, and send it as an `Authorization` header. This changes `auth.py` and the
   middleware in `main.py`.
3. **Building.** iOS apps can only be built on macOS: a Mac with Xcode, or a cloud Mac build service (Codemagic, GitHub
   macOS runners). An Apple Developer account ($99/year) is needed to install beyond 7 days or use TestFlight.
4. **App Store.** Apple may reject a thin web wrapper; TestFlight (private) avoids review for personal use.

Push notifications ("clips are ready") would be the main benefit over the home-screen web app and need an Apple
push key plus a small backend sender.
