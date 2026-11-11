#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh, then replay deletions before
# opening the service again (product plan §7.4: a backup is not deletion).
#
#   scripts/restore.sh /var/backups/gatherloom/20261111T031500Z --yes
#
# Replaces the current database and attachments. The deletion log on the
# server is kept and merged with the backup's copy, so entries deleted after
# the backup was taken are deleted again.
set -euo pipefail

source_dir="${1:?usage: scripts/restore.sh <backup directory> --yes}"
[ "${2:-}" = "--yes" ] || { echo "This replaces the database and attachments. Add --yes to continue." >&2; exit 2; }
root="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$root/deploy/.env.prod"
compose() { docker compose -f "$root/deploy/compose.prod.yaml" --env-file "$env_file" "$@"; }
oneoff() { compose run --rm --no-deps -T "$@"; }

(cd "$source_dir" && sha256sum --check --quiet SHA256SUMS)
echo "Stopping the service"
compose stop caddy api worker scheduler
compose up -d --wait postgres

echo "Restoring the database"
compose exec -T postgres dropdb -U gatherloom --if-exists --force gatherloom
compose exec -T postgres createdb -U gatherloom gatherloom
compose exec -T postgres pg_restore -U gatherloom -d gatherloom --no-owner <"$source_dir/database.dump"

echo "Restoring attachments and merging the deletion log"
oneoff --entrypoint sh api -c 'rm -rf /data/storage/* && tar -C /data -xf -' <"$source_dir/storage.tar"
oneoff --entrypoint sh api -c \
  'cat /data/state/deletions.jsonl 2>/dev/null; cat' <"$source_dir/deletions.jsonl" \
  | sort -u >"$source_dir/deletions.merged.jsonl"
oneoff --entrypoint sh api -c 'cat >/data/state/deletions.jsonl' <"$source_dir/deletions.merged.jsonl"
rm -f "$source_dir/deletions.merged.jsonl"

echo "Migrating and replaying deletions"
oneoff api alembic upgrade head
oneoff api python -m app.admin replay-deletions

echo "Starting the service"
compose up -d --wait
echo "Restore complete."
