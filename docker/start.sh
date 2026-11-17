#!/bin/sh
# Starts the app. First, unless a proxy is already configured, it puts the server's YouTube traffic
# behind Cloudflare WARP (free, no account needed: a new anonymous device is registered at start).
# YouTube refuses most hosting servers' own addresses; a WARP address is accepted far more often.
# Set CLIPPER_WARP=0 to turn this off, or YTDLP_PROXY to use your own proxy instead.
set -u

if [ "${CLIPPER_WARP:-1}" = "1" ] && [ -z "${YTDLP_PROXY:-}" ]; then
  mkdir -p /tmp/warp
  cd /tmp/warp
  if [ ! -f wgcf-profile.conf ]; then
    for attempt in 1 2 3; do
      if wgcf register --accept-tos >/dev/null 2>&1 && wgcf generate >/dev/null 2>&1; then break; fi
      sleep 3
    done
  fi
  if [ -f wgcf-profile.conf ]; then
    printf 'WGConfig = /tmp/warp/wgcf-profile.conf\n\n[Socks5]\nBindAddress = 127.0.0.1:40000\n' > wp.conf
    # Keep the proxy running: if it stops, start it again.
    ( while true; do wireproxy -c /tmp/warp/wp.conf >/dev/null 2>&1; sleep 2; done ) &
    export YTDLP_PROXY="socks5://127.0.0.1:40000"
    # With a way to reach YouTube, this server's worker takes the link jobs itself.
    export CLIPPER_WORKER_ACCEPTS=all
    echo "WARP proxy ready: YouTube links are downloaded through it."
  else
    echo "WARP could not be set up; YouTube links use this server's own address."
  fi
fi

cd /app/backend
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}" --proxy-headers --forwarded-allow-ips='*' --no-access-log
