output "server_public_ip" {
  description = "Static public IP address of the server"
  value       = ionoscloud_ipblock.main.ips[0]
}

output "ssh_connection" {
  description = "SSH command to connect to the server"
  value       = "ssh root@${ionoscloud_ipblock.main.ips[0]}"
}

output "datacenter_id" {
  description = "ID of the created Virtual Data Center"
  value       = ionoscloud_datacenter.main.id
}
