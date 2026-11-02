#!/usr/bin/env bash
# Deploy do Leveling Keeper na hel1: StackUpdate do Portainer com o YAML do repo e
# LEVELING_TAG=<tag> (dry-run antes). Rodado pelo Woodpecker (.woodpecker/deploy.yaml, dentro de
# `hel1-deploy exec`) ou à mão como break-glass / rollback:
#
#   infra/deploy.sh <sha12>
#
# Sem migração: o estado é um JSON no volume. Segredo só do host: /root/.portainer-token.
set -euo pipefail

TAG="${1:?usage: deploy.sh <image-tag>}"
[ "$TAG" != latest ] || { echo "refusing :latest; pass the commit tag being deployed" >&2; exit 2; }
cd "$(dirname "$0")/.."
read -r -a UPDATER <<<"${PORTAINER_STACK_UPDATE:-portainer-stack-update}"

"${UPDATER[@]}" --stack leveling --yaml infra/docker-stack.yml --set-env LEVELING_TAG="$TAG" --dry-run
"${UPDATER[@]}" --stack leveling --yaml infra/docker-stack.yml --set-env LEVELING_TAG="$TAG"
