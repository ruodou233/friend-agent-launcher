#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

load_required_env
require_command docker
require_command tar
require_command mktemp
docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"

if [ -e "$BACKUP_DIR" ] && [ -L "$BACKUP_DIR" ]; then
  die "BACKUP_DIR must not be a symlink"
fi
mkdir -p "$BACKUP_DIR"
[ -d "$BACKUP_DIR" ] || die "BACKUP_DIR is not a directory"

running_services="$(compose ps --services --status running)" || die "unable to inspect compose services"
printf '%s\n' "$running_services" | grep -Fxq db || die "db service is not running"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
work_dir="$(mktemp -d "$BACKUP_DIR/.friend-backup.XXXXXX")"
archive_tmp="$BACKUP_DIR/.friend-v1a-$timestamp.tar.gz.tmp"
archive="$BACKUP_DIR/friend-v1a-$timestamp.tar.gz"
[ ! -e "$archive_tmp" ] || die "backup temp path already exists"
[ ! -e "$archive" ] || die "backup archive already exists"

cleanup() {
  if [ -d "${work_dir:-}" ]; then rm -rf -- "$work_dir"; fi
  if [ -e "${archive_tmp:-}" ]; then rm -f -- "$archive_tmp"; fi
}
trap cleanup EXIT

compose exec -T -e "MYSQL_PWD=$MYSQL_ROOT_PASSWORD" db \
  mysqldump --single-transaction --routines --events --triggers "$MYSQL_DATABASE" > "$work_dir/db.sql" \
  || die "database dump failed"

cp "$DEPLOY_DIR/Caddyfile" "$work_dir/Caddyfile"
cp "$DEPLOY_DIR/docker-compose.yml" "$work_dir/docker-compose.yml"
{
  printf 'schema_version=friend-v1a-backup-1\n'
  printf 'restore_scope=database-only;config-files-for-review\n'
  printf 'created_at=%s\n' "$timestamp"
} > "$work_dir/manifest.txt"

tar -czf "$archive_tmp" -C "$work_dir" db.sql Caddyfile docker-compose.yml manifest.txt \
  || die "backup archive creation failed"
chmod 600 "$archive_tmp"
mv "$archive_tmp" "$archive"
printf 'backup created: %s\n' "$archive"
