#!/bin/bash
# Smite Node Installer

set -e

echo "=== Smite Node Installer ==="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root (use sudo)"
    exit 1
fi

# Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
fi

# Get installation directory
INSTALL_DIR="/opt/smite-node"
echo "Installing to: $INSTALL_DIR"

# Clone from GitHub if needed
if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    echo "Smite Node already installed in $INSTALL_DIR"
    cd "$INSTALL_DIR"
else
    echo "Setting up Smite Node..."
    mkdir -p "$INSTALL_DIR"
    cd "$INSTALL_DIR"
    
    # Clone just the node files or create minimal structure
    # For now, we'll create the necessary files
fi

# Prompt for configuration
echo ""
echo "Configuration:"

read -p "Panel CA certificate path: " PANEL_CA_PATH
if [ ! -f "$PANEL_CA_PATH" ]; then
    echo "Error: CA certificate not found at $PANEL_CA_PATH"
    exit 1
fi

read -p "Panel address (host:port, e.g., panel.example.com:443): " PANEL_ADDRESS
if [ -z "$PANEL_ADDRESS" ]; then
    echo "Error: Panel address is required"
    exit 1
fi

read -p "Node API port (default: 8888): " NODE_API_PORT
NODE_API_PORT=${NODE_API_PORT:-8888}

read -p "Node name (default: node-1): " NODE_NAME
NODE_NAME=${NODE_NAME:-node-1}

# Copy CA certificate
mkdir -p certs
cp "$PANEL_CA_PATH" certs/ca.crt

# Create .env file
cat > .env << EOF
NODE_API_PORT=$NODE_API_PORT
NODE_NAME=$NODE_NAME

PANEL_CA_PATH=/etc/smite-node/certs/ca.crt
PANEL_ADDRESS=$PANEL_ADDRESS
EOF

# Clone node files from GitHub
if [ ! -f "Dockerfile" ]; then
    echo "Cloning node files from GitHub..."
    TEMP_DIR=$(mktemp -d)
    git clone https://github.com/zZedix/Smite.git "$TEMP_DIR" || {
        echo "Error: Failed to clone repository"
        exit 1
    }
    
    # Copy node files
    cp -r "$TEMP_DIR/node"/* .
    rm -rf "$TEMP_DIR"
fi

# Install CLI
if [ -f "/opt/smite/cli/smite-node.py" ]; then
    sudo cp /opt/smite/cli/smite-node.py /usr/local/bin/smite-node
    sudo chmod +x /usr/local/bin/smite-node
elif [ -f "$INSTALL_DIR/../Smite/cli/smite-node.py" ]; then
    sudo cp "$INSTALL_DIR/../Smite/cli/smite-node.py" /usr/local/bin/smite-node
    sudo chmod +x /usr/local/bin/smite-node
else
    # Download CLI directly
    echo "Downloading CLI tool..."
    sudo curl -L https://raw.githubusercontent.com/zZedix/Smite/main/cli/smite-node.py -o /usr/local/bin/smite-node
    sudo chmod +x /usr/local/bin/smite-node
fi

# Create config directory
mkdir -p config

# Start services
echo ""
echo "Starting Smite Node..."
docker compose up -d

# Wait for services
echo "Waiting for services to start..."
sleep 5

# Check status
if docker ps | grep -q smite-node; then
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

