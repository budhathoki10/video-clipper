# Deploying Clipper on Render

One Render service runs everything: the dashboard, the API and the background worker. The repository
contains the `Dockerfile` and `render.yaml` it needs. Nothing secret is stored in the repository: you
enter keys in Render's dashboard.

## What to expect

- **Plan.** `render.yaml` asks for the `standard` plan (about 2 GB of memory) with a 10 GB disk. Check
  Render's current prices. The free plan will not work for this app: it sleeps, it has no disk (the
  database, sign-ins and the YouTube connection are erased on every restart) and it has too little
  memory for video work.
- **Speed.** There is no GPU. Transcription uses the CPU model (`small`) and encoding uses x264, so a
  long video takes much longer than on a PC with a graphics card.
- **Downloads.** YouTube often blocks downloads from data-centre addresses. If a job fails with a
  download error on Render but the same link works on your PC, that is the cause.
- **Keys changed in Settings** are written to the container, not to Render. They are lost on the next
  deploy. Change keys in Render's Environment tab.

## 1. Google Cloud (about 5 minutes)

In the project you already use for sign-in:

1. Enable **YouTube Data API v3**.
2. Open the **Web application** OAuth client (the one behind `GOOGLE_LOGIN_CLIENT_ID`) and add two
   *Authorized redirect URIs*, using your final address (shown by Render after the first deploy, or
   your custom domain):
   - `https://YOUR-APP.onrender.com/api/auth/google/callback`
   - `https://YOUR-APP.onrender.com/api/youtube/callback`
3. Under *Audience*, add your own Google account (and the one that owns the YouTube channel) as test
   users while the app is in testing mode.

## 2. Create the service

1. Push the repository to GitHub (done: `main`).
2. In Render: **New > Blueprint**, choose the repository. Render reads `render.yaml`.
3. Fill in the values it asks for (see the table). Everything marked secret stays in Render only.
4. Deploy. The first build takes several minutes (it builds the dashboard and installs Python
   packages).

| Variable | What to enter |
|---|---|
| `ALLOWED_EMAILS` | Your Google email, plus anyone else who may sign in (comma separated) |
| `GOOGLE_LOGIN_CLIENT_ID`, `GOOGLE_LOGIN_CLIENT_SECRET` | From the Web application OAuth client |
| `NVIDIA_API_KEY` | Your NVIDIA key (or leave empty and use another provider) |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` | Only if you use those providers |
| `BLOMP_ACCESS_KEY_ID`, `BLOMP_SECRET_ACCESS_KEY`, `BLOMP_BUCKET` | Where finished clips are kept |

`PUBLIC_URL` is set automatically from Render's own address (`RENDER_EXTERNAL_URL`). Add a `PUBLIC_URL`
environment variable yourself only if you use a custom domain.

## 3. First sign-in

Open the address, press **Continue with Google** and sign in with an address in `ALLOWED_EMAILS`. The
first account to sign in is the administrator. Then open **Settings > Posting > Connect YouTube**. Only
the administrator can connect or disconnect the channel on a hosted app.

If the sign-in says the address is not allowed, or that sign-up is closed, check `ALLOWED_EMAILS` in
Render's Environment tab.

## 4. Keeping it awake (only if you do not use a paid always-on plan)

`GET https://YOUR-APP.onrender.com/api/ping` returns `{"ok": true}`, needs no sign-in and does no
work. Point an external scheduler at it (for example cron-job.org). Free instances sleep after about
15 minutes without traffic, so every 10 to 14 minutes is enough. This only keeps the page awake; it
does not give a free instance a disk or more memory.

## 5. Phone home screen

Open the address in Safari, choose Share, then **Add to Home Screen**. Sign in again inside the
home-screen app (it does not share Safari's login).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Google says `redirect_uri_mismatch` | The two redirect URIs in step 1 do not match your address exactly (scheme, host, no trailing slash) |
| "Sign-up is closed" | `ALLOWED_EMAILS` is empty |
| Jobs fail while downloading | YouTube is blocking the server's address |
| Everything resets after a restart | The service is on a plan without a disk, or `CLIPPER_DATA_DIR` is not `/data` |
| Build fails | Open the build log in Render; `docker build -t clipper .` reproduces it locally |
