#!/bin/bash
# Installs a monthly cron job to archive and prune sensor data older than 6 months.
# Run once: ./install_archive_cron.sh
# To remove the job later: crontab -e  and delete the archivedb line.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/archivedb.log"
CRON_CMD="0 2 1 * * cd $SCRIPT_DIR && pipenv run python3 archivedb.py -d PROD -m 6 --remove >> $LOG_FILE 2>&1"

# Add the line only if it isn't already present
if crontab -l 2>/dev/null | grep -q 'archivedb.py'; then
    echo "Cron job already installed. Current crontab:"
    crontab -l | grep 'archivedb.py'
else
    (crontab -l 2>/dev/null; echo "$CRON_CMD") | crontab -
    echo "Cron job installed:"
    echo "  $CRON_CMD"
fi
