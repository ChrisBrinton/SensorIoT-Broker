variable "ionos_token" {
  description = "IONOS Cloud API token. Generate at: https://dcd.ionos.com/latest/#/profile/token"
  type        = string
  sensitive   = true
}

variable "ssh_public_key" {
  description = "SSH public key to install on the server for root access (e.g. contents of ~/.ssh/id_rsa.pub)"
  type        = string
}

variable "repo_url" {
  description = "Git repository URL to clone onto the server (e.g. https://github.com/youruser/sensoriot.git)"
  type        = string
}

variable "google_web_client_id" {
  description = "Google Web Client ID for Google Home OAuth (from Google Cloud Console)"
  type        = string
  sensitive   = true
}

variable "google_home_client_id" {
  description = "Google Home Client ID for Smart Home integration"
  type        = string
  sensitive   = true
}

variable "google_home_client_secret" {
  description = "Google Home Client Secret for Smart Home integration"
  type        = string
  sensitive   = true
}
