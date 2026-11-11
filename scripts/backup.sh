#!/usr/bin/env bash
# Daily backup: database, attachments, deletion log and deploy/.env.prod.
#
#   scripts/backup.sh            # run from the repository root on the server
#
# Schedule it once a day, e.g. in root's crontab:
#   15 3 * * * cd /srv/gatherloom && scripts/backup.sh >> /var/log/gatherloom-backup.log 2>&1
#
# Settings come from deploy/.env.prod: BACKUP_DIR (default
# /var/backups/gatherloom), BACKUP_KEEP_DAYS (default 14) and optionally
# BACKUP_REMOTE, an rsync destination that receives the same directory.
# Backups contain model keys and the database password: keep them private.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
env_file="$root/deploy/.env.prod"
setting() { grep -E "^$1=" "$env_file" | tail -n1 | cut -d= -f2- || true; }
backup_dir="${BACKUP_DIR:-$(setting BACKUP_DIR)}"
backup_dir="${backup_dir:-/var/backups/gatherloom}"
keep_days="${BACKUP_KEEP_DAYS:-$(setting BACKUP_KEEP_DAYS)}"
keep_days="${keep_days:-14}"
remote="${BACKUP_REMOTE:-$(setting BACKUP_REMOTE)}"
compose() { docker compose -f "$root/deploy/compose.prod.yaml" --env-file "$env_file" "$@"; }

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$backup_dir/$stamp"
partial="$target.partial"
umask 077
mkdir -p "$partial"

echo "[$stamp] backing up to $target"
compose exec -T postgres pg_dump -U gatherloom -d gatherloom --format=custom >"$partial/database.dump"
compose exec -T api tar -C /data -cf - storage >"$partial/storage.tar"
compose exec -T api sh -c 'cat /data/state/deletions.jsonl 2>/dev/null || true' >"$partial/deletions.jsonl"
cp "$env_file" "$partial/env.prod"
(cd "$partial" && sha256sum database.dump storage.tar deletions.jsonl env.prod >SHA256SUMS)
mv "$partial" "$target"

find "$backup_dir" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime +"$keep_days" -exec rm -rf {} +
if [ -n "$remote" ]; then
  rsync -a --delete "$backup_dir/" "$remote/"
fi
echo "[$stamp] done: $(du -sh "$target" | cut -f1)"
