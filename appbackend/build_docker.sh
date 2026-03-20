set BUILDKIT_PROGRESS=plain
# Build context is the repo root (../) so anomalydetection/ is accessible to the Dockerfile
docker build --platform linux/amd64/v3  -f SensorIoT-REST_server/Dockerfile -t sensoriot_server SensorIoT-REST_server  2>&1
