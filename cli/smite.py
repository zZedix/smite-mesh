#!/usr/bin/env python3
"""
Smite Panel CLI
"""
import os
import sys
import subprocess
import argparse
import getpass
from pathlib import Path

# Try to import requests, if not available, install it or use urllib
try:
    import requests
except ImportError:
    try:
        import urllib.request
        import urllib.parse
        import json as json_lib
        HAS_REQUESTS = False
    except ImportError:
        print("Error: Please install requests: pip install requests")
        sys.exit(1)
else:
    HAS_REQUESTS = True


def get_compose_file():
    """Get docker-compose file path"""
    project_root = Path(__file__).parent.parent
    # Try root docker-compose.yml first, then docker/docker-compose.panel.yml
    root_compose = project_root / "docker-compose.yml"
    if root_compose.exists():
        return root_compose
    return project_root / "docker" / "docker-compose.panel.yml"


def get_env_file():
    """Get .env file path"""
    project_root = Path(__file__).parent.parent
    return project_root / ".env"


def get_panel_port():
    """Get panel port from .env file"""
    env_file = get_env_file()
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("PANEL_PORT="):
                return int(line.split("=")[1].strip())
    return 8000


def get_panel_url():
    """Get panel API URL"""
    port = get_panel_port()
    return f"http://localhost:{port}"


def run_docker_compose(args, capture_output=False):
    """Run docker compose command"""
    compose_file = get_compose_file()
    if not compose_file.exists():
        print(f"Error: docker-compose.yml not found at {compose_file}")
        sys.exit(1)
    
    cmd = ["docker", "compose", "-f", str(compose_file)] + args
    result = subprocess.run(cmd, capture_output=capture_output, text=True)
    return result


def cmd_admin_create(args):
    """Create admin user"""
    username = args.username or input("Username: ")
    password = args.password or getpass.getpass("Password: ")
    
    # Try to create via API first (if admin API endpoint exists)
    # Otherwise, try direct database access
    panel_url = get_panel_url()
    
    # For now, use direct database access (requires dependencies)
    # In future, we can add an admin API endpoint
    try:
        # Add panel to path for imports
        project_root = Path(__file__).parent.parent
        panel_path = project_root / "panel"
        if not panel_path.exists():
            print("Error: Panel directory not found")
            sys.exit(1)
        
        sys.path.insert(0, str(panel_path))
        
        from app.database import AsyncSessionLocal, init_db
        from app.models import Admin
        from sqlalchemy import select
        from passlib.context import CryptContext
        import asyncio
        
        pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
        
        async def create():
            await init_db()
            async with AsyncSessionLocal() as session:
                # Check if admin exists
                result = await session.execute(select(Admin).where(Admin.username == username))
                existing = result.scalar_one_or_none()
                if existing:
                    print(f"Error: Admin user '{username}' already exists")
                    return
                
                # Create admin
                password_hash = pwd_context.hash(password)
                admin = Admin(username=username, password_hash=password_hash)
                session.add(admin)
                await session.commit()
                print(f"Admin user '{username}' created successfully!")
        
        asyncio.run(create())
        
    except ImportError as e:
        print("Error: Panel dependencies not installed.")
        print("Installing required dependencies...")
        
        # Try to install dependencies
        panel_requirements = project_root / "panel" / "requirements.txt"
        if panel_requirements.exists():
            subprocess.run([
                sys.executable, "-m", "pip", "install", 
                "passlib[bcrypt]", "sqlalchemy", "aiosqlite", 
                "cryptography", "python-jose[cryptography]"
            ], check=False)
            # Try again
            try:
                from app.database import AsyncSessionLocal, init_db
                from app.models import Admin
                from sqlalchemy import select
                from passlib.context import CryptContext
                import asyncio
                
                pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
                
                async def create():
                    await init_db()
                    async with AsyncSessionLocal() as session:
                        result = await session.execute(select(Admin).where(Admin.username == username))
                        existing = result.scalar_one_or_none()
                        if existing:
                            print(f"Error: Admin user '{username}' already exists")
                            return
                        
                        password_hash = pwd_context.hash(password)
                        admin = Admin(username=username, password_hash=password_hash)
                        session.add(admin)
                        await session.commit()
                        print(f"Admin user '{username}' created successfully!")
                
                asyncio.run(create())
            except Exception as e2:
                print(f"Error: Failed to create admin: {e2}")
                print("\nAlternative: Create admin via Docker:")
                print(f"  docker compose exec smite-panel python -c \"from app.database import AsyncSessionLocal, init_db; from app.models import Admin; from sqlalchemy import select; from passlib.context import CryptContext; import asyncio; pwd = CryptContext(schemes=['bcrypt']); ...\"")
                sys.exit(1)
        else:
            print(f"Error: Could not find panel requirements at {panel_requirements}")
            sys.exit(1)


