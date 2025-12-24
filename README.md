# Smite - Tunneling Control Panel

## Panel Installation

### Quick Install

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/main/scripts/install.sh)"
```

### Manual Install

```bash
git clone https://github.com/zZedix/Smite.git
cd Smite
cp .env.example .env
sudo bash cli/install_cli.sh
docker compose up -d
smite admin create
```

## Node Installation

### Quick Install

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/main/scripts/smite-node.sh)"
```

### Manual Install

```bash
cd node
mkdir -p certs
# Copy CA certificate from panel to certs/ca.crt
cat > .env << EOF
NODE_API_PORT=8888
NODE_NAME=node-1
NODE_ROLE=iran
PANEL_CA_PATH=/etc/smite-node/certs/ca.crt
PANEL_ADDRESS=panel.example.com:443
PANEL_API_PORT=8000
EOF
docker compose up -d
```
