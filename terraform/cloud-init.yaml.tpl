#cloud-config

# Note: ${...} below are Terraform templatefile variables.
# $${ and $$ are literal $ characters passed through to the shell.

packages:
  - git
  - ca-certificates
  - curl
  - gnupg

write_files:
  # App .env — written to /tmp first, moved after git clone in runcmd
  - path: /tmp/app.env
    permissions: "0600"
    content: |
      GOOGLE_WEB_CLIENT_ID=${google_web_client_id}
      GOOGLE_HOME_CLIENT_ID=${google_home_client_id}
      GOOGLE_HOME_CLIENT_SECRET=${google_home_client_secret}

  # Systemd unit so Docker Compose restarts on reboot
  - path: /etc/systemd/system/sensoriot.service
    content: |
      [Unit]
      Description=SensorIoT Docker Compose Services
      After=docker.service network-online.target
      Requires=docker.service

      [Service]
      Type=simple
      WorkingDirectory=/opt/sensoriot
      ExecStart=/usr/bin/docker compose up
      ExecStop=/usr/bin/docker compose down
      Restart=on-failure
      RestartSec=10

      [Install]
      WantedBy=multi-user.target

runcmd:
  # Install Docker CE via convenience script
  - curl -fsSL https://get.docker.com | sh

  # Clone the application repository
  - git clone ${repo_url} /opt/sensoriot

  # Place .env file (written by write_files above)
  - mv /tmp/app.env /opt/sensoriot/SensorIoT-REST_server/.env

  # Build images and start all services
  - cd /opt/sensoriot && docker compose up -d --build

  # Enable systemd unit for auto-start on reboot
  - systemctl enable sensoriot
