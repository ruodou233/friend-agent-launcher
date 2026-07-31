#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ENV_FILE:-$DEPLOY_DIR/.env}"

die() {
  printf 'new-api-deployment: %s\n' "$*" >&2
  exit 1
}

validate_env_file() {
  [ -f "$ENV_FILE" ] || die "missing ENV_FILE: $ENV_FILE"
  [ ! -L "$ENV_FILE" ] || die "ENV_FILE must not be a symlink"

  awk -F= '
    /^[[:space:]]*$/ || /^[[:space:]]*#/ { next }
    {
      if ($0 !~ /^[A-Za-z_][A-Za-z0-9_]*=[^[:space:]#]+$/) {
        printf "invalid .env line %d\n", NR > "/dev/stderr"
        bad = 1
        next
      }
      if (seen[$1]++) {
        printf "duplicate .env key %s\n", $1 > "/dev/stderr"
        bad = 1
      }
      if ($0 ~ /\$\(|`|;|&&|\|\||[<>]/) {
        printf "shell syntax is not allowed in .env key %s\n", $1 > "/dev/stderr"
        bad = 1
      }
    }
    END { exit bad ? 1 : 0 }
  ' "$ENV_FILE" || die "unsafe .env file"
}

env_value() {
  local key="$1"
  awk -F= -v wanted="$key" '
    $1 == wanted { print substr($0, length(wanted) + 2); found = 1; exit }
    END { if (!found) exit 1 }
  ' "$ENV_FILE"
}

required_env() {
  local key="$1"
  local value
  value="$(env_value "$key")" || die "missing .env key: $key"
  [ -n "$value" ] || die ".env key is empty: $key"
  printf '%s' "$value"
}

reject_placeholder() {
  local key="$1"
  local value="$2"
  case "$value" in
    *replace-with-*|*REPLACE_WITH_*|*placeholder*|*PLACEHOLDER*|*.invalid)
      die "$key still contains a template placeholder"
      ;;
  esac
}

load_required_env() {
  validate_env_file
  export COMPOSE_PROJECT_NAME="$(required_env COMPOSE_PROJECT_NAME)"
  export PUBLIC_HOST="$(required_env PUBLIC_HOST)"
  export NEW_API_IMAGE="$(required_env NEW_API_IMAGE)"
  export NEW_API_PORT="$(required_env NEW_API_PORT)"
  export NEW_API_UPSTREAM_PROVIDER="$(required_env NEW_API_UPSTREAM_PROVIDER)"
  export MYSQL_DATABASE="$(required_env MYSQL_DATABASE)"
  export MYSQL_USER="$(required_env MYSQL_USER)"
  export MYSQL_PASSWORD="$(required_env MYSQL_PASSWORD)"
  export MYSQL_ROOT_PASSWORD="$(required_env MYSQL_ROOT_PASSWORD)"
  export NEW_API_SESSION_SECRET="$(required_env NEW_API_SESSION_SECRET)"
  export BACKUP_DIR="$(required_env BACKUP_DIR)"

  reject_placeholder PUBLIC_HOST "$PUBLIC_HOST"
  reject_placeholder NEW_API_IMAGE "$NEW_API_IMAGE"
  reject_placeholder MYSQL_PASSWORD "$MYSQL_PASSWORD"
  reject_placeholder MYSQL_ROOT_PASSWORD "$MYSQL_ROOT_PASSWORD"
  reject_placeholder NEW_API_SESSION_SECRET "$NEW_API_SESSION_SECRET"
  [ "$NEW_API_UPSTREAM_PROVIDER" = "LowcostAI" ] || die "V1A only permits the LowcostAI placeholder"
  [[ "$PUBLIC_HOST" =~ ^[A-Za-z0-9.-]+$ ]] || die "PUBLIC_HOST must be a hostname without scheme or path"
  [[ "$NEW_API_PORT" =~ ^[0-9]+$ ]] || die "NEW_API_PORT must be numeric"
  [ "$NEW_API_PORT" -ge 1 ] && [ "$NEW_API_PORT" -le 65535 ] || die "NEW_API_PORT is out of range"
  [[ "$BACKUP_DIR" != *..* ]] || die "BACKUP_DIR must not contain .."
  [[ "$BACKUP_DIR" != *[[:space:]]* ]] || die "BACKUP_DIR must not contain whitespace"
  case "$BACKUP_DIR" in
    /var/backups/friend-v1a|/var/backups/friend-v1a/*) ;;
    *) die "BACKUP_DIR must stay under /var/backups/friend-v1a" ;;
  esac
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

compose() {
  docker compose --env-file "$ENV_FILE" --project-directory "$DEPLOY_DIR" -f "$DEPLOY_DIR/docker-compose.yml" "$@"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  die "lib.sh is an internal library; invoke a named check/backup/restore script"
fi