def cmd_status(args):
    """Show system status"""
    print("Panel Status:")
    print("-" * 50)
    
    # Check docker
    result = subprocess.run(["docker", "ps", "--filter", "name=smite-panel", "--format", "{{.Status}}"], 
                          capture_output=True, text=True)
    if result.stdout.strip():
        print(f"Docker: {result.stdout.strip()}")
    else:
        print("Docker: Not running")
    
    # Check API
    try:
        panel_url = get_panel_url()
        
        if HAS_REQUESTS:
            response = requests.get(f"{panel_url}/api/status", timeout=2)
            if response.status_code == 200:
                data = response.json()
                print(f"API: Running")
                print(f"Nodes: {data['nodes']['active']}/{data['nodes']['total']} active")
                print(f"Tunnels: {data['tunnels']['active']}/{data['tunnels']['total']} active")
            else:
                print("API: Not responding")
        else:
            # Fallback to urllib
            req = urllib.request.Request(f"{panel_url}/api/status")
            with urllib.request.urlopen(req, timeout=2) as response:
                data = json_lib.loads(response.read().decode())
                print(f"API: Running")
                print(f"Nodes: {data['nodes']['active']}/{data['nodes']['total']} active")
                print(f"Tunnels: {data['tunnels']['active']}/{data['tunnels']['total']} active")
    except Exception as e:
        print(f"API: Not accessible ({e})")


def cmd_update(args):
    """Update panel"""
    print("Updating panel...")
    run_docker_compose(["pull"])
    run_docker_compose(["up", "-d", "--force-recreate"])


def cmd_edit(args):
    """Edit docker-compose.yml"""
    compose_file = get_compose_file()
    editor = os.environ.get("EDITOR", "nano")
    subprocess.run([editor, str(compose_file)])


def cmd_edit_env(args):
    """Edit .env file"""
    env_file = get_env_file()
    if not env_file.exists():
        print(f".env file not found. Creating from .env.example...")
        example_file = env_file.parent / ".env.example"
        if example_file.exists():
            env_file.write_text(example_file.read_text())
        else:
            env_file.write_text("")
    
    editor = os.environ.get("EDITOR", "nano")
    subprocess.run([editor, str(env_file)])


def cmd_logs(args):
    """Stream logs"""
    follow = ["--follow"] if args.follow else []
    run_docker_compose(["logs"] + follow + ["smite-panel"])


def main():
    parser = argparse.ArgumentParser(description="Smite Panel CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    # admin create
    admin_parser = subparsers.add_parser("admin", help="Admin management")
    admin_subparsers = admin_parser.add_subparsers(dest="admin_action")
    create_parser = admin_subparsers.add_parser("create", help="Create admin user")
    create_parser.add_argument("--username", help="Username")
    create_parser.add_argument("--password", help="Password")
    
    # status
    subparsers.add_parser("status", help="Show system status")
    
    # update
    subparsers.add_parser("update", help="Update panel")
    
    # edit
    subparsers.add_parser("edit", help="Edit docker-compose.yml")
    
    # edit-env
    subparsers.add_parser("edit-env", help="Edit .env file")
    
    # logs
    logs_parser = subparsers.add_parser("logs", help="View logs")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow logs")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    if args.command == "admin":
        if args.admin_action == "create":
            cmd_admin_create(args)
        else:
            admin_parser.print_help()
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "update":
        cmd_update(args)
    elif args.command == "edit":
        cmd_edit(args)
    elif args.command == "edit-env":
        cmd_edit_env(args)
    elif args.command == "logs":
        cmd_logs(args)


if __name__ == "__main__":
    main()
