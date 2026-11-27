# --- Stage 1: build the bgutil PO Token provider server (Node.js) ---
FROM node:22-bookworm-slim AS potbuild
WORKDIR /pot
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/2.0.2.tar.gz \
       | tar xz --strip-components=1
WORKDIR /pot/server
RUN npm ci && npx tsc

# --- Stage 2: runtime ---
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# node runtime: used by yt-dlp's EJS challenge solver and the POT server
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Patched EJS challenge solver (fix for yt-dlp/ejs#79, built from ejs master).
# Overwrites the released 0.8.0 solver scripts, which break on the newer
# YouTube player served to some accounts.
COPY vendor/ejs/core.min.js vendor/ejs/lib.min.js \
     /usr/local/lib/python3.12/site-packages/yt_dlp_ejs/yt/solver/

# PO Token provider server (plugin half was installed via requirements.txt)
COPY --from=potbuild /pot/server /pot/server

COPY bot.py start.sh ./
CMD ["sh", "start.sh"]
