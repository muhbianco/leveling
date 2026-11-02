#!/usr/bin/env bash
# Grava /root/leveling-secrets/secrets.env na hel1. Pergunta cada valor sem eco; Enter vazio mantém
# o valor que já está no arquivo. Nenhum valor passa pela tela: só os nomes das chaves no fim.
#   GAME_USERNAME         nome de caçador do Leveling Idle
#   GAME_PASSWORD         senha do jogo (vazio = o robô não entra sozinho; login pela tela remota)
#   GOOGLE_CLIENT_SECRET  segredo do cliente OAuth "Leveling Keeper" (Google Cloud)
# Rodar de novo é seguro (sobrescreve de forma atômica). O container relê o arquivo sozinho.
set -euo pipefail

DIR=/root/leveling-secrets
OUT=$DIR/secrets.env
APP_UID=1001  # usuário do container (Dockerfile)
KEYS=(GAME_USERNAME GAME_PASSWORD GOOGLE_CLIENT_SECRET)

current() {  # chave -> valor atual do arquivo, sem ecoar
  [ -f "$OUT" ] || return 0
  local line
  line=$(grep -E "^$1=" "$OUT" | tail -n1 || true)
  printf '%s' "${line#*=}"
}

declare -A values
for key in "${KEYS[@]}"; do
  old=$(current "$key")
  hint=""
  [ -n "$old" ] && hint=" (Enter mantém o atual)"
  read -r -s -p "$key$hint: " value
  echo
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "ABORT: $key com quebra de linha"; exit 1
  fi
  values[$key]=${value:-$old}
done

[ -n "${values[GOOGLE_CLIENT_SECRET]}" ] || echo "aviso: GOOGLE_CLIENT_SECRET vazio — o login do painel não funciona"
[ -n "${values[GAME_USERNAME]}" ] && [ -n "${values[GAME_PASSWORD]}" ] \
  || echo "aviso: sem usuário e senha do jogo — login só pela tela remota"

install -d -m 700 -o "$APP_UID" -g "$APP_UID" "$DIR"
tmp=$(mktemp "$DIR/.secrets.XXXXXX")
trap 'rm -f "$tmp"' EXIT
for key in "${KEYS[@]}"; do
  printf '%s=%s\n' "$key" "${values[$key]}"
done >"$tmp"
chown "$APP_UID:$APP_UID" "$tmp"
chmod 400 "$tmp"
mv -f "$tmp" "$OUT"
trap - EXIT
echo "ok: $OUT com $(cut -d= -f1 "$OUT" | paste -sd, -)"
