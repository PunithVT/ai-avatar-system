#!/usr/bin/env bash
# Backend container entrypoint — runs DB migrations then starts the server.
set -e

echo "[startup] Waiting for PostgreSQL to accept connections..."
until python -c "
import os, sys
try:
    import psycopg2
    url = os.environ.get('DATABASE_URL', '')
    # parse minimal connection params from the URL
    import urllib.parse as up
    r = up.urlparse(url)
    psycopg2.connect(
        host=r.hostname, port=r.port or 5432,
        dbname=r.path.lstrip('/'), user=r.username, password=r.password,
        connect_timeout=3,
    ).close()
    sys.exit(0)
except Exception as e:
    sys.exit(1)
" 2>/dev/null; do
    echo "[startup] PostgreSQL not ready yet — retrying in 2s..."
    sleep 2
done
echo "[startup] PostgreSQL is ready."

echo "[startup] Running database migrations (alembic upgrade head)..."
if ! alembic upgrade head; then
    echo "[startup] ERROR: Alembic migrations failed. Check logs above." >&2
    exit 1
fi
echo "[startup] Migrations applied successfully."

# Ensure runtime directories exist
mkdir -p voice_profiles /tmp/avatars /tmp/videos /tmp/audio

echo "[startup] Starting uvicorn..."
exec uvicorn main:app --host 0.0.0.0 --port 8000 --workers "${UVICORN_WORKERS:-4}"
