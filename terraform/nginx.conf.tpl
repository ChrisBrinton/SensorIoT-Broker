# Nginx reverse proxy template for SensorIoT Broker
# After provisioning, copy to /etc/nginx/sites-available/sensoriot
# and run: certbot --nginx -d ${domain_name}

server {
    listen 80;
    server_name ${domain_name};

    # REST API
    location / {
        proxy_pass http://127.0.0.1:5050;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}

# MQTT is handled directly on port 1883 (not proxied through nginx)
