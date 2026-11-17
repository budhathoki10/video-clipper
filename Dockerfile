# Clipper for a Linux host (Render, any VPS). One container runs the dashboard, the API and the
# background worker (the API starts and supervises the worker itself).
#
# Build:  docker build -t clipper .
# Run:    docker run -p 10000:10000 --env-file backend/.env clipper

# --- 1. build the dashboard (static export) ---------------------------------------------------
FROM node:22-slim AS dashboard
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY frontend/ ./
RUN npm run build

# --- 2. the app -------------------------------------------------------------------------------
FROM python:3.13-slim

# CPU-only host: skip the GPU attempt and use the CPU transcription model straight away.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CLIPPER_DATA_DIR=/data \
    CLIPPER_FORCE_CPU=1

# ffmpeg renders the clips; fontconfig lets it find the caption fonts; nodejs lets yt-dlp solve
# YouTube's challenges.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig nodejs ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Cloudflare WARP without a VPN driver: wgcf registers a free anonymous device and wireproxy turns it
# into a local SOCKS proxy that yt-dlp uses (see docker/start.sh).
ARG WGCF_VERSION=2.3.1
ARG WIREPROXY_VERSION=1.1.3
RUN curl -fsSL -o /usr/local/bin/wgcf \
      "https://github.com/ViRb3/wgcf/releases/download/v${WGCF_VERSION}/wgcf_${WGCF_VERSION}_linux_amd64" \
    && chmod +x /usr/local/bin/wgcf \
    && curl -fsSL "https://github.com/windtf/wireproxy/releases/download/v${WIREPROXY_VERSION}/wireproxy_linux_amd64.tar.gz" \
       | tar -xz -C /usr/local/bin wireproxy \
    && chmod +x /usr/local/bin/wireproxy

WORKDIR /app/backend
COPY backend/requirements.txt ./
RUN pip install -r requirements.txt && pip install "yt-dlp[default]"

COPY backend/app ./app
COPY backend/assets ./assets
COPY --from=dashboard /build/frontend/out /app/frontend/out
COPY docker/start.sh /app/start.sh
RUN sed -i 's/\r$//' /app/start.sh && chmod +x /app/start.sh

# Scratch space for the video being processed. Everything lasting is in MongoDB and the storage.
RUN mkdir -p /data
EXPOSE 10000

# docker/start.sh sets up the YouTube proxy, then runs uvicorn (with --proxy-headers, because behind a
# hosting proxy the real scheme and client arrive in X-Forwarded-* headers).
CMD ["/app/start.sh"]
