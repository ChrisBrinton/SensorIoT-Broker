#!/bin/sh
nohup pipenv run gunicorn --reload --timeout 120 --access-logfile - --log-file gunicorn.log -w 4 -b 0.0.0.0:5050 server:app &
