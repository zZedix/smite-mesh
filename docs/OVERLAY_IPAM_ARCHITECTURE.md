# Overlay IP Management (IPAM) - Architecture

## Overview

The Overlay IP Management (IPAM) feature provides centralized control over overlay IP address assignment for Smite nodes. This enables node identity within mesh networks and supports site-to-site VPN routing.

## Architecture Layers

### 1. Control Plane (Smite Panel)
- **IPAMManager**: Handles IP allocation, assignment, and release
- **Overlay API**: REST endpoints for pool and assignment management
- **Database**: Stores pool configuration and node-to-IP mappings

### 2. Node Agent
- **OverlayManager**: Receives IP assignments and configures WireGuard interfaces
- **Agent API**: Endpoints for receiving and applying overlay IPs

### 3. Data Layer
- **OverlayPool**: Stores CIDR pool configuration
- **OverlayAssignment**: Maps nodes to overlay IPs

## Key Components

### Panel Side

#### `ipam_manager.py`
- `get_or_create_pool()`: Creates or retrieves overlay pool
- `allocate_ip()`: Allocates IP from pool (first-free algorithm)
- `release_ip()`: Releases IP when node is deleted
- `update_node_ip()`: Manual IP override
- `get_pool_status()`: Returns pool utilization statistics
- `list_assignments()`: Lists all node-to-IP mappings

#### `routers/overlay.py`
- `POST /api/overlay/pool`: Create/update overlay pool
- `GET /api/overlay/pool`: Get pool configuration
- `POST /api/overlay/assign/{node_id}`: Assign IP to node
- `PUT /api/overlay/assign/{node_id}`: Update IP assignment (manual override)
- `DELETE /api/overlay/release/{node_id}`: Release IP from node
- `GET /api/overlay/status`: Get pool status and statistics
- `GET /api/overlay/assignments`: List all assignments
- `GET /api/overlay/node/{node_id}`: Get IP for specific node

### Node Side

#### `overlay_manager.py`
- `assign_ip()`: Assigns IP to WireGuard interface using `ip addr add`
- `remove_ip()`: Removes IP from interface using `ip addr del`
- `get_current_ip()`: Queries current IP from interface
- `ensure_interface_exists()`: Creates WireGuard interface if needed

#### `routers/agent.py` (overlay endpoints)
- `POST /api/agent/overlay/assign`: Receive and apply overlay IP
- `POST /api/agent/overlay/remove`: Remove overlay IP
- `GET /api/agent/overlay/status`: Get current overlay IP status

## IP Allocation Algorithm

### First-Free Algorithm
1. Query all existing assignments from database
2. Iterate through network hosts (excluding network/broadcast)
3. Return first IP not in assigned set
4. If pool exhausted, return None

### Manual Override
- Admin can specify preferred IP
- Validates IP is in pool
- Checks IP is not already assigned
- Updates or creates assignment

## Integration Points

### Node Registration
- When Iran node registers, automatically allocate overlay IP
- IP stored in `node_metadata["overlay_ip"]`
- Foreign nodes do not receive overlay IPs

### Node Deletion
- Overlay IP automatically released when node deleted
- Prevents IP pool fragmentation

## Database Schema

### OverlayPool
```python
id: str (UUID)
cidr: str (unique)  # e.g., "10.250.0.0/24"
description: str (optional)
created_at: datetime
updated_at: datetime
```

### OverlayAssignment
```python
id: str (UUID)
node_id: str (unique)  # Foreign key to Node
overlay_ip: str (unique)  # e.g., "10.250.0.1"
interface_name: str (default: "wg0")
assigned_at: datetime
updated_at: datetime
```

## IP Assignment Flow

1. **Node Registration**:
   ```
   Node registers → Panel allocates IP → IP stored in DB → 
   IP sent to node agent → Agent configures WireGuard interface
   ```

2. **Manual Assignment**:
   ```
   Admin assigns IP via API → IP validated → Assignment created/updated →
   IP sent to node agent → Agent updates interface
   ```

3. **IP Release**:
   ```
   Node deleted → Assignment removed from DB → 
   Release command sent to agent → Agent removes IP from interface
   ```

## WireGuard Interface Configuration

### Interface Creation
- Uses `wg-quick up <interface>` if interface doesn't exist
- Falls back to manual `ip link` commands if needed

### IP Assignment
```bash
ip addr add 10.250.0.1/32 dev wg0
```

### IP Removal
```bash
ip addr del 10.250.0.1/32 dev wg0
```

## Frontend Features

### Overlay Management Page
- Pool status dashboard (total, assigned, available, utilization)
- Utilization progress bar with color coding
- Assignment table (node name, IP, interface, assigned date)
- Pool creation modal

### Nodes Page Integration
- Overlay IP column in nodes table
- Shows "Not assigned" if no IP allocated
- IP displayed in monospace font with blue color

## Pool Status Monitoring

### Metrics
- **Total IPs**: Network size - 2 (network + broadcast)
- **Assigned IPs**: Count of active assignments
- **Available IPs**: Total - Assigned
- **Utilization**: (Assigned / Total) * 100%

### Warnings
- Pool exhausted when available_ips = 0
- High utilization warning at 90%+
- Visual indicators (red/yellow/green) based on utilization

## Security Considerations

1. **IP Validation**: All IPs validated against pool CIDR
2. **Duplicate Prevention**: Database unique constraints prevent duplicate assignments
3. **Node Verification**: IP assignment requires valid node ID
4. **Interface Isolation**: Each node manages its own interface

## Use Cases

### WireGuard Mesh
- Each node gets unique overlay IP
- Mesh uses overlay IPs for peer endpoints
- Enables routing between LAN subnets

### Site-to-Site VPN
- Nodes identified by overlay IPs
- Routing tables use overlay IPs
- Health checks use overlay IPs

### Node Identity
- Overlay IP serves as stable node identifier
- Independent of public IP changes
- Works behind NAT

## Limitations

1. **Single Pool**: Currently supports one overlay pool (can be extended)
2. **IPv4 Only**: IPv6 support can be added
3. **Static Assignment**: No DHCP (by design)
4. **Linux Only**: Requires `ip` command and WireGuard support

## Future Enhancements

1. **Multiple Pools**: Support for multiple overlay networks
2. **IPv6 Support**: Dual-stack overlay networks
3. **Reservation System**: Reserve IP ranges for specific purposes
4. **Automatic Reassignment**: Auto-reassign on node failure
5. **IP History**: Track IP assignment history
6. **Bulk Operations**: Assign/release multiple IPs at once

