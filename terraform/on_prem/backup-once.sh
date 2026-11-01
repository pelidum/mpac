#!/bin/bash
set -euo pipefail
umask 077
stamp=$(date -u +%Y%m%d_%H%M%S)
pending="/backups/mpac_${stamp}.dump.partial"
trap 'rm -f "$pending"' EXIT
pg_dump -Fc -f "$pending"
# Retain immutable blobs referenced by older retained database dumps, too.
/tools/rclone copy :s3:blobs/blobs /backups/blobs
mv "$pending" "/backups/mpac_${stamp}.dump"
find /backups -maxdepth 1 -name '*.dump' -mtime +"$RETENTION_DAYS" -delete
