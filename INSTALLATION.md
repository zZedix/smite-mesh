# Smite Installation Guide

## Overview

Smite uses a **dual-node architecture** with two types of nodes:

- **Iran Nodes**: Act as servers in reverse tunnels
- **Foreign Servers**: Act as clients in reverse tunnels and receive forwarded traffic

Both node types run the same `smite-node` software but with different roles configured.

---

## 📦 Panel Installation

The panel is installed once and manages all nodes.

### Quick Install

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/main/scripts/install.sh)"
```

### Manual Install

1. **Clone the repository:**
```bash
git clone https://github.com/zZedix/Smite.git
cd Smite
```

2. **Configure environment:**
```bash
cp .env.example .env
# Edit .env with your settings
```

3. **Install CLI tools:**
```bash
sudo bash cli/install_cli.sh
```

4. **Start services:**
```bash
docker compose up -d
```

5. **Create admin user:**
```bash
smite admin create
```

6. **Access web interface:**
   - Open `http://your-panel-ip:8000` in your browser
   - Login with the admin credentials you created

### Get CA Certificates

After panel installation, you need to download CA certificates:

1. **For Iran Nodes**: Go to **Nodes** tab → Click **View CA Certificate** → Copy the certificate
2. **For Foreign Servers**: Go to **Servers** tab → Click **View CA Certificate** → Copy the certificate

Or download from panel server:
```bash
# Iran node CA certificate
cat /path/to/smite/certs/ca.crt

# Foreign server CA certificate  
cat /path/to/smite/certs/ca-server.crt
```

---

## 🖥️ Node Installation

### Understanding Node Types

#### Iran Nodes
- **Purpose**: Run tunnel servers
- **Location**: Usually in Iran or restricted regions
- **Function**: 
  - Hosts FRP servers
  - Receives overlay IPs from IPAM
  - Participates in WireGuard mesh

#### Foreign Servers
- **Purpose**: Run tunnel clients and receive forwarded traffic
- **Location**: Usually outside restricted regions (e.g., Europe, US)
- **Function**:
  - Connects to Iran nodes as client
  - Receives forwarded traffic from Iran nodes
  - Receives overlay IPs from IPAM
  - Participates in WireGuard mesh

### Installation Methods

#### Method 1: Quick Install Script (Recommended)

