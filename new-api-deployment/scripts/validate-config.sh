#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

load_required_env
require_command docker
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"

compose config --quiet || die "docker compose config rejected the template"

for route in \
  "/v1/friend/preflight" \
  "/v1/friend/catalog" \
  "/v1/friend/balance" \
  "/v1/messages"; do
  grep -Fq "path $route" "$DEPLOY_DIR/Caddyfile" || die "missing exact public route: $route"
done

grep -Fq 'reverse_proxy friend-gateway:{$FRIEND_GATEWAY_PORT}' "$DEPLOY_DIR/Caddyfile" || die "Friend routes must terminate at friend-gateway"
grep -Fq '  friend-gateway:' "$DEPLOY_DIR/docker-compose.yml" || die "friend-gateway service is missing"
grep -Fq 'FRIEND_GATEWAY_MODE:' "$DEPLOY_DIR/docker-compose.yml" || die "friend-gateway mode is not wired"
grep -Fq 'FRIEND_GATEWAY_KEY_BINDINGS_FILE:' "$DEPLOY_DIR/docker-compose.yml" || die "server-side key binding file is not wired"

if grep -Eq 'path /internal|path /v1/(models|responses)' "$DEPLOY_DIR/Caddyfile"; then
  die "Caddyfile contains a forbidden public route"
fi

if grep -Fq 'reverse_proxy new-api:' "$DEPLOY_DIR/Caddyfile"; then
  die "Caddy must not blindly proxy Friend routes to new-api"
fi

if awk '
  /^  new-api:/ { in_service = 1; next }
  /^  [A-Za-z0-9_-]+:/ { in_service = 0 }
  in_service && /^    ports:/ { found = 1 }
  END { exit found ? 0 : 1 }
' "$DEPLOY_DIR/docker-compose.yml"; then
  die "new-api must not publish a host port"
fi

if awk '
  /^  db:/ { in_service = 1; next }
  /^  [A-Za-z0-9_-]+:/ { in_service = 0 }
  in_service && /^    ports:/ { found = 1 }
  END { exit found ? 0 : 1 }
' "$DEPLOY_DIR/docker-compose.yml"; then
  die "database must not publish a host port"
fi

if awk '
  /^  friend-gateway:/ { in_service = 1; next }
  /^  [A-Za-z0-9_-]+:/ { in_service = 0 }
  in_service && /^    ports:/ { found = 1 }
  END { exit found ? 0 : 1 }
' "$DEPLOY_DIR/docker-compose.yml"; then
  die "friend-gateway must not publish a host port"
fi

printf 'compose and route validation: ok (public edge is path allowlisted)\n'
