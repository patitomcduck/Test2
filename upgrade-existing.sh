#!/usr/bin/env bash
set -euo pipefail
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${1:-}" ]]; then TARGET="$1";
elif [[ -d "$HOME/docker/collector-pos-clean" ]]; then TARGET="$HOME/docker/collector-pos-clean";
else TARGET="$HOME/docker/collector-pos"; fi

echo "Actualizando Collector POS en: $TARGET"
if [[ ! -d "$TARGET" ]]; then echo "ERROR: no existe $TARGET"; exit 1; fi
mkdir -p "$TARGET/backups-upgrade"
STAMP="$(date +%Y%m%d-%H%M%S)"
if [[ -f "$TARGET/data/pos.db" ]]; then cp -f "$TARGET/data/pos.db" "$TARGET/backups-upgrade/pos-$STAMP.db"; fi
if [[ -f "$TARGET/.env" ]]; then cp -f "$TARGET/.env" "$TARGET/backups-upgrade/env-$STAMP"; fi
for f in app.py pricing_engine.py price_scheduler.py backup_scheduler.py requirements.txt Dockerfile README.md PATCH_NOTES.txt VERSION configure-justtcg.sh; do cp -f "$SOURCE/$f" "$TARGET/$f"; done
rm -rf "$TARGET/static" "$TARGET/templates"
cp -a "$SOURCE/static" "$TARGET/static"
cp -a "$SOURCE/templates" "$TARGET/templates"
# Preserve the currently configured host port when possible.
PORT="$(grep -Eo '"[0-9]+:8080"' "$TARGET/compose.yaml" 2>/dev/null | head -1 | tr -d '"' | cut -d: -f1 || true)"
PORT="${PORT:-8088}"
sed "s/8088:8080/${PORT}:8080/" "$SOURCE/compose.yaml" > "$TARGET/compose.yaml"
cd "$TARGET"
docker compose up -d --build
printf '\n✓ Collector POS actualizado a V2.3 Pilot\n'
printf '✓ Base de datos y .env conservados\n'
printf '✓ Respaldo automático diario habilitado\n'
printf 'Abre el mismo puerto que ya utilizabas.\n'