The installer will guide you through the process:

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/main/scripts/smite-node.sh)"
```

**The installer will ask:**
1. Panel address (e.g., `panel.example.com:443`)
2. Panel port (default: `8000`)
3. Node API port (default: `8888`)
4. Node name (e.g., `node-ir`, `node-tr`)
5. **Node role**: Choose `1` for Iran Node or `2` for Foreign Server
6. **CA Certificate**: Paste the appropriate certificate
   - Iran nodes: Use certificate from **Nodes** tab
   - Foreign servers: Use certificate from **Servers** tab

#### Method 2: Manual Installation

##### Step 1: Prepare Node Server

```bash
# Install Docker (if not installed)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
rm get-docker.sh
```

##### Step 2: Create Node Directory

```bash
mkdir -p /opt/smite-node
cd /opt/smite-node
```

##### Step 3: Get Node Files

```bash
# Clone repository
git clone https://github.com/zZedix/Smite.git /tmp/smite
cp -r /tmp/smite/node/* /opt/smite-node/
rm -rf /tmp/smite
cd /opt/smite-node
```

##### Step 4: Configure Node

Create the CA certificate file:

```bash
mkdir -p certs
# Paste the CA certificate content
nano certs/ca.crt
# For Iran nodes: paste ca.crt from panel
# For Foreign servers: paste ca-server.crt from panel
```

Create `.env` file:

```bash
cat > .env << EOF
# Node Configuration
NODE_API_PORT=8888
NODE_NAME=node-1
NODE_ROLE=iran

# Panel Connection
PANEL_CA_PATH=/etc/smite-node/certs/ca.crt
PANEL_ADDRESS=panel.example.com:443
PANEL_API_PORT=8000
EOF
```

**Important**: Set `NODE_ROLE` to:
- `iran` for Iran nodes
- `foreign` for Foreign servers

##### Step 5: Start Node

```bash
docker compose up -d
```

##### Step 6: Verify Installation

```bash
# Check node status
docker compose ps

# View logs
docker compose logs -f

# Check if node registered with panel
# Look for "Node registered successfully" in logs
```

---

## 🔧 Configuration Examples

### Iran Node Configuration

```bash
# .env file for Iran Node
NODE_API_PORT=8888
NODE_NAME=node-iran-1
NODE_ROLE=iran
PANEL_CA_PATH=/etc/smite-node/certs/ca.crt
PANEL_ADDRESS=panel.example.com:443
PANEL_API_PORT=8000
```

**What this node does:**
- Registers as "iran" role in panel
- Receives overlay IP from IPAM (e.g., `10.250.0.1`)
- Can run FRP servers
- Can participate in WireGuard mesh

### Foreign Server Configuration

```bash
# .env file for Foreign Server
NODE_API_PORT=8888
NODE_NAME=server-foreign-1
NODE_ROLE=foreign
PANEL_CA_PATH=/etc/smite-node/certs/ca.crt
PANEL_ADDRESS=panel.example.com:443
PANEL_API_PORT=8000
```

**What this server does:**
- Registers as "foreign" role in panel
- Receives overlay IP from IPAM (e.g., `10.250.0.2`)
- Connects to Iran nodes as client
- Receives forwarded traffic from Iran nodes
- Can participate in WireGuard mesh
- Appears in "Servers" tab (not "Nodes" tab)

---

## 📋 Installation Checklist

### Panel Installation
- [ ] Docker and Docker Compose installed
- [ ] Repository cloned
- [ ] `.env` file configured
- [ ] CLI tools installed (`smite` command)
- [ ] Services started (`docker compose up -d`)
- [ ] Admin user created (`smite admin create`)
- [ ] Web interface accessible
- [ ] CA certificates downloaded

### Iran Node Installation
- [ ] Docker installed on node server
- [ ] Node files copied to `/opt/smite-node`
- [ ] `ca.crt` certificate saved to `certs/ca.crt`
- [ ] `.env` file created with `NODE_ROLE=iran`
- [ ] Panel address configured correctly
- [ ] Node started (`docker compose up -d`)
- [ ] Node appears in panel's "Nodes" tab
- [ ] Node received overlay IP (check in panel or Overlay IP tab)

### Foreign Server Installation
- [ ] Docker installed on server
- [ ] Server files copied to `/opt/smite-node`
- [ ] `ca-server.crt` certificate saved to `certs/ca.crt`
- [ ] `.env` file created with `NODE_ROLE=foreign`
- [ ] Panel address configured correctly
- [ ] Server started (`docker compose up -d`)
- [ ] Server appears in panel's "Servers" tab
- [ ] Server received overlay IP (check in panel or Overlay IP tab)

---

## 🚀 Quick Start Example

### Scenario: Connect 3 Servers

1. **Install Panel** (on any server):
```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/main/scripts/install.sh)"
smite admin create
```

2. **Install Iran Node** (in Iran):
```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/main/scripts/smite-node.sh)"
# Choose role: 1 (Iran Node)
# Use ca.crt from Nodes tab
```

3. **Install Foreign Server** (outside Iran):
```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/main/scripts/smite-node.sh)"
# Choose role: 2 (Foreign Server)
# Use ca-server.crt from Servers tab
```

4. **Create Overlay Pool** (in panel):
   - Go to **Overlay IP** tab
   - Create pool: `10.250.0.0/24`
   - Nodes automatically receive overlay IPs

5. **Create WireGuard Mesh** (in panel):
   - Go to **WireGuard Mesh** tab
   - Select nodes
   - Specify LAN subnets
   - Apply mesh

6. **Result**: All servers connected via WireGuard with LAN-to-LAN routing!

---

## 🔍 Verification

### Check Panel Status
```bash
smite status
```

### Check Node Status
```bash
smite-node status
```

### View Logs
```bash
# Panel logs
smite logs

# Node logs
smite-node logs
```

### Verify in Web UI
1. **Nodes Tab**: Should show Iran nodes with overlay IPs
2. **Servers Tab**: Should show Foreign servers
3. **Overlay IP Tab**: Should show pool status and assignments
4. **WireGuard Mesh Tab**: Should show created meshes

---

## ❓ Common Issues

### Node Not Appearing in Panel
- Check node logs: `smite-node logs`
- Verify CA certificate is correct
- Ensure panel address is reachable
- Check firewall allows connection

### Wrong CA Certificate
- Iran nodes must use `ca.crt` (from Nodes tab)
- Foreign servers must use `ca-server.crt` (from Servers tab)
- Certificate must match node role

### Overlay IP Not Assigned
- Ensure IPAM pool is created
- Verify node/server is registered in panel
- Check Overlay IP tab for assignments
- Both Iran nodes and Foreign servers receive overlay IPs

### Mesh Not Working
- Ensure all nodes have overlay IPs
- Check FRP tunnels are created
- Verify WireGuard interfaces are up
- Check routes are configured

---

## 📚 Next Steps

After installation:
1. Create overlay IP pool
2. Create WireGuard mesh for site-to-site VPN
3. Create tunnels for port forwarding
4. Configure LAN subnets for routing

See the main [README.md](README.md) for more details on features and usage.

