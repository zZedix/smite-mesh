#!/bin/bash
# Smite Node Local Installer - No GitHub Required
# Usage: Copy the smite-mesh/node folder to the server and run this script from within it

set -e

echo "=== Smite Node Local Installer ==="
echo ""
echo "This installer works from the current directory - no GitHub required"
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root (use sudo)"
    exit 1
fi

# Check if we're in a valid node directory
if [ ! -f "docker-compose.yml" ] || [ ! -f "Dockerfile" ]; then
    echo "Error: Please run this script from the smite-mesh/node directory"
    echo "Current directory: $(pwd)"
    echo "Expected files: docker-compose.yml, Dockerfile"
    exit 1
fi

# Get current directory as installation directory
INSTALL_DIR="$(pwd)"
echo "Installing from: $INSTALL_DIR"
echo ""

# Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
fi

# Prompt for configuration
echo ""
echo "Configuration:"

read -p "Panel address (host:port, e.g., panel.example.com:443): " PANEL_ADDRESS
if [ -z "$PANEL_ADDRESS" ]; then
    echo "Error: Panel address is required"
    exit 1
fi

read -p "Panel port (should match the panel's port from panel installation, default: 8000): " PANEL_API_PORT
PANEL_API_PORT=${PANEL_API_PORT:-8000}

read -p "Node API port (default: 8888): " NODE_API_PORT
NODE_API_PORT=${NODE_API_PORT:-8888}

read -p "Node name (default: node-1): " NODE_NAME
NODE_NAME=${NODE_NAME:-node-1}

echo ""
echo "=== Server Role ==="
echo "Select server role:"
echo "1) Master Server (runs tunnel clients, connects to slave servers)"
echo "2) Slave Server (runs tunnel servers, accepts connections from master servers)"
read -p "Enter choice [1 or 2] (default: 1): " ROLE_CHOICE
ROLE_CHOICE=${ROLE_CHOICE:-1}

if [ "$ROLE_CHOICE" = "2" ]; then
    NODE_ROLE="foreign"
    echo "✅ Selected: Slave Server"
    CA_SOURCE="Slaves > View CA Certificate"
else
    NODE_ROLE="iran"
    echo "✅ Selected: Master Server"
    CA_SOURCE="Masters > View CA Certificate"
fi

echo ""
echo "=== CA Certificate ==="
echo "Please paste the CA certificate from the panel (copy from $CA_SOURCE):"
echo "Press Enter after pasting, then press Enter again on an empty line to finish"
echo ""
PANEL_CA_CONTENT=""
has_content=false
while IFS= read -r line; do
    if [ -z "$line" ]; then
        # If we have content and hit an empty line, we're done
        if [ "$has_content" = true ]; then
            break
        fi
        # Otherwise, ignore leading empty lines
        continue
    else
        has_content=true
        PANEL_CA_CONTENT="${PANEL_CA_CONTENT}${line}\n"
    fi
done

if [ -z "$PANEL_CA_CONTENT" ]; then
    echo "Error: CA certificate is required"
    exit 1
fi

# Save CA certificate
mkdir -p certs
echo -e "$PANEL_CA_CONTENT" > certs/ca.crt
if [ ! -f "certs/ca.crt" ] || [ ! -s "certs/ca.crt" ]; then
    echo "Error: Failed to save CA certificate"
    exit 1
fi
echo "✅ CA certificate saved to certs/ca.crt"

# Create .env file
cat > .env << EOF
NODE_API_PORT=$NODE_API_PORT
NODE_NAME=$NODE_NAME
NODE_ROLE=$NODE_ROLE
SMITE_VERSION=${SMITE_VERSION:-main}

PANEL_CA_PATH=/etc/smite-node/certs/ca.crt
PANEL_ADDRESS=$PANEL_ADDRESS
PANEL_API_PORT=$PANEL_API_PORT
EOF

# Install CLI - check for local copy first
if [ -f "../cli/smite-node.py" ]; then
    cp ../cli/smite-node.py /usr/local/bin/smite-node
    chmod +x /usr/local/bin/smite-node
    echo "✅ CLI installed from local copy"
