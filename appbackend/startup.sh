#!/bin/bash
#nginx -c /etc/nginx/nginx.conf &
nginx &

# Regression forecast recorder — stores predictions every 60 minutes for
# comparison with actuals and future model fine-tuning
python3 -u ForecastRecorder.py \
    --dbconn ${MONGODB_HOST:-127.0.0.1}:27017 \
    --interval 60 \
    > forecast_recorder.log 2>&1 &

gunicorn --timeout 120 --access-logfile - --log-file gunicorn.log -w 4 -b 0.0.0.0:5050 server:app

# Wait for any process to exit
wait -n

# Exit with status of process that exited first
exit $?

