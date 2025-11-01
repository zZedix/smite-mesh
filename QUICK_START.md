# Quick Start Guide

## Panel Installation (Manual)

If you cloned the repository manually instead of using the installer:

1. **Clone and setup:**
```bash
git clone https://github.com/zZedix/Smite.git
cd Smite
cp .env.example .env
# Edit .env if needed
```

2. **Install CLI tools:**
```bash
sudo bash cli/install_cli.sh
```

Or manually:
```bash
sudo cp cli/smite.py /usr/local/bin/smite
sudo chmod +x /usr/local/bin/smite
```

3. **Start services:**
```bash
docker compose up -d
```

4. **Create admin user:**
```bash
smite admin create
```

5. **Access panel:**
Open http://localhost:8000 in your browser

## Panel Installation (One-line)

```bash
sudo bash -c "$(curl -sL https://raw.githubusercontent.com/zZedix/Smite/master/scripts/install.sh)"
```

This automatically:
- Installs git and Docker
- Clones the repository
- Sets up the environment
- Installs CLI tools
- Starts services

