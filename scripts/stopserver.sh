#!/bin/bash

kill -9 `ps aux|grep gunicorn|grep server:app|awk '{ print $2 }'` 
gzip -f nohup.out.old
gzip -f gunicorn.log.old
mv nohup.out nohup.out.old
mv gunicorn.log gunicorn.log.old

