docker run --network host --name $1 -p 80:80 -p 443:443 \
  -v sensoriot_models:/models \
  sensoriot_server
