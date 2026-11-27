#!/bin/sh
set -e
# PO Token provider server (bgutil) on localhost, required by the yt-dlp plugin.
node /pot/server/build/main.js --port 4416 &
exec python bot.py
