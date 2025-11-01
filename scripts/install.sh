#!/bin/bash
# Smite Panel Installer

set -e

echo "=== Smite Panel Installer ==="
echo ""

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root (use sudo)"
    exit 1
fi

# Detect OS
OS="$(uname -s)"
ARCH="$(uname -m)"

# Install Docker if not present
if ! command -v docker &> /dev/null; then
    echo "Docker not found. Installing Docker..."
    curl -fsSL https://get.docker.com -o get-docker.sh
    sh get-docker.sh
    rm get-docker.sh
fi

# Install docker-compose if not present
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "docker-compose not found. Installing..."
    # Docker Compose V2 is included with Docker Desktop or can be installed separately
    echo "Please install docker-compose separately"
    exit 1
fi

# Get installation directory
INSTALL_DIR="/opt/smite"
echo "Installing to: $INSTALL_DIR"

# Check if directory exists and has files
if [ -d "$INSTALL_DIR" ] && [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
    echo "Smite already installed in $INSTALL_DIR"
    cd "$INSTALL_DIR"
else
    # Clone from GitHub
    echo "Cloning Smite from GitHub..."
    rm -rf "$INSTALL_DIR"
    git clone https://github.com/zZedix/Smite.git "$INSTALL_DIR" || {
        echo "Error: Failed to clone repository"
        echo "Make sure git is installed: apt-get install -y git"
        exit 1
    }
    cd "$INSTALL_DIR"
fi

# Prompt for configuration
echo ""
echo "Configuration:"
read -p "Panel port (default: 8000): " PANEL_PORT
PANEL_PORT=${PANEL_PORT:-8000}

read -p "Database type [sqlite/mysql] (default: sqlite): " DB_TYPE
DB_TYPE=${DB_TYPE:-sqlite}

# Create .env file
cat > .env << EOF
PANEL_PORT=$PANEL_PORT
PANEL_HOST=0.0.0.0
HTTPS_ENABLED=false
DOCS_ENABLED=true

DB_TYPE=$DB_TYPE
DB_PATH=./data/smite.db

HYSTERIA2_PORT=443
HYSTERIA2_CERT_PATH=./certs/ca.crt
HYSTERIA2_KEY_PATH=./certs/ca.key

SECRET_KEY=$(openssl rand -hex 32)
EOF

if [ "$DB_TYPE" = "mysql" ]; then
    read -p "MySQL host (default: localhost): " DB_HOST
    DB_HOST=${DB_HOST:-localhost}
    read -p "MySQL port (default: 3306): " DB_PORT
    DB_PORT=${DB_PORT:-3306}
    read -p "MySQL database name: " DB_NAME
    read -p "MySQL user: " DB_USER
    read -sp "MySQL password: " DB_PASSWORD
    echo ""
    
    cat >> .env << EOF

DB_HOST=$DB_HOST
DB_PORT=$DB_PORT
DB_NAME=$DB_NAME
DB_USER=$DB_USER
DB_PASSWORD=$DB_PASSWORD
EOF
fi

# Create necessary directories
mkdir -p panel/data panel/certs

# Generate CA certificate if not exists
if [ ! -f "panel/certs/ca.crt" ]; then
    echo "Generating CA certificate..."
    # This would use the panel's cert generation, but for now create a placeholder
    # The panel will generate it on first run
    touch panel/certs/ca.crt panel/certs/ca.key
fi

# Install CLI
if [ -f "cli/install_cli.sh" ]; then
    bash cli/install_cli.sh
fi

# Start services
echo ""
echo "Starting Smite Panel..."
docker compose up -d

# Wait for services
echo "Waiting for services to start..."
sleep 5

# Check status
if docker ps | grep -q smite-panel; then
    echo ""
    echo "✅ Smite Panel installed successfully!"
    echo ""
    echo "Panel URL: http://localhost:$PANEL_PORT"
    echo "API Docs: http://localhost:$PANEL_PORT/docs"
    echo ""
    echo "Next steps:"
    echo "  1. Create admin user: smite admin create"
    echo "  2. Access the web interface at http://localhost:$PANEL_PORT"
    echo ""
else
    echo "❌ Installation completed but panel is not running"
    echo "Check logs with: docker compose -f docker/docker-compose.yml logs"
    exit 1
fi

