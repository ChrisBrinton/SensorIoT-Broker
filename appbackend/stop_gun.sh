#!/bin/bash

kill -9 `ps aux|grep gunicorn|grep server:app|awk '{ print $2 }'` 

