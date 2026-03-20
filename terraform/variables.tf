variable "ionos_token" {
  description = "IONOS Cloud API token (set via IONOS_TOKEN env var or terraform.tfvars)"
  type        = string
  sensitive   = true
}

variable "datacenter_name" {
  description = "Name for the IONOS datacenter"
  type        = string
  default     = "sensoriot-dc"
}

variable "datacenter_location" {
  description = "IONOS datacenter location (us/las, us/ewr, de/fra, de/txl, gb/lhr, es/vit, fr/par)"
  type        = string
  default     = "us/las"
}

variable "server_name" {
  description = "Name for the VPS instance"
  type        = string
  default     = "sensoriot-broker"
}

variable "server_cores" {
  description = "Number of CPU cores"
  type        = number
  default     = 2
}

variable "server_ram_mb" {
  description = "RAM in MB (must be multiple of 256)"
  type        = number
  default     = 2048
}

variable "volume_size_gb" {
  description = "Boot volume size in GB"
  type        = number
  default     = 20
}

variable "volume_disk_type" {
  description = "Disk type: HDD, SSD Standard, or SSD Premium"
  type        = string
  default     = "SSD Standard"
}

variable "ssh_public_key" {
  description = "SSH public key for server access"
  type        = string
}

variable "image_name" {
  description = "OS image name (regex matched)"
  type        = string
  default     = "ubuntu-22.04"
}

variable "domain_name" {
  description = "Domain name for the server (used in nginx config)"
  type        = string
  default     = ""
}
