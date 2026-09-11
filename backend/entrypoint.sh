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

echo "[startup] Checking migration state..."
# If tables already exist but alembic has no history, stamp to current head
# so alembic doesn't try to re-create existing tables.
CURRENT=$(alembic current 2>/dev/null || true)
if [ -z "$CURRENT" ]; then
    # Check if the users table already exists (pre-alembic install)
    TABLES=$(python -c "
import os, psycopg2, urllib.parse as up
url = os.environ.get('DATABASE_URL','')
r = up.urlparse(url)
conn = psycopg2.connect(host=r.hostname, port=r.port or 5432,
    dbname=r.path.lstrip('/'), user=r.username, password=r.password)
cur = conn.cursor()
cur.execute(\"SELECT to_regclass('public.users')\")
print(cur.fetchone()[0] or '')
conn.close()
" 2>/dev/null || echo "")
    if [ "$TABLES" = "users" ]; then
        echo "[startup] Existing DB detected without alembic history — stamping to head..."
        alembic stamp head
    fi
fi

echo "[startup] Running database migrations (alembic upgrade head)..."
if ! alembic upgrade head; then
    echo "[startup] ERROR: Alembic migrations failed. Check logs above." >&2
    exit 1
fi
echo "[startup] Migrations applied successfully."

# Ensure runtime directories exist
mkdir -p voice_profiles /tmp/avatars /tmp/videos /tmp/audio

echo "[startup] Starting uvicorn..."
# --forwarded-allow-ips decides whose X-Forwarded-For we believe.
#
# uvicorn turns --proxy-headers on by default but only trusts 127.0.0.1, and
# nginx reaches this container over the compose network from a private
# address. So the header was silently discarded and request.client.host
# resolved to nginx's own IP on EVERY request. Two consequences:
#   * the per-IP rate-limit bucket collapsed into one global bucket, so a
#     single noisy client could 429 every other anonymous user;
#   * every access log line recorded the proxy address instead of the client.
#
# uvicorn 0.27 matches trusted hosts by exact string — it does NOT parse
# CIDR ranges (that landed in a later release), so a value like
# "172.16.0.0/12" would never match and would leave the bug in place. "*"
# is the working choice for this topology.
#
# SAFETY PRECONDITION: "*" means "trust X-Forwarded-For from anyone", which
# is only sound because this service is never reachable directly — neither
# docker-compose.yml nor docker-compose.prod.yml publishes a port for the
# backend, so nginx is the sole ingress. If you ever expose this container
# directly, override FORWARDED_ALLOW_IPS with the proxy's literal address:
# otherwise a client can spoof X-Forwarded-For and forge its rate-limit
# identity at will.
exec uvicorn main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --workers "${UVICORN_WORKERS:-4}" \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
