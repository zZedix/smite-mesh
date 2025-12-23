# WireGuard Mesh over Smite Backhaul - Architecture

## Overview

This document describes the architecture and implementation of the WireGuard Mesh feature, which creates site-to-site VPN connectivity between multiple Linux VPS nodes using Smite Backhaul as the transport layer.

## Architecture Layers

### 1. Control Plane (Smite Panel)
- **WireGuardMeshManager**: Generates WireGuard keypairs, creates mesh configurations
- **Mesh API**: REST endpoints for creating, applying, and managing meshes
- **Database**: Stores mesh configurations, node assignments, and topology

### 2. Transport Layer (Backhaul UDP)
- Each node runs a **Backhaul UDP server** on a unique port
- WireGuard uses these Backhaul server endpoints as its transport
- Backhaul handles NAT traversal and provides reliable UDP connectivity

### 3. Overlay Network (WireGuard)
- WireGuard runs **on top of** Backhaul (not as a separate tunnel type)
- Each node gets an IP in the overlay subnet (e.g., 10.250.0.1, 10.250.0.2)
- WireGuard encrypts and routes traffic between nodes

### 4. Routing Layer (Linux Kernel)
- IPv4 forwarding enabled on all nodes
- Routes added for remote LAN subnets via WireGuard interface
- Pure routing (no NAT) between LANs

## Data Flow

```
[LAN 192.168.10.0/24] 
    ↓
[Node IR - WireGuard Interface wg-xxx]
    ↓ (encrypted)
[Backhaul UDP Server :3080]
    ↓ (UDP transport)
[Backhaul UDP Client]
    ↓
[Node TR - WireGuard Interface wg-xxx]
    ↓
[LAN 192.168.20.0/24]
```

## Key Components

### Panel Side

#### `wireguard_mesh_manager.py`
- `generate_keypair()`: Creates WireGuard private/public keys
- `create_mesh_config()`: Generates mesh configuration for all nodes
- `generate_wireguard_config()`: Creates WireGuard config file content
- `get_peer_routes()`: Extracts LAN subnets for routing

#### `routers/mesh.py`
- `POST /api/mesh/create`: Create new mesh
- `POST /api/mesh/{mesh_id}/apply`: Apply mesh to all nodes
- `GET /api/mesh/{mesh_id}/status`: Get mesh status from all nodes
- `POST /api/mesh/{mesh_id}/rotate-keys`: Rotate WireGuard keys
- `GET /api/mesh`: List all meshes
- `DELETE /api/mesh/{mesh_id}`: Delete mesh

### Node Side

#### `wireguard_adapter.py`
- `apply()`: Applies WireGuard configuration, brings up interface
- `remove()`: Brings down interface and removes config
- `status()`: Queries WireGuard status (handshakes, peers)
- `_setup_routes()`: Adds routes for remote LAN subnets
- `_enable_ip_forwarding()`: Enables IPv4 forwarding

#### `routers/agent.py` (mesh endpoints)
- `POST /api/agent/mesh/apply`: Apply mesh configuration
- `POST /api/agent/mesh/remove`: Remove mesh
- `GET /api/agent/mesh/{mesh_id}/status`: Get mesh status

## Configuration Example

### Mesh Creation Request
```json
{
  "name": "Office Mesh",
  "node_ids": ["node-ir-id", "node-tr-id", "node-uae-id"],
  "lan_subnets": {
    "node-ir-id": "192.168.10.0/24",
    "node-tr-id": "192.168.20.0/24",
    "node-uae-id": "192.168.30.0/24"
  },
  "overlay_subnet": "10.250.0.0/24",
  "topology": "full-mesh",
  "mtu": 1280
}
```

### Generated WireGuard Config (Node IR)
```ini
[Interface]
PrivateKey = <private_key>
Address = 10.250.0.1/32
MTU = 1280

[Peer]
PublicKey = <node-tr-public-key>
AllowedIPs = 10.250.0.2/32, 192.168.20.0/24
Endpoint = <node-tr-ip>:3080
PersistentKeepalive = 25

[Peer]
PublicKey = <node-uae-public-key>
AllowedIPs = 10.250.0.3/32, 192.168.30.0/24
Endpoint = <node-uae-ip>:3080
PersistentKeepalive = 25
```

## Topology Support

### Full-Mesh
- Every node connects to every other node
- Best for small networks (< 10 nodes)
- Low latency, high redundancy
- O(n²) connections

### Hub-Spoke
- One hub node, all others connect to hub
- Better for larger networks
- Hub becomes single point of failure
- O(n) connections

## Backhaul Integration

1. **Server Creation**: For each node in mesh, create a Backhaul UDP server
   - Unique port per node (hash-based)
   - Server listens on `0.0.0.0:port`
   - Endpoint = `node_ip:port`

2. **WireGuard Endpoint**: WireGuard peers use Backhaul server endpoints
   - `Endpoint = <node-ip>:<backhaul-port>`
   - Backhaul handles NAT traversal
   - WireGuard traffic flows through Backhaul UDP tunnel

## Routing Setup

For each remote LAN subnet:
```bash
ip route add <remote-lan-subnet> dev wg-<mesh-id>
```

Example:
```bash
ip route add 192.168.20.0/24 dev wg-abc123
ip route add 192.168.30.0/24 dev wg-abc123
```

## Monitoring

### Handshake Status
- Query via `wg show <interface>`
- Shows last handshake time per peer
- Indicates connectivity status

### Latency Detection
- Can be measured via ping over WireGuard overlay IPs
- Example: `ping 10.250.0.2` from node IR

### Packet Loss
- Monitor via `wg show <interface> transfer`
- Shows bytes sent/received per peer

## Security Considerations

1. **Key Management**: Private keys stored in database (encrypted at rest recommended)
2. **Key Rotation**: Supported via `/rotate-keys` endpoint
3. **Access Control**: Mesh API requires authentication
4. **Network Isolation**: Each mesh uses unique overlay subnet

## MTU Considerations

- Default MTU: 1280 (conservative for double encapsulation)
- Backhaul overhead: ~40 bytes
- WireGuard overhead: ~32 bytes
- Total overhead: ~72 bytes
- Recommended MTU: 1280-1420 depending on path MTU

## Testing Scenario

### Setup
1. 3 Linux VPS nodes: node-ir, node-tr, node-uae
2. Each node has a LAN subnet:
   - IR: 192.168.10.0/24
   - TR: 192.168.20.0/24
   - UAE: 192.168.30.0/24
3. WireGuard overlay: 10.250.0.0/24

### Test Steps
1. Create mesh via API
2. Apply mesh to all nodes
3. Verify WireGuard interfaces are up
4. Test connectivity: `ping 10.250.0.2` from node IR
5. Test LAN routing: `ping 192.168.20.1` from node IR
6. Check handshake status via status endpoint

## Limitations

1. **Linux Only**: WireGuard requires Linux kernel support
2. **Root Required**: Interface creation and routing require root
3. **Backhaul Dependency**: Mesh requires Backhaul UDP tunnels
4. **NAT Limitations**: Complex NAT scenarios may require additional configuration

## Future Enhancements

1. **Automatic MTU Discovery**: Path MTU discovery for optimal MTU
2. **Health Monitoring**: Automated latency and packet loss monitoring
3. **Auto-reconnect**: Automatic reconnection on failure
4. **Traffic Statistics**: Per-mesh traffic statistics
5. **Frontend UI**: Web interface for mesh management

