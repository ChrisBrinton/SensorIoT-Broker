#!/bin/bash

mosquitto  -v -c /mosquitto.conf -d 

#python3 DataBroker.py --dbconn host.docker.internal:27017 --host 127.0.0.1 --port 1883 &
python3 DataBroker.py --dbconn 127.0.0.1:27017 --host localhost  --port 1883 --log true > /var/log/broker.log  &

# Wait for any process to exit
wait -n

# Exit with status of process that exited first
exit $?