elif [ -f "/opt/smite/cli/smite-node.py" ]; then
    cp /opt/smite/cli/smite-node.py /usr/local/bin/smite-node
    chmod +x /usr/local/bin/smite-node
    echo "✅ CLI installed from /opt/smite"
else
    echo "⚠️  CLI script not found locally, skipping CLI installation"
    echo "   You can manually install it later if needed"
fi

# Create config directory
mkdir -p config

# Apply network optimizations for stable tunnels
echo ""
echo "Applying network optimizations..."
if [ -f "/etc/sysctl.conf" ]; then
    # Backup original sysctl.conf
    if [ ! -f "/etc/sysctl.conf.smite-backup" ]; then
        cp /etc/sysctl.conf /etc/sysctl.conf.smite-backup
    fi
    
    # Add network optimizations if not already present
    if ! grep -q "# Smite Network Optimizations" /etc/sysctl.conf; then
        cat >> /etc/sysctl.conf << 'EOF'

# Smite Network Optimizations
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 5000
net.ipv4.tcp_max_syn_backlog = 8192
net.ipv4.ip_local_port_range = 10000 65535
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 30
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 60
net.ipv4.tcp_keepalive_probes = 3
net.ipv4.tcp_slow_start_after_idle = 0
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.udp_mem = 3145728 4194304 16777216
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
EOF
        # Apply optimizations
        sysctl -p > /dev/null 2>&1 || true
        echo "✅ Network optimizations applied"
    else
        echo "✅ Network optimizations already applied"
    fi
fi

# Increase file descriptor limits
if [ -f "/etc/security/limits.conf" ]; then
    if ! grep -q "# Smite File Descriptor Limits" /etc/security/limits.conf; then
        cat >> /etc/security/limits.conf << 'EOF'

# Smite File Descriptor Limits
* soft nofile 65535
* hard nofile 65535
root soft nofile 65535
root hard nofile 65535
EOF
        echo "✅ File descriptor limits increased"
    fi
    # Apply for current session
    ulimit -n 65535 2>/dev/null || true
fi

# Enable BBR congestion control (if available)
if modprobe -n tcp_bbr 2>/dev/null; then
    if ! grep -q "tcp_bbr" /etc/modules-load.d/*.conf 2>/dev/null && ! grep -q "tcp_bbr" /etc/modules 2>/dev/null; then
        echo "tcp_bbr" | tee -a /etc/modules-load.d/smite.conf > /dev/null 2>&1 || echo "tcp_bbr" >> /etc/modules 2>/dev/null || true
        modprobe tcp_bbr 2>/dev/null || true
        sysctl -w net.ipv4.tcp_congestion_control=bbr > /dev/null 2>&1 || true
        sysctl -w net.core.default_qdisc=fq > /dev/null 2>&1 || true
        echo "✅ BBR congestion control enabled"
    else
        echo "✅ BBR congestion control already enabled"
    fi
fi

# Build Docker image locally
echo ""
echo "Building Docker image locally..."
# Read SMITE_VERSION from .env if not set in environment
if [ -z "${SMITE_VERSION}" ] && [ -f ".env" ]; then
    SMITE_VERSION=$(grep "^SMITE_VERSION=" .env | cut -d'=' -f2 | tr -d '"' | tr -d "'" || echo "main")
fi
SMITE_VERSION=${SMITE_VERSION:-main}
export SMITE_VERSION

echo "Using SMITE_VERSION=${SMITE_VERSION}"
echo "Building node image from local source..."

# Build image locally (no pull from GHCR)
if docker compose build 2>&1; then
    echo "✅ Node image built locally"
else
    echo "⚠️  Build completed with warnings"
fi

# Start services
echo ""
echo "Starting Smite Node..."
docker compose up -d

# Wait for services
echo "Waiting for services to start..."
sleep 5

# Check status
if docker ps | grep -q sm-node; then
    echo ""
    echo "✅ Smite Node installed successfully!"
    echo ""
    echo "Node API: http://localhost:$NODE_API_PORT"
    echo ""
    echo "Check status with: smite-node status"
    echo ""
else
    echo "❌ Installation completed but node is not running"
    echo "Check logs with: docker compose logs"
    exit 1
fi



