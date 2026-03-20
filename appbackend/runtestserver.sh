#!/bin/sh
nohup gunicorn --reload --timeout 120 --access-logfile - --log-file gunicorn.log -w 1 -b 0.0.0.0:5051 server:app &
