#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

[ "$#" -eq 1 ] || die "usage: RESTORE_CONFIRM=YES restore.sh /var/backups/friend-v1a/friend-v1a-<timestamp>.tar.gz"
[ "${RESTORE_CONFIRM:-}" = "YES" ] || die "restore is destructive; set RESTORE_CONFIRM=YES explicitly"
load_required_env
require_command docker
require_command tar
require_command mktemp
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"

archive="$1"
case "$archive" in
  "$BACKUP_DIR"/*.tar.gz) ;;
  *) die "restore archive must be a .tar.gz inside BACKUP_DIR" ;;
esac
[ -f "$archive" ] || die "restore archive does not exist"
[ ! -L "$archive" ] || die "restore archive must not be a symlink"

entries="$(tar -tzf "$archive")" || die "cannot inspect restore archive"
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  case "$entry" in
    db.sql|Caddyfile|docker-compose.yml|manifest.txt) ;;
    *) die "archive contains an unexpected or unsafe entry: $entry" ;;
  esac
done <<< "$entries"

for expected in db.sql Caddyfile docker-compose.yml manifest.txt; do
  count="$(printf '%s\n' "$entries" | awk -v wanted="$expected" '$0 == wanted { count += 1 } END { print count + 0 }')"
  [ "$count" = 1 ] || die "archive must contain exactly one $expected"
done

work_dir="$(mktemp -d "$BACKUP_DIR/.friend-restore.XXXXXX")"
cleanup() { if [ -d "${work_dir:-}" ]; then rm -rf -- "$work_dir"; fi; }
trap cleanup EXIT
tar -xzf "$archive" -C "$work_dir" || die "archive extraction failed"
[ -f "$work_dir/db.sql" ] || die "database dump missing after extraction"
[ ! -L "$work_dir/db.sql" ] || die "database dump must not be a symlink"
grep -Fq 'schema_version=friend-v1a-backup-1' "$work_dir/manifest.txt" || die "unsupported backup schema"

running_services="$(compose ps --services --status running)" || die "unable to inspect compose services"
printf '%s\n' "$running_services" | grep -Fxq db || die "db service is not running"

# Caddy/Compose files are extracted for human review only; this script never overwrites the deployed config.
compose exec -T -e "MYSQL_PWD=$MYSQL_ROOT_PASSWORD" db \
  mysql --protocol=socket --user=root --database="$MYSQL_DATABASE" --binary-mode < "$work_dir/db.sql" \
  || die "database restore failed"
printf 'database restore completed: %s\n' "$archive"
