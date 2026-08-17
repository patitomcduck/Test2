#!/usr/bin/env bash
set -euo pipefail
TARGET="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
ENVFILE="$TARGET/.env"

if [[ ! -f "$ENVFILE" ]]; then
  echo "ERROR: No encontré $ENVFILE"
  exit 1
fi

read -rsp "Pega la nueva API key de JustTCG (no se mostrará): " KEY
echo
if [[ -z "$KEY" ]]; then
  echo "No se hizo ningún cambio."
  exit 1
fi

python3 - "$ENVFILE" "$KEY" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); key=sys.argv[2]
lines=p.read_text().splitlines()
out=[]; found=False
for line in lines:
    if line.startswith('JUSTTCG_API_KEY='):
        out.append('JUSTTCG_API_KEY='+key); found=True
    else:
        out.append(line)
if not found:
    out.append('JUSTTCG_API_KEY='+key)
p.write_text('\n'.join(out)+'\n')
PY
chmod 600 "$ENVFILE"
cd "$TARGET"
docker compose up -d --force-recreate collector-pos price-updater

echo "✓ JustTCG actualizado y servicios reiniciados."
