#!/usr/bin/env bash
# Grava /root/leveling-secrets/secrets.env na hel1. Nenhum valor passa pela tela: só os nomes das
# chaves no fim. Rodar de novo é seguro (sobrescreve de forma atômica); o container relê sozinho.
#
#   install-secrets.sh                    pergunta cada valor sem eco (Enter mantém o atual)
#   install-secrets.sh --ensure-data-key  não pergunta nada: só gera a DATA_KEY se faltar
#   install-secrets.sh --wuzapi           não pergunta nada: copia URL e token da SESSÃO da WuzAPI
#                                         do MegaPromoOz (o token de admin não sai de lá)
#
#   GAME_USERNAME         caçador do dono (vazio = o dono cadastra pelo painel, como os jogadores)
#   GAME_PASSWORD         senha do jogo do dono
#   GOOGLE_CLIENT_SECRET  segredo do cliente OAuth "Leveling Keeper" (Google Cloud)
#   DATA_KEY              chave Fernet das senhas cadastradas pelo painel. Gerada aqui, uma vez;
#                         NUNCA trocar: as senhas já guardadas deixam de abrir (o painel pede de novo)
#   WUZAPI_BASE_URL       WuzAPI interna (rede chatbot-net), para o pedido de acesso no WhatsApp
#   WUZAPI_SESSION_TOKEN  token da sessão do MegaPromoOz (a mesma que o sesi-vagas usa)
set -euo pipefail

DIR=/root/leveling-secrets
OUT=$DIR/secrets.env
APP_UID=1001  # usuário do container (Dockerfile)
WUZAPI=${WUZAPI_ENV:-/root/.megapromooz-wuzapi.env}
ASKED=(GAME_USERNAME GAME_PASSWORD GOOGLE_CLIENT_SECRET)
COPIED=(WUZAPI_BASE_URL WUZAPI_SESSION_TOKEN)
KEYS=("${ASKED[@]}" DATA_KEY "${COPIED[@]}")

MODE=ask
case "${1:-}" in
  "") ;;
  --ensure-data-key) MODE=ensure ;;
  --wuzapi) MODE=wuzapi ;;
  *) echo "uso: $0 [--ensure-data-key | --wuzapi]" >&2; exit 2 ;;
esac

current() {  # chave -> valor atual do arquivo, sem ecoar
  [ -f "$OUT" ] || return 0
  local line
  line=$(grep -E "^$1=" "$OUT" | tail -n1 || true)
  printf '%s' "${line#*=}"
}

declare -A values
for key in "${KEYS[@]}"; do
  values[$key]=$(current "$key")
done

if [ "$MODE" = ask ]; then
  for key in "${ASKED[@]}"; do
    hint=""
    [ -n "${values[$key]}" ] && hint=" (Enter mantém o atual)"
    read -r -s -p "$key$hint: " value
    echo
    if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
      echo "ABORT: $key com quebra de linha"; exit 1
    fi
    [ -n "$value" ] && values[$key]=$value
  done
fi

if [ "$MODE" = wuzapi ]; then
  [ -r "$WUZAPI" ] || { echo "ABORT: $WUZAPI não existe"; exit 1; }
  for key in "${COPIED[@]}"; do
    line=$(grep -E "^$key=" "$WUZAPI" | tail -n1 || true)
    line=${line#*=}
    line=${line%\"}; line=${line#\"}; line=${line%\'}; line=${line#\'}
    [ -n "$line" ] || { echo "ABORT: $key vazio em $WUZAPI"; exit 1; }
    values[$key]=$line
  done
fi

generated=""
if [ -z "${values[DATA_KEY]}" ]; then
  # Fernet: 32 bytes aleatórios em base64 url-safe (44 caracteres, com o "=").
  values[DATA_KEY]=$(head -c 32 /dev/urandom | base64 | tr '+/' '-_')
  generated=" (DATA_KEY gerada agora)"
fi

[ -n "${values[GOOGLE_CLIENT_SECRET]}" ] || echo "aviso: GOOGLE_CLIENT_SECRET vazio — o login do painel não funciona"

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
echo "ok: $OUT com $(cut -d= -f1 "$OUT" | paste -sd, -)$generated"
