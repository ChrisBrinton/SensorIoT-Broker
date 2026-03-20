#!/bin/bash
#nginx -c /etc/nginx/nginx.conf &
nginx &

gunicorn --timeout 120 --access-logfile - --log-file gunicorn.log -w 4 -b 0.0.0.0:5050 server:app

# Wait for any process to exit
wait -n

# Exit with status of process that exited first
exit $?

