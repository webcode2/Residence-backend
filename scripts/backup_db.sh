#!/usr/bin/env bash
set -euo pipefail

# Automated PostgreSQL Backup Script for Residence Backend
# Can be run via cron (e.g. daily at 02:00 AM)

BACKUP_DIR="${BACKUP_DIR:-/var/backups/residence}"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
RETENTION_DAYS="${RETENTION_DAYS:-14}"

DB_HOST="${POSTGRES_SERVER:-localhost}"
DB_PORT="${POSTGRES_PORT:-5432}"
DB_NAME="${POSTGRES_DB:-residence_db}"
DB_USER="${POSTGRES_USER:-postgres}"

mkdir -p "${BACKUP_DIR}"

BACKUP_FILE="${BACKUP_DIR}/${DB_NAME}_backup_${TIMESTAMP}.sql.gz"

echo "[BACKUP] Starting database backup for '${DB_NAME}' at ${TIMESTAMP}..."

# Export password if set in environment
export PGPASSWORD="${POSTGRES_PASSWORD:-}"

pg_dump -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" -F c -b -v | gzip > "${BACKUP_FILE}"

echo "[BACKUP] Backup successfully created at: ${BACKUP_FILE}"
echo "[BACKUP] File size: $(du -sh "${BACKUP_FILE}" | cut -f1)"

# Prune old backups older than retention window
echo "[BACKUP] Pruning backups older than ${RETENTION_DAYS} days..."
find "${BACKUP_DIR}" -type f -name "${DB_NAME}_backup_*.sql.gz" -mtime +"${RETENTION_DAYS}" -delete

echo "[BACKUP] Maintenance finished."
