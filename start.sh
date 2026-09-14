#!/bin/sh
# Single startup path for local Docker and for Railway.
#
# "exec" replaces this shell with uvicorn so that SIGTERM from the platform goes
# straight to the server and shutdown hooks (worker stop, database close) run.
set -eu

PORT="${PORT:-8080}"
DATABASE_PATH="${DATABASE_PATH:-/data/app.db}"
DB_DIR="$(dirname "$DATABASE_PATH")"

mkdir -p "$DB_DIR" 2>/dev/null || true

if [ ! -w "$DB_DIR" ]; then
  echo "FATAL: $DB_DIR is not writable. On Railway, attach a volume mounted at /data and set DATABASE_PATH=/data/app.db." >&2
  exit 1
fi

echo "starting on port ${PORT}, database ${DATABASE_PATH}"

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
