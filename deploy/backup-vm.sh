#!/usr/bin/env bash
# Snapshot completo de los datos de HeatSignal en la VM -> Drive (gdrive:backups/vm/<UTC>/).
# Copia consistente de cada SQLite con la API de backup (sirve con la base en uso), comprimida con gzip,
# mas los JSON publicados y las exportaciones diarias. No borra nada en Drive (rclone copy).
set -euo pipefail
DATA="$HOME/heatsignal/data"
STAMP="$(date -u +%Y-%m-%dT%H%MZ)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
DEST="gdrive:backups/vm/$STAMP"
for db in "$DATA"/*.sqlite; do
  name="$(basename "$db")"
  python3 - "$db" "$TMP/$name" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=60)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
  gzip -6 "$TMP/$name"
  echo "  $name -> $(du -h "$TMP/$name.gz" | cut -f1)"
done
cp "$DATA"/*.json "$TMP"/ 2>/dev/null || true
rclone copy "$TMP" "$DEST" --transfers 2
[ -d "$DATA/export" ] && rclone copy "$DATA/export" "gdrive:" --exclude "*.tmp" --transfers 2
echo "respaldo VM listo en $DEST"
