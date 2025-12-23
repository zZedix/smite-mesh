# IPAM and WireGuard Mesh Integration

## Overview

The IPAM (Overlay IP Management) system is now fully integrated with WireGuard Mesh, providing a unified overlay network where nodes use persistent, centrally-managed overlay IP addresses.

## Key Changes

### 1. Unified Overlay Network
- **Before**: Each mesh created its own overlay subnet with temporary IPs
- **After**: All meshes use the IPAM pool subnet, and all nodes (both Iran and Foreign) use their persistent IPAM-assigned overlay IPs

### 2. IP Assignment Flow
1. **Node Registration**: All nodes (both Iran and Foreign) automatically receive overlay IPs from IPAM pool
2. **Mesh Creation**: Mesh uses IPAM pool CIDR and existing node overlay IPs
3. **Mesh Application**: WireGuard interfaces use IPAM-assigned IPs

### 3. Architecture

```
┌─────────────────────────────────────────┐
│         Smite Panel (Control Plane)     │
│                                          │
│  ┌──────────────┐    ┌──────────────┐   │
│  │   IPAM Pool  │───▶│  Node IPs    │   │
│  │ 10.250.0.0/24│    │ 10.250.0.1   │   │
│  └──────────────┘    │ 10.250.0.2   │   │
│                      │ 10.250.0.3   │   │
│                      └──────────────┘   │
│                             │            │
│                             ▼            │
│                      ┌──────────────┐   │
│                      │ WireGuard    │   │
│                      │ Mesh Config  │   │
│                      └──────────────┘   │
└─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────┐
│         Node Agent (Data Plane)         │
│                                          │
│  ┌──────────────┐    ┌──────────────┐   │
│  │  wg0 (IPAM)  │    │ wg-{mesh}    │   │
│  │ 10.250.0.1   │    │ 10.250.0.1   │   │
│  └──────────────┘    └──────────────┘   │
│                                          │
│  Note: Mesh interface uses same IP      │
│  as IPAM-assigned overlay IP            │
└─────────────────────────────────────────┘
```

## Implementation Details

### Panel Side Changes

#### `panel/app/routers/mesh.py`
- **Mesh Creation**: 
  - Validates IPAM pool exists
  - Uses IPAM pool CIDR as overlay subnet
  - Allocates IPAM IPs for nodes that don't have them
  - Passes IPAM IPs to mesh config generator

- **Mesh Application**:
  - Includes IPAM overlay IP in mesh spec
  - WireGuard config uses IPAM-assigned IPs

#### `panel/app/wireguard_mesh_manager.py`
- **`create_mesh_config()`**:
  - Now requires `overlay_ip` in node config (from IPAM)
  - Validates IPAM IPs are in overlay subnet
  - Uses IPAM IPs instead of generating new ones

- **`generate_wireguard_config()`**:
  - Uses IPAM-assigned overlay IP in `[Interface]` section
  - Peer configurations use IPAM IPs

### Node Side

#### `node/app/wireguard_adapter.py`
- Mesh interface receives IPAM-assigned overlay IP
- Interface name: `wg-{mesh_id[:8]}`
- IP configuration: `Address = {ipam_overlay_ip}/32`

### Frontend Changes

#### `frontend/src/pages/Mesh.tsx`
- Overlay subnet field auto-populated from IPAM pool
- Field disabled when IPAM pool exists (shows pool CIDR)
- Help text explains IPAM integration
- Fetches pool status on modal open

## User Workflow

### 1. Setup IPAM Pool
```
POST /api/overlay/pool
{
  "cidr": "10.250.0.0/24",
  "description": "Main overlay network"
}
```

### 2. Register Nodes
- Nodes automatically receive overlay IPs from pool
- IPs stored in `node_metadata["overlay_ip"]`
- IPs persist across restarts

### 3. Create Mesh
```
POST /api/mesh/create
{
  "name": "Office Mesh",
  "node_ids": ["node-1", "node-2", "node-3"],
  "lan_subnets": {
    "node-1": "192.168.10.0/24",
    "node-2": "192.168.20.0/24",
    "node-3": "192.168.30.0/24"
  },
  "overlay_subnet": null,  // Uses IPAM pool
  "topology": "full-mesh"
}
```

### 4. Apply Mesh
- WireGuard interfaces created with IPAM-assigned IPs
- Routes configured for LAN subnets
- Backhaul UDP tunnels created as transport

## Benefits

1. **Consistent Node Identity**: Nodes have persistent overlay IPs
2. **Unified Network**: All meshes use same overlay subnet
3. **Centralized Management**: IP allocation controlled by IPAM
4. **No IP Conflicts**: IPAM prevents duplicate assignments
5. **Persistent IPs**: IPs survive mesh deletion/recreation

## IP Persistence

- **IPAM IPs**: Persist across mesh deletion
- **Node Identity**: Overlay IP remains constant
- **Multiple Meshes**: Same node can participate in multiple meshes with same IP
- **IP Release**: Only happens when node is deleted

## Validation

### Mesh Creation Checks
1. IPAM pool must exist
2. Selected overlay subnet must match IPAM pool CIDR
3. All nodes must have IPAM-assigned overlay IPs (auto-allocated if missing)
4. IPAM IPs must be in overlay subnet

### Error Handling
- **No Pool**: Error message guides user to create pool first
- **Mismatched Subnet**: Rejects mesh creation with clear error
- **Pool Exhausted**: Fails gracefully with helpful message

## Migration Notes

### Existing Meshes
- Existing meshes continue to work
- New meshes use IPAM integration
- Old meshes can be recreated to use IPAM

### Backward Compatibility
- Mesh creation still accepts `overlay_subnet` parameter
- If provided, must match IPAM pool CIDR
- If not provided, uses IPAM pool CIDR automatically

## Future Enhancements

1. **Multiple Pools**: Support for multiple overlay networks
2. **IP Reservation**: Reserve IPs for specific purposes
3. **IP History**: Track IP assignment changes
4. **Auto-Reassignment**: Reassign IPs on node failure
5. **Mesh IP Display**: Show IPAM IPs in mesh status

