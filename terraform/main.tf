terraform {
  required_providers {
    ionoscloud = {
      source  = "ionos-cloud/ionoscloud"
      version = "~> 6.4"
    }
  }
}

provider "ionoscloud" {
  token = var.ionos_token
}

# ---------------------------------------------------------------------------
# Virtual Data Center
# ---------------------------------------------------------------------------

resource "ionoscloud_datacenter" "main" {
  name        = "sensoriot-datacenter"
  location    = "us/ewr"
  description = "SensorIoT production datacenter"
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

resource "ionoscloud_lan" "public" {
  datacenter_id = ionoscloud_datacenter.main.id
  public        = true
  name          = "public-lan"
}

resource "ionoscloud_ipblock" "main" {
  location = "us/ewr"
  size     = 1
  name     = "sensoriot-ipblock"
}

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

resource "ionoscloud_server" "app" {
  name          = "sensoriot-app"
  datacenter_id = ionoscloud_datacenter.main.id
  cores         = 2
  ram           = 4096
  image_name    = "ubuntu:22.04"
  type          = "VCPU"
  ssh_keys      = [var.ssh_public_key]

  user_data = base64encode(templatefile("${path.module}/cloud-init.yaml.tpl", {
    repo_url                  = var.repo_url
    google_web_client_id      = var.google_web_client_id
    google_home_client_id     = var.google_home_client_id
    google_home_client_secret = var.google_home_client_secret
  }))

  volume {
    name      = "system"
    size      = 50
    disk_type = "SSD Standard"
    bus       = "VIRTIO"
  }

  nic {
    lan             = ionoscloud_lan.public.id
    name            = "public-nic"
    dhcp            = true
    firewall_active = true
    ips             = [ionoscloud_ipblock.main.ips[0]]

    firewall {
      protocol         = "TCP"
      name             = "allow-ssh"
      port_range_start = 22
      port_range_end   = 22
      type             = "INGRESS"
    }

    firewall {
      protocol         = "TCP"
      name             = "allow-http"
      port_range_start = 80
      port_range_end   = 80
      type             = "INGRESS"
    }

    firewall {
      protocol         = "TCP"
      name             = "allow-https"
      port_range_start = 443
      port_range_end   = 443
      type             = "INGRESS"
    }

    firewall {
      protocol         = "TCP"
      name             = "allow-mqtt"
      port_range_start = 1883
      port_range_end   = 1883
      type             = "INGRESS"
    }
  }
}
