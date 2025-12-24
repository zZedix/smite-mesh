# Smite - Tunneling Control Panel

<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/SmiteD.png"/>
    <source media="(prefers-color-scheme: light)" srcset="assets/SmiteL.png"/>
    <img src="assets/SmiteL.png" alt="Smite Logo" width="200"/>
  </picture>
  
  **Modern tunnel management built on FRP, featuring dual-node architecture, intuitive WebUI, real-time status tracking, and open-source freedom.**
  
  [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
  [![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
  [![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-009688.svg)](https://fastapi.tiangolo.com/)
  [![React](https://img.shields.io/badge/React-18+-61DAFB.svg)](https://reactjs.org/)
  [![TypeScript](https://img.shields.io/badge/TypeScript-5.0+-3178C6.svg)](https://www.typescriptlang.org/)
  [![Docker](https://img.shields.io/badge/Docker-24.0+-2496ED.svg)](https://www.docker.com/)
  [![Nginx](https://img.shields.io/badge/Nginx-1.25+-009639.svg)](https://www.nginx.com/)
  [![SQLite](https://img.shields.io/badge/SQLite-3.42+-003B57.svg)](https://www.sqlite.org/)
</div>

---

## 🚀 Features

- **FRP Tunnels**: Support for TCP and UDP reverse tunnels via FRP
- **Dual-Node Architecture**: Iran nodes act as servers, Foreign servers act as clients for reverse tunnels
- **Docker-First**: Easy deployment with Docker Compose
- **Web UI**: Modern, intuitive web interface with real-time connection status tracking
- **CLI Tools**: Powerful command-line tools for management
- **Node Support**: Easy reverse tunnel setup with FRP nodes

---

## 📋 Prerequisites

- Docker and Docker Compose installed
- For Iran servers, install Docker first:
  ```bash
  curl -fsSL https://raw.githubusercontent.com/manageitir/docker/main/install-ubuntu.sh | sh
  ```

---

## 🔧 Panel Installation

### Quick Install

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/main/scripts/install.sh)"
```

### Manual Install

1. Clone the repository:
```bash
git clone https://github.com/zZedix/Smite.git
cd Smite
```

2. Copy environment file and configure:
```bash
cp .env.example .env
# Edit .env with your settings
```
> **Tip**: To free ports `80/443` for tunnels, set `SMITE_HTTP_PORT` and `SMITE_HTTPS_PORT` in `.env`. Nginx will render its configuration from `nginx/default.conf.template` using these values.

3. Install CLI tools:
```bash
sudo bash cli/install_cli.sh
```

4. Start services:
```bash
docker compose up -d
```

5. Create admin user:
```bash
smite admin create
```

6. Access the web interface at `http://localhost:8000`

### CA Certificates

The panel generates two separate CA certificates:
- **`ca.crt`** / **`ca.key`**: Used for Iran nodes
- **`ca-server.crt`** / **`ca-server.key`**: Used for Foreign servers

Both certificates are available in the panel's `certs/` directory and can be downloaded from the Servers page in the web UI.

---

## 🖥️ Node Installation

> **Note**: Nodes are used for **FRP** tunnels, providing easy reverse tunnel functionality.

### Node Types

Smite uses a **dual-node architecture**:

- **Iran Nodes**: 
  - Act as servers in reverse tunnels (FRP)
  - Receive overlay IPs from IPAM
  - Participate in WireGuard mesh
  - Use `ca.crt` certificate (from Nodes tab)

- **Foreign Servers**: 
  - Act as clients in reverse tunnels
  - Receive forwarded traffic from Iran nodes
  - Do NOT receive overlay IPs
  - Use `ca-server.crt` certificate (from Servers tab)

### Quick Install

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/main/scripts/smite-node.sh)"
```

The installer will prompt for:
- Panel address (host:port)
- Panel port (default: 8000)
- Node API port (default: 8888)
- Node name (default: node-1)
- **Node role**: Choose `1` for Iran Node or `2` for Foreign Server
- **CA Certificate**: Paste the appropriate certificate
  - Iran nodes: Certificate from **Nodes** tab
  - Foreign servers: Certificate from **Servers** tab

### Manual Install

1. Navigate to node directory:
```bash
cd node
```

2. Copy Panel CA certificate:
```bash
mkdir -p certs
# For Iran nodes, use ca.crt (from Nodes tab)
cp /path/to/panel/ca.crt certs/ca.crt
# For Foreign servers, use ca-server.crt (from Servers tab)
# cp /path/to/panel/ca-server.crt certs/ca.crt
```

3. Create `.env` file:
```bash
cat > .env << EOF
NODE_API_PORT=8888
NODE_NAME=node-1
NODE_ROLE=iran
PANEL_CA_PATH=/etc/smite-node/certs/ca.crt
PANEL_ADDRESS=panel.example.com:443
PANEL_API_PORT=8000
EOF
```

> **Important**: Set `NODE_ROLE=iran` for Iran nodes or `NODE_ROLE=foreign` for Foreign servers. The panel validates node roles during registration.

4. Start node:
```bash
docker compose up -d
```

### Detailed Installation Guide

For comprehensive installation instructions, examples, and troubleshooting, see [INSTALLATION.md](INSTALLATION.md).

---

## 🛠️ CLI Tools

### Panel CLI (`smite`)

**Admin Management:**
```bash
smite admin create      # Create admin user
smite admin update      # Update admin password
```

**Panel Management:**
```bash
smite status            # Show system status
smite update            # Update panel (pull images and recreate)
smite restart           # Restart panel (recreate to pick up .env changes)
smite logs              # View panel logs
```

**Configuration:**
```bash
smite edit              # Edit docker-compose.yml
smite edit-env          # Edit .env file
```

### Node CLI (`smite-node`)

**Node Management:**
```bash
smite-node status       # Show node status
smite-node update       # Update node (pull images and recreate)
smite-node restart      # Restart node (recreate to pick up .env changes)
smite-node logs         # View node logs
```

**Configuration:**
```bash
smite-node edit         # Edit docker-compose.yml
smite-node edit-env     # Edit .env file
```

---

## 📖 Tunnel Types

### FRP Tunnels (Reverse Tunnel)
FRP (Fast Reverse Proxy) tunnels use a dual-node architecture: Iran nodes run the FRP server (frps), and Foreign servers run the FRP client (frpc). They provide reliable TCP/UDP reverse tunnel functionality. FRP supports both TCP and UDP protocols, with optional IPv6 support for tunneling IPv6 traffic over IPv4 networks.

---

## 📝 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 💰 Donations

If you find Smite useful and want to support its development, consider making a donation:

### Cryptocurrency Donations

- **Bitcoin (BTC)**: `bc1q637gahjssmv9g3903j88tn6uyy0w2pwuvsp5k0`
- **Ethereum (ETH)**: `0x5B2eE8970E3B233F79D8c765E75f0705278098a0`
- **Tron (TRX)**: `TSAsosG9oHMAjAr3JxPQStj32uAgAUmMp3`
- **USDT (BEP20)**: `0x5B2eE8970E3B233F79D8c765E75f0705278098a0`
- **TON**: `UQA-95WAUn_8pig7rsA9mqnuM5juEswKONSlu-jkbUBUhku6`

### Other Ways to Support

- ⭐ Star the repository if you find it useful
- 🐛 Report bugs and suggest improvements
- 📖 Improve documentation and translations
- 🔗 Share with others who might benefit

---

<div align="center">
  
  **Made with ❤️ by [zZedix](https://github.com/zZedix)**
  
  *Securing the digital world, one line of code at a time!*
  
</div>
