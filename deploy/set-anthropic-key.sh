#!/usr/bin/env bash
# Cambia la ANTHROPIC_API_KEY de la VM sin que pase por ningun chat ni historial.
#   gcloud compute ssh --zone asia-northeast1-a instance-20261016-191100 --project vocal-operand-271906 \
#     -- -t 'bash ~/heatsignal/deploy/set-anthropic-key.sh'
# 1) pide la key sin mostrarla, 2) la prueba con un pedido minimo, 3) solo si funciona la guarda en .env
#    (permisos 600) y reinicia el servicio. Si la prueba falla, no toca nada.
set -euo pipefail
ENV_FILE="$HOME/heatsignal/.env"

read -rsp "Pega la API key nueva de Anthropic (no se muestra) y Enter: " KEY
echo
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
if [[ "$KEY" != sk-ant-* ]]; then
  echo "No parece una API key de Anthropic (tiene que empezar con sk-ant-). No se cambio nada."
  exit 1
fi

echo "Probando la key con un pedido minimo..."
RESP="$(curl -s https://api.anthropic.com/v1/messages \
  -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"claude-haiku-5-5","max_tokens":5,"messages":[{"role":"user","content":"ok"}]}')"
if ! printf '%s' "$RESP" | grep -q '"type":"message"'; then
  echo "La key NO funciona. Respuesta de Anthropic:"
  printf '%s\n' "$RESP" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin); print(" ", (d.get("error") or {}).get("message", d))
except Exception:
    print("  (respuesta ilegible)")'
  echo "No se cambio nada."
  exit 1
fi
echo "La key funciona."

umask 077
touch "$ENV_FILE"
grep -v '^ANTHROPIC_API_KEY=' "$ENV_FILE" > "$ENV_FILE.tmp" || true
printf 'ANTHROPIC_API_KEY=%s\n' "$KEY" >> "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
chmod 600 "$ENV_FILE"
unset KEY RESP

sudo systemctl restart heatsignal-ingestion
sleep 5
echo "Servicio: $(systemctl is-active heatsignal-ingestion). Listo: el primer ciclo con LLM corre ~30 min despues del reinicio."
