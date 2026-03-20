docker stop `docker ps | grep sensoriot_server | sed 's/ /\n/g'| tail -1`
./build_docker
./run_docker.sh  `tr -dc A-Za-z0-9 </dev/urandom | head -c 13; echo` > /dev/null  & 
