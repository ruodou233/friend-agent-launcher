#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

[ "$#" -eq 1 ] || die "usage: healthcheck.sh https://approved-host"
load_required_env
require_command curl
require_command python3
require_command docker
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"

base_url="${1%/}"
[ "$base_url" = "https://$PUBLIC_HOST" ] || die "health URL must exactly match https://PUBLIC_HOST"

running_services="$(compose ps --services --status running)" || die "unable to inspect compose services"
for service in caddy friend-gateway new-api db; do
  printf '%s\n' "$running_services" | grep -Fxq "$service" || die "service is not running: $service"
done

response_file="$(mktemp "${TMPDIR:-/tmp}/friend-v1a-health.XXXXXX")"
cleanup() { rm -f -- "$response_file"; }
trap cleanup EXIT

request_id="healthcheck-$(date -u +%s)"
status="$(curl --proto '=https' --tlsv1.2 --silent --show-error --max-time 15 \
  -H "X-Request-Id: $request_id" \
  --output "$response_file" --write-out '%{http_code}' \
  "$base_url/v1/friend/preflight?client_version=healthcheck&product=claude&protocol=anthropic-messages")" || die "HTTPS preflight request failed"
[ "$status" = "200" ] || die "preflight returned HTTP $status"

python3 - "$response_file" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    body = json.load(handle)
assert body.get("available") is True, "preflight is not available"
assert body.get("product") == "claude", "unexpected product"
assert body.get("protocol") == "anthropic-messages", "unexpected protocol"
assert isinstance(body.get("request_id"), str) and body["request_id"], "missing request_id"
PY

for blocked_path in /internal/keys /internal/manual-recharges/example /healthz /v1/models /v1/responses; do
  blocked_status="$(curl --proto '=https' --tlsv1.2 --silent --show-error --max-time 10 \
    -o /dev/null -w '%{http_code}' -H "X-Request-Id: $request_id" \
    "$base_url$blocked_path")" || die "route boundary probe failed: $blocked_path"
  case "$blocked_status" in
    2??) die "forbidden path is publicly reachable: $blocked_path ($blocked_status)" ;;
  esac
done

printf 'healthcheck: ok (preflight reachable; management and non-V1 paths not 2xx)\n'
