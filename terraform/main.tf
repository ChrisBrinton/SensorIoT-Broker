terraform {
  required_version = ">= 1.5"

  required_providers {
    ionoscloud = {
      source  = "ionos-cloud/ionoscloud"
      version = ">= 6.4"
    }
  }
}

provider "ionoscloud" {
  token = var.ionos_token
}

# ---------------------------------------------------------------------------
# Datacenter
# ---------------------------------------------------------------------------
resource "ionoscloud_datacenter" "main" {
  name        = var.datacenter_name
  location    = var.datacenter_location
  description = "SensorIoT backend infrastructure"
}

# ---------------------------------------------------------------------------
# Public LAN
# ---------------------------------------------------------------------------
resource "ionoscloud_lan" "public" {
  datacenter_id = ionoscloud_datacenter.main.id
  name          = "sensoriot-public"
  public        = true
}

# ---------------------------------------------------------------------------
# Static IP
# ---------------------------------------------------------------------------
resource "ionoscloud_ipblock" "server_ip" {
  location = var.datacenter_location
  size     = 1
  name     = "sensoriot-ip"
}

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
resource "ionoscloud_server" "broker" {
  name          = var.server_name
  datacenter_id = ionoscloud_datacenter.main.id
  cores         = var.server_cores
  ram           = var.server_ram_mb
  type          = "ENTERPRISE"

  image_name    = var.image_name
  ssh_keys      = [var.ssh_public_key]

  volume {
    name      = "boot"
    size      = var.volume_size_gb
    disk_type = var.volume_disk_type
    bus       = "VIRTIO"
  }

  nic {
    lan             = ionoscloud_lan.public.id
    name            = "public"
    dhcp            = false
    ips             = [ionoscloud_ipblock.server_ip.ips[0]]
    firewall_active = true

    firewall {
      protocol         = "TCP"
      name             = "SSH"
      port_range_start = 22
      port_range_end   = 22
      source_ip        = ""
    }

    firewall {
      protocol         = "TCP"
      name             = "HTTPS"
      port_range_start = 443
      port_range_end   = 443
    }

    firewall {
      protocol         = "TCP"
      name             = "HTTP"
      port_range_start = 80
      port_range_end   = 80
    }

    firewall {
      protocol         = "TCP"
      name             = "MQTT"
      port_range_start = 1883
      port_range_end   = 1883
    }
  }
}

# ---------------------------------------------------------------------------
# Cloud-init provisioning (runs on first boot)
# ---------------------------------------------------------------------------
resource "null_resource" "provision" {
  depends_on = [ionoscloud_server.broker]

  connection {
    type        = "ssh"
    host        = ionoscloud_ipblock.server_ip.ips[0]
    user        = "root"
    private_key = file("~/.ssh/id_rsa")
  }

  # Copy the project to the server
  provisioner "file" {
    source      = "${path.module}/.."
    destination = "/opt/sensoriot-broker"
  }

  # Install Docker and start services
  provisioner "remote-exec" {
    inline = [
      "set -e",

      "# Install Docker",
      "apt-get update -qq",
      "apt-get install -y ca-certificates curl gnupg",
      "install -m 0755 -d /etc/apt/keyrings",
      "curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg",
      "chmod a+r /etc/apt/keyrings/docker.gpg",
      "echo \"deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable\" | tee /etc/apt/sources.list.d/docker.list > /dev/null",
      "apt-get update -qq",
      "apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin",

      "# Install nginx for reverse proxy / TLS termination",
      "apt-get install -y nginx certbot python3-certbot-nginx",

      "# Start the stack",
      "cd /opt/sensoriot-broker/docker",
      "docker compose up -d --build",

      "# Enable Docker to start on boot",
      "systemctl enable docker",

      "echo 'Provisioning complete.'",
    ]
  }
}
