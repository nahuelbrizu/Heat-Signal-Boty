#!/usr/bin/env bash
# Genera freqtrade/config.hyp.secrets.json en la VM (API REST del Freqtrade de hipotesis, SOLO 127.0.0.1:8082).
#   gcloud compute ssh --zone asia-northeast1-a instance-20261016-191100 --project vocal-operand-271906 \
#     -- 'bash ~/heatsignal/deploy/gen-freqtrade-hyp-secrets.sh'
# - Usuario, clave, jwt_secret_key y ws_token aleatorios (modulo secrets de Python); permisos 600.
# - No imprime ningun secreto. Si el archivo ya existe no lo toca (borrarlo a mano para regenerar y reiniciar
#   heatsignal-freqtrade-hyp; el puente lo relee en cada corrida).
# - Ver la clave (para FreqUI, si algun dia se usa por tunel):
#   python3 -c 'import json;print(json.load(open("/home/nahue/heatsignal/freqtrade/config.hyp.secrets.json"))["api_server"]["password"])'
set -euo pipefail
OUT="$HOME/heatsignal/freqtrade/config.hyp.secrets.json"

if [[ -e "$OUT" ]]; then
  echo "Ya existe $OUT: no se cambio nada."
  exit 0
fi

umask 077
python3 - "$OUT" <<'EOF'
import json, secrets, sys
cfg = {"api_server": {
    "enabled": True,
    "listen_ip_address": "127.0.0.1",   # nunca 0.0.0.0: sin puerto abierto en el firewall
    "listen_port": 8082,
    "verbosity": "error",
    "enable_openapi": False,
    "jwt_secret_key": secrets.token_hex(32),
    "ws_token": secrets.token_urlsafe(24),
    "CORS_origins": [],
    "username": "hs-hyp-" + secrets.token_hex(3),
    "password": secrets.token_urlsafe(24),
}}
with open(sys.argv[1], "x", encoding="utf-8") as f:
    json.dump(cfg, f, indent=2)
EOF
chmod 600 "$OUT"
echo "Listo: $OUT (permisos 600, API en 127.0.0.1:8082). No se imprimio ningun secreto."
