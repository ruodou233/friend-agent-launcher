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

if grep -Eq 'path /internal|path /v1/(models|responses)' "$DEPLOY_DIR/Caddyfile"; then
  die "Caddyfile contains a forbidden public route"
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

printf 'compose and route validation: ok (public edge is path allowlisted)\n'
