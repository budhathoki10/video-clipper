# Instructions for coding agents (Codex, Claude Code)

Clipper turns long YouTube videos and live streams into captioned, ready-to-post clips and posts them to
the owner's YouTube channel. Full explanation: **`docs/DOCUMENTATION.md`** (read it before changing
anything non-trivial). User-facing quick start: `README.md`.

## Map
- `backend/app/main.py` dashboard API (queues jobs, never does heavy work)
- `backend/app/worker.py` background process that runs jobs; `runner.py` video steps; `live.py` live streams
- `backend/app/pipeline/` download, transcribe, signals, AI selection, captions, render, panel, trends
- `backend/app/llm.py` AI providers; `youtube.py` posting; `storage.py` remote storage; `db.py` MongoDB;
  `keys.py` encrypted per-person keys and YouTube tokens; `auth.py` Google sign-in
- `frontend/` Next.js dashboard (static export, served by the API); `scripts/` Windows start/stop
- `backend/tests/test_e2e.py` the tests

## Commands
```
cd backend
.venv\Scripts\python -m pytest -m "not network"     # fast checks, ~20 s, in-memory database
.venv\Scripts\python -m pytest                      # full run incl. real video + storage, ~8 min
cd ..\frontend && npm run build                     # rebuild the dashboard the API serves
scripts\start.ps1 / scripts\stop.ps1                # run / stop in the background
```
Restart with `stop.ps1` then `start.ps1` after backend changes (the worker is a separate process).

## Hard rules
1. **Secrets.** Keys live only in `backend/.env` (gitignored). Never put a key, token or bucket name in
   source, tests, docs, logs, API responses or the frontend. Never print a key back. The tests check this.
2. **Storage is invisible to users.** Clips are held in remote S3-compatible storage (see docs section 9).
   Do not add UI, settings, messages, paths or "free disk" that reveal it. `GET/PUT /api/settings` hide
   `storage`; clip and job responses omit `file`/`thumb`/`files`.
3. **Not topic-specific.** The product works for any video. No game, show or channel names in prompts,
   defaults, copy or personas.
4. **Honest claims.** Scores rank clips; they never promise views. Do not add copy that does.
5. **UI quality.** Follow the design system in `frontend/app/globals.css` and `components/ui.tsx`
   (light theme only; one accent; hairline borders plus very soft shadows; navy text; 12/13/14/16/20/24 type scale; 4 px grid; use the -ink text shades for green/amber/red text). Every list needs loading,
   empty and error states; every control needs hover, focus-visible, disabled, loading. Mobile first. No
   gradients, glass, emoji, filler marketing copy. Plain verbs, sentence case, errors say how to fix.
6. **Next.js here has breaking changes.** Read `frontend/AGENTS.md` and
   `frontend/node_modules/next/dist/docs/` before frontend work. It is a static export (`output: "export"`).
7. **Windows details.** Keep ffmpeg filter paths relative (run with `cwd`), use `CREATE_NO_WINDOW`, pass
   user text to PowerShell through environment variables, never into the script text.
8. **Tests.** Change behaviour, change a test. Run the offline suite before finishing; run the full suite
   when pipeline, storage or posting changed.
9. **Git.** Do not commit, push or open PRs unless the owner asks. Never add `Co-Authored-By`, "Generated
   with" lines or session links to commits or PRs.

10. **Everyone is an ordinary user; nothing is shared.** There is no administrator. Every job, clip
   and post has a `user_id`; read and change work only through `_job_for`, `_clip_for` and `_mine` in
   `main.py` so one person never reaches another's work (a stranger's id returns 404). Settings, AI
   keys and the YouTube channel are per person (`db.settings_for`, `keys.py`, `youtube.py`). The only
   shared secret is the platform `NVIDIA_API_KEY`. Do not add pages or routes that list other people
   or their work. New routes that touch jobs, clips, posts, keys or settings need a test.
11. **The server keeps nothing.** Data is in MongoDB (`db.py`; documents have a string `id`), files are
   in S3-compatible storage (`storage.py`). The job folder under `CLIPPER_DATA_DIR` is scratch space
   that `runner.persist_job_files` empties when a job finishes. Never add something that must survive
   on the server's own disk. Keys and tokens are encrypted with `APP_SECRET_KEY` (`keys.py`).

## Things easy to get wrong
- Jobs resume from files in `data/jobs/<id>/`; a step must write its result before the next step reads it.
- A clip's `file`/`thumb` are `s3:<key>` (remote) or a path relative to `data/` (local): use
  `storage.is_remote`, `storage.local_copy`, `storage.delete`, never assume a local path.
- YouTube decides Short vs video from the file (vertical/square and up to 3 min = Short); see `main.clip_kind`.
- Scheduling a YouTube upload needs `privacyStatus=private` plus `publishAt`.
- Hook-writing rules live in one place: `selection.HOOK_RULES`.
