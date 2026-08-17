#!/usr/bin/env bash
set -euo pipefail
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-$HOME/docker/collector-pos-clean}"
PORT="${COLLECTOR_POS_PORT:-8089}"

if [[ -e "$TARGET/data/pos.db" ]]; then
  echo "ERROR: $TARGET ya contiene una base de datos."
  echo "No se sobrescribió nada. Usa otra ruta o elimina esa instalación conscientemente."
  exit 1
fi

mkdir -p "$TARGET" "$TARGET/data"
for f in app.py pricing_engine.py price_scheduler.py backup_scheduler.py requirements.txt Dockerfile .env.example README.md PATCH_NOTES.txt VERSION configure-justtcg.sh; do cp -f "$SOURCE/$f" "$TARGET/$f"; done
rm -rf "$TARGET/static" "$TARGET/templates"
cp -a "$SOURCE/static" "$TARGET/static"
cp -a "$SOURCE/templates" "$TARGET/templates"

# Compose con puerto configurable para no chocar con una instalación existente.
sed "s/8088:8080/${PORT}:8080/" "$SOURCE/compose.yaml" > "$TARGET/compose.yaml"

SECRET=""
if command -v python3 >/dev/null 2>&1; then
  SECRET="$(python3 - <<'PY2'
import secrets
print(secrets.token_hex(32))
PY2
)"
elif command -v openssl >/dev/null 2>&1; then
  SECRET="$(openssl rand -hex 32)"
else
  echo "ERROR: se necesita python3 u openssl para generar SECRET_KEY."
  exit 1
fi

echo
echo "Configuración de JustTCG"
echo "JustTCG se usa para One Piece, Magic, Yu-Gi-Oh!, Lorcana y como respaldo de Pokémon."
read -rsp "Pega la API key de JustTCG (no se mostrará en pantalla): " JUSTTCG_KEY
echo
if [[ -z "$JUSTTCG_KEY" ]]; then
  echo "AVISO: no ingresaste una clave. Pokémon seguirá funcionando con TCGdex, pero otros TCG no podrán consultarse hasta configurar JustTCG."
else
  echo "✓ Clave JustTCG recibida. Se guardará solo en esta instalación."
fi

cat > "$TARGET/.env" <<EOF2
SECRET_KEY=$SECRET
JUSTTCG_API_KEY=$JUSTTCG_KEY
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
SMTP_FROM=
SMTP_USE_TLS=1
EOF2
chmod 600 "$TARGET/.env"

cd "$TARGET"
docker compose up -d --build

echo
echo "Collector POS V2.3 Pilot instalado."
echo "Abre: http://IP-DEL-EQUIPO:${PORT}"
if [[ -n "$JUSTTCG_KEY" ]]; then
  echo "JustTCG: configurado ✓"
else
  echo "JustTCG: pendiente (ejecuta ./configure-justtcg.sh cuando tengas la clave)"
fi
echo "Primero crea el administrador y luego entra a Configuración para personalizar la tienda."
