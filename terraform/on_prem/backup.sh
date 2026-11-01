#!/bin/bash
set -uo pipefail
umask 077
chmod 700 /backups
while true; do
  # Keep publication and deletion paused for a consistent database/blob backup.
  if psql -X -v ON_ERROR_STOP=1 <<'SQL'
SELECT pg_advisory_lock(674288901);
\! /bin/bash /etc/mpac/backup-once.sh
\if :SHELL_ERROR
SELECT 1/0;
\endif
SELECT pg_advisory_unlock(674288901);
SQL
  then
    echo "Backup completed"
  else
    echo "Backup failed; retrying next interval" >&2
  fi
  sleep "$BACKUP_INTERVAL"
done
