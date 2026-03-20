output "server_public_ip" {
  description = "Public IP address of the SensorIoT Broker server"
  value       = ionoscloud_ipblock.server_ip.ips[0]
}

output "datacenter_id" {
  description = "IONOS datacenter ID"
  value       = ionoscloud_datacenter.main.id
}

output "server_id" {
  description = "IONOS server ID"
  value       = ionoscloud_server.broker.id
}

output "ssh_command" {
  description = "SSH command to connect to the server"
  value       = "ssh root@${ionoscloud_ipblock.server_ip.ips[0]}"
}

output "api_url" {
  description = "REST API URL (direct, before nginx)"
  value       = "http://${ionoscloud_ipblock.server_ip.ips[0]}:5050"
}

output "mqtt_url" {
  description = "MQTT broker URL"
  value       = "mqtt://${ionoscloud_ipblock.server_ip.ips[0]}:1883"
}
