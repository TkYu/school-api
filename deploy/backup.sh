#!/usr/bin/env bash
# Backup on the host (cron, e.g. nightly): a consistent database dump (pg_dump, never a copy of the database files while
# it runs), plus the original files, the secrets, .env and the compose files. Models are left out (downloaded again). Keeps 14 of each.
set -euo pipefail
cd "$(dirname "$0")/.."
STAMP=$(date +%Y%m%d-%H%M%S)
docker compose exec -T db pg_dump -U school -Fc school > "backups/db-$STAMP.dump"
# the original files through the API container, so the same works with named volumes (compose.yml) and with a folder
# on the host (a compose.local.yml); then the host-only settings
docker compose exec -T api tar -C /data -czf - files > "backups/files-$STAMP.tar.gz"
tar -czf "backups/config-$STAMP.tar.gz" secrets .env $(ls compose*.yml)
chmod 600 "backups/db-$STAMP.dump" "backups/files-$STAMP.tar.gz" "backups/config-$STAMP.tar.gz"
ls -1t backups/db-*.dump | tail -n +15 | xargs -r rm -f
ls -1t backups/files-*.tar.gz | tail -n +15 | xargs -r rm -f
ls -1t backups/config-*.tar.gz | tail -n +15 | xargs -r rm -f
echo "backups/db-$STAMP.dump backups/files-$STAMP.tar.gz backups/config-$STAMP.tar.gz"
# restore: docker compose exec -T db pg_restore -U school -d school --clean < backups/db-<stamp>.dump
