#!/bin/bash

mosquitto  -v -c /mosquitto.conf -d

# NOAA forecast publisher — runs every 60 minutes
python3 -u NOAAPublisher.py \
    --dbconn ${MONGODB_HOST:-127.0.0.1}:27017 \
    --interval 60 \
    --firebase-key /firebase_service_account.json \
    > noaa.log 2>&1 &

# Alert rule publisher — polls every 1 minute
python3 -u AlertPublisher.py \
    --dbconn ${MONGODB_HOST:-127.0.0.1}:27017 \
    --interval 1 \
    --firebase-key /firebase_service_account.json \
    > alert.log 2>&1 &

#python3 DataBroker.py --dbconn host.docker.internal:27017 --host 127.0.0.1 --port 1883 &
python3 DataBroker.py --dbconn ${MONGODB_HOST:-127.0.0.1}:27017 --host localhost --port 1883 --log > broker.log 2>&1

# Wait for any process to exit
wait -n

# Exit with status of process that exited first
exit $?
