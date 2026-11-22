# Clipper

Turns long YouTube videos and live streams into captioned clips that are ready to post.
The engine runs on this PC in the background; finished clips are kept in private storage.
Full documentation for developers and coding agents: `docs/DOCUMENTATION.md` (rules in `AGENTS.md`).

## Start and stop

```powershell
scripts\start.ps1        # starts the engine in the background and opens http://127.0.0.1:8765
scripts\stop.ps1         # stops it; unfinished jobs resume on the next start
scripts\autostart.ps1    # optional: start with Windows (-Remove to undo)
```

Closing the browser does not stop anything. A separate worker process does the
work and shows a Windows notification when clips are ready. The PC must stay
on; the worker keeps Windows awake while jobs are running.

## What it does

1. **Paste a link** on Home. A finished video is never downloaded whole: its sound is analysed and each clip fetches only its own part. A stream that is live right now is watched until it ends, or until it has the number of clips you asked for.
2. **Find moments.** Speech is transcribed on the graphics card. Loudness, sudden jumps, speaking speed and YouTube's "most replayed" data mark candidate moments.
3. **AI picks and trims** the candidates, writes a title, on-screen hook, description and hashtags, guided by the current trend profile.
4. **Render.** Each clip is cut in the chosen format (9:16, 16:9, 1:1) and quality, with word-by-word captions, the hook and a credit to the source channel.
5. **Audience panel.** Up to 100 simulated viewers react to each clip; their answers become a 0-100 score used to rank clips.
6. **Post** now or on a schedule to your connected YouTube channel, or download the file.

## Settings

Clip count and length, format, layout, quality, frame rate, caption style, AI
engine and model, audience panel size, live-stream sensitivity, posting
defaults and trend searches are all under **Settings**.

### Accounts

The public landing page leads to Google sign-in. Everyone who signs in is an ordinary user with a
private workspace; there is no administrator and no page that lists other people. Browser sessions use
an opaque HttpOnly cookie.

Google login uses a dedicated **Web application** OAuth client in `backend/.env` as
`GOOGLE_LOGIN_CLIENT_ID` and `GOOGLE_LOGIN_CLIENT_SECRET`. YouTube connection separately uses the
**Desktop app** values `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Credentials cannot be entered
through the browser or returned by the API.

### AI engine and keys

The default engine is NVIDIA Nemotron Ultra. Claude, Gemini and local Ollama
can be selected in Settings. Keys are stored only in `backend/.env`, which git
ignores. The app never shows a saved key again: Settings displays only whether
one is set and its last four characters. Replace a key in Settings or by
editing `backend/.env`.

### YouTube posting

Settings has the four steps for connecting a channel. Two limits come from
YouTube, not from this app: until Google has audited your Cloud project,
videos uploaded through the API stay private, and the number of uploads per day is
capped by quota. Downloading a clip and uploading it by hand always works.

## Tests

```powershell
cd backend
.venv\Scripts\python -m pytest                    # everything, about 6 minutes
.venv\Scripts\python -m pytest -m "not network"   # offline checks, about 20 seconds
```

The full run processes the first 6 minutes of a real video end to end and
leaves the finished clips in `data\test-run\jobs\e2e\clips` for you to watch.
Use another video with `set CLIPPER_TEST_URL=https://youtu.be/...`.

## First-time setup on another PC

Needs Python 3.11+, Node 20+ and ffmpeg on PATH. An NVIDIA card makes
transcription and rendering much faster; without one it falls back to the CPU.

```powershell
cd backend
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # then add your key
cd ..
scripts\start.ps1
```

Open the dashboard and sign in with Google. Everyone who signs in has their own private workspace,
their own settings and AI keys, and connects their own YouTube channel. There is no administrator.
Accounts, jobs and clips are kept in MongoDB (`MONGODB_URI` in `backend/.env`) and videos in the
storage you configure, so the server itself keeps nothing.

## Layout

```
backend/app/
  main.py        dashboard API (queues jobs, never does heavy work)
  worker.py      background process that runs the jobs
  runner.py      steps for a pasted video; resumes from the last finished step
  live.py        live-stream watcher
  llm.py         one interface over NVIDIA, Claude, Gemini, Ollama
  youtube.py     channel sign-in, upload, schedule, view counts
  pipeline/      download, transcribe, signals, selection, captions, render, panel, trends
backend/tests/   test_e2e.py
frontend/        Next.js dashboard, exported to static files and served by the API
scripts/         start, stop, autostart
data/            videos, clips, database, tokens (not in git)
```

## Things to know

- **No tool can promise views.** Scores rank clips against each other; they are not a forecast.
- **Other people's footage.** YouTube can withhold monetisation for reused content, and creators can file copyright claims. Clip creators who allow it, keep the credit on, and add something of your own.
- **Live chat is not read.** Live detection uses the stream's audio and speech only.
- **The audience panel reads, it does not watch.** Personas see the transcript, title and hook, not the picture.

To develop the dashboard with hot reload: `cd frontend; npm run dev` (uses the API on port 8765).

## Use it from an iPhone (home-screen app)

Clipper is an installable web app, so there is nothing to download from the App Store.

1. On the iPhone, open the Clipper address in **Safari** (it must be Safari, not Chrome).
2. Tap the **Share** button, then **Add to Home Screen**, then **Add**.
3. Open it from the home screen: it runs full screen with its own icon and name.

The phone is only a window into the engine. The engine (download, transcription, rendering) runs on
whichever computer or server Clipper is installed on, and that machine must be on and reachable from the
phone. To use it with your own laptop switched off, run the engine on an always-on server (see
`docs/DOCUMENTATION.md`, section 19).
