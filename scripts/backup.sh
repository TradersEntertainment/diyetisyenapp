#!/bin/sh
# pg_dump backup of the diyetisyen database.
# Usage: scripts/backup.sh [output_dir]
# Reads DATABASE_URL (postgresql+asyncpg://user:pass@host:port/db) or the POSTGRES_* vars.
set -eu

OUT_DIR="${1:-backups}"
mkdir -p "$OUT_DIR"

URL="${DATABASE_URL:-postgresql+asyncpg://diyetisyen:diyetisyen@db:5432/diyetisyen}"
# strip the sqlalchemy driver suffix so pg_dump understands the URL
PG_URL=$(echo "$URL" | sed 's/postgresql+asyncpg/postgresql/')

STAMP=$(date +%Y%m%d_%H%M%S)
FILE="$OUT_DIR/diyetisyen_$STAMP.sql.gz"

pg_dump --dbname="$PG_URL" | gzip > "$FILE"
echo "backup written: $FILE"

# keep the 30 most recent backups
ls -1t "$OUT_DIR"/diyetisyen_*.sql.gz 2>/dev/null | tail -n +31 | xargs -r rm -f
