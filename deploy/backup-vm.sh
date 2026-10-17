#!/usr/bin/env bash
# Snapshot completo de los datos de HeatSignal en la VM -> Drive (gdrive:backups/vm/<UTC>/).
# Copia consistente de cada SQLite con la API de backup (sirve con la base en uso), comprimida con gzip,
# mas los JSON publicados y las exportaciones diarias. No borra nada en Drive (rclone copy).
set -euo pipefail
DATA="$HOME/heatsignal/data"
STAMP="$(date -u +%Y-%m-%dT%H%MZ)"
# La copia temporal de las bases va al mismo disco: si no hay lugar para la mas grande x1.5 (+256 MB), no se arranca
# (con el disco lleno es peor llenarlo del todo; el archivado por niveles libera espacio primero).
NEED=$(( $(du -cb "$DATA"/*.sqlite | tail -1 | cut -f1) * 3 / 2 + 268435456 ))
FREE=$(df -B1 --output=avail "${TMPDIR:-/tmp}" | tail -1 | tr -d ' ')
if [ "$FREE" -lt "$NEED" ]; then
  echo "sin espacio para el respaldo: libres $FREE B, hacen falta ~$NEED B" >&2
  exit 1
fi
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
