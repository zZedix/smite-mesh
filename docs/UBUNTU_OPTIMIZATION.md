# Ubuntu Network Optimization for Stable Tunnels

## System-Level Optimizations

### 1. Increase Connection Limits

```bash
# Edit sysctl.conf
sudo nano /etc/sysctl.conf

# Add these lines:
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 5000
net.ipv4.tcp_max_syn_backlog = 8192
net.ipv4.ip_local_port_range = 10000 65535
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 30

# Apply changes
sudo sysctl -p
```

### 2. TCP Keep-Alive Settings

```bash
# Add to /etc/sysctl.conf:
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 60
net.ipv4.tcp_keepalive_probes = 3
net.ipv4.tcp_slow_start_after_idle = 0

sudo sysctl -p
```

### 3. Increase File Descriptor Limits

```bash
# Edit limits.conf
sudo nano /etc/security/limits.conf

# Add these lines:
* soft nofile 65535
* hard nofile 65535
root soft nofile 65535
root hard nofile 65535

# Apply for current session
ulimit -n 65535
```

### 4. Disable Swap (if you have enough RAM)

```bash
# Temporarily disable
sudo swapoff -a

# Permanently disable
sudo sed -i '/ swap / s/^\(.*\)$/#\1/g' /etc/fstab
```

### 5. Network Buffer Sizes

```bash
# Add to /etc/sysctl.conf:
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.udp_mem = 3145728 4194304 16777216

sudo sysctl -p
```

### 6. Disable IPv6 (if not needed)

```bash
# Edit /etc/sysctl.conf:
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1

sudo sysctl -p
```

### 7. Disable TCP Timestamps (can help with NAT)

```bash
# Add to /etc/sysctl.conf:
net.ipv4.tcp_timestamps = 0

sudo sysctl -p
```

### 8. Increase Connection Tracking

```bash
# If using iptables/nftables
sudo modprobe nf_conntrack
echo 'net.netfilter.nf_conntrack_max = 1000000' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### 9. Optimize for High-Latency Networks

```bash
# Add to /etc/sysctl.conf:
net.ipv4.tcp_congestion_control = bbr
net.core.default_qdisc = fq

# Load BBR module
sudo modprobe tcp_bbr
echo 'tcp_bbr' | sudo tee -a /etc/modules-load.d/modules.conf

sudo sysctl -p
```

## Docker-Specific Optimizations

### 1. Increase Docker Network Subnet

Edit `/etc/docker/daemon.json`:
```json
{
  "default-address-pools": [
    {
      "base": "172.17.0.0/16",
      "size": 24
    }
  ],
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
```

Restart Docker:
```bash
sudo systemctl restart docker
```

## Monitoring

### Check Current Settings
```bash
# Check connection limits
ss -s

# Check file descriptors
ulimit -n

# Check network stats
cat /proc/sys/net/core/somaxconn
cat /proc/sys/net/ipv4/tcp_max_syn_backlog
```

### Monitor Connections
```bash
# Watch connections
watch -n 1 'ss -s'

# Check ESTABLISHED connections
ss -tun | grep ESTAB | wc -l
```

## After Applying Changes

1. Reboot the server to ensure all changes take effect
2. Monitor system logs: `journalctl -f`
3. Monitor tunnel stability and connection counts

