#!/bin/sh
set -e
# Ensure data dir exists and owned by appuser for SQLite WAL files
mkdir -p /app/data
chown -R appuser:appuser /app/data 2>/dev/null || true
# Optional: show DB path
# ls -ld /app/data || true
# Run the bot as appuser
exec su -s /bin/sh -c "python src/bot.py" appuser
