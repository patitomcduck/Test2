#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:-$HOME/docker/collector-pos}"
if [[ "${2:-}" != "--CONFIRMAR-BORRADO" ]]; then
  echo "Este comando BORRA datos y personalización de: $TARGET"
  echo "Hace un respaldo primero. Para continuar:"
  echo "$0 "$TARGET" --CONFIRMAR-BORRADO"
  exit 1
fi
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$TARGET/backups/antes-de-limpiar-$STAMP"
mkdir -p "$BACKUP"
[[ -f "$TARGET/data/pos.db" ]] && cp -a "$TARGET/data/pos.db" "$BACKUP/pos.db"
[[ -d "$TARGET/data/brand" ]] && cp -a "$TARGET/data/brand" "$BACKUP/brand"
[[ -f "$TARGET/.env" ]] && cp -a "$TARGET/.env" "$BACKUP/.env"
cd "$TARGET"
docker compose down || true
rm -f "$TARGET/data/pos.db"
rm -rf "$TARGET/data/brand"
rm -f "$TARGET/.env"
cp "$TARGET/.env.example" "$TARGET/.env"
SECRET="$(python3 - <<'PY2'
import secrets
print(secrets.token_hex(32))
PY2
)"
sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$SECRET/" "$TARGET/.env"
docker compose up -d --build
echo "Instalación limpiada. Respaldo: $BACKUP"
