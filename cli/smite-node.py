#!/usr/bin/env python3
"""
Smite Node CLI
"""
import os
import sys
import subprocess
import argparse
import shutil
from pathlib import Path


def get_compose_file():
    """Get docker-compose file path"""
    possible_roots = [
        Path("/opt/smite-node"),  # Current installation path
        Path("/opt/sm-node"),  # Legacy path
        Path("/usr/local/node"),  # Legacy installation path
        Path.cwd(),
        Path(__file__).parent.parent / "node",
    ]
    
    for node_dir in possible_roots:
        compose_file = node_dir / "docker-compose.yml"
        if compose_file.exists():
            return compose_file
    
    return Path("/opt/smite-node") / "docker-compose.yml"


def get_env_file():
    """Get .env file path"""
    possible_roots = [
        Path("/opt/smite-node"),  # Current installation path
        Path("/opt/sm-node"),  # Legacy path
        Path("/usr/local/node"),  # Legacy installation path
        Path.cwd(),
        Path(__file__).parent.parent / "node",
    ]
    
    for node_dir in possible_roots:
        env_file = node_dir / ".env"
        if env_file.exists():
            return env_file
    
    return Path("/opt/smite-node") / ".env"


def run_docker_compose(args, capture_output=False, env_vars=None):
    """Run docker compose command"""
    compose_file = get_compose_file()
    if not compose_file.exists():
        print(f"Error: docker-compose.yml not found at {compose_file}")
        print(f"\nPlease ensure you're in the node directory or docker-compose.yml exists at:")
        print(f"  - /opt/smite-node/docker-compose.yml")
        print(f"  - /opt/sm-node/docker-compose.yml")
        print(f"  - /usr/local/node/docker-compose.yml")
        print(f"  - {Path.cwd()}/docker-compose.yml")
        sys.exit(1)
    
    # Change to the directory containing docker-compose.yml so relative paths work
    compose_dir = compose_file.parent
    original_cwd = Path.cwd()
    
    try:
        os.chdir(compose_dir)
        cmd = ["docker", "compose", "-f", str(compose_file)] + args
        # Merge environment variables if provided
        process_env = os.environ.copy()
        if env_vars:
            process_env.update(env_vars)
        result = subprocess.run(cmd, capture_output=capture_output, text=True, cwd=str(compose_dir), env=process_env)
        if not capture_output and result.returncode != 0:
            sys.exit(result.returncode)
        return result
    finally:
        os.chdir(original_cwd)


def cmd_status(args):
    """Show node status"""
    print("Node Status:")
    print("-" * 50)
    
    result = subprocess.run(["docker", "ps", "--filter", "name=sm-node", "--format", "{{.Status}}"], 
                          capture_output=True, text=True)
    if result.stdout.strip():
        print(f"Docker: {result.stdout.strip()}")
    else:
        print("Docker: Not running")
    
    try:
        try:
            import requests
        except ImportError:
            print("API: requests library not installed")
            return
            
        env_file = get_env_file()
        port = 8888
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("NODE_API_PORT="):
                    port = int(line.split("=")[1])
        
        response = requests.get(f"http://localhost:{port}/api/agent/status", timeout=2)
        if response.status_code == 200:
            data = response.json()
            print(f"API: Running")
            print(f"Active Tunnels: {data.get('active_tunnels', 0)}")
        else:
            print("API: Not responding")
    except Exception as e:
        print(f"API: Not accessible ({e})")


def cmd_update(args):
    """Update node (pull images and recreate)"""
    print("Updating node...")
    
    # Ensure SMITE_VERSION is set to 'main' in .env
    env_file = get_env_file()
    env_updated = False
    if env_file.exists():
        env_content = env_file.read_text()
        lines = env_content.split('\n')
        updated_lines = []
        smite_version_set = False
        for line in lines:
            if line.strip().startswith("SMITE_VERSION="):
                # Update to main if it's set to something else
                if "SMITE_VERSION=main" not in line:
                    updated_lines.append("SMITE_VERSION=main")
                    env_updated = True
                else:
                    updated_lines.append(line)
                smite_version_set = True
            else:
                updated_lines.append(line)
        if not smite_version_set:
            updated_lines.append("SMITE_VERSION=main")
            env_updated = True
        if env_updated:
            env_file.write_text('\n'.join(updated_lines))
            print("Setting SMITE_VERSION=main in .env file")
    else:
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("SMITE_VERSION=main\n")
        env_updated = True
        print("Created .env file with SMITE_VERSION=main")
    
    # Ensure environment variable is set for docker compose
    env_vars = os.environ.copy()
    env_vars["SMITE_VERSION"] = "main"
    
    run_docker_compose(["pull"], env_vars=env_vars)
    run_docker_compose(["up", "-d", "--force-recreate"], env_vars=env_vars)
    print("Node updated.")


def cmd_restart(args):
    """Restart node (recreate container to pick up .env changes, no pull)"""
    print("Restarting node...")
    run_docker_compose(["stop", "sm-node"])
    run_docker_compose(["rm", "-f", "sm-node"])
    result = run_docker_compose(["up", "-d", "--no-deps", "--no-pull", "sm-node"], capture_output=True)
    if result.returncode != 0 and "--no-pull" in result.stderr:
        run_docker_compose(["up", "-d", "--no-deps", "sm-node"])
    else:
        if result.returncode != 0:
            print(result.stderr)
            sys.exit(result.returncode)
    print("Node restarted. Tunnels will be restored by the panel.")


def cmd_edit(args):
    """Edit docker-compose.yml"""
    compose_file = get_compose_file()
    editor = os.environ.get("EDITOR", "nano")
    subprocess.run([editor, str(compose_file)])


def cmd_edit_env(args):
    """Edit .env file"""
    env_file = get_env_file()
    if not env_file.exists():
        print(f".env file not found. Creating...")
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("")
    
    editor = os.environ.get("EDITOR", "nano")
    subprocess.run([editor, str(env_file)])


def cmd_logs(args):
    """Stream logs"""
    follow = ["--follow"] if args.follow else []
    run_docker_compose(["logs"] + follow + ["sm-node"])


def cmd_uninstall(args):
    """Uninstall Smite Node - removes everything"""
    print("=" * 60)
    print("⚠️  WARNING: This will completely remove Smite Node!")
    print("=" * 60)
    print("\nThis will remove:")
    print("  - All Docker containers (sm-node)")
    print("  - All Docker volumes")
    print("  - Installation directory (/opt/smite-node)")
    print("  - CLI script (/usr/local/bin/sm-node)")
    print("  - Docker images (ghcr.io/zzedix/sm-node)")
    print("\n⚠️  ALL DATA WILL BE LOST!")
    print("=" * 60)
    
    response = input("\nAre you sure you want to continue? Type 'yes' to confirm: ")
    if response.lower() != 'yes':
        print("Uninstall cancelled.")
        sys.exit(0)
    
    print("\nStarting uninstall...")
    
    # Stop and remove containers
    print("\n[1/5] Stopping and removing containers...")
    try:
        compose_file = get_compose_file()
        if compose_file.exists():
            compose_dir = compose_file.parent
            original_cwd = Path.cwd()
            try:
                os.chdir(compose_dir)
                subprocess.run(["docker", "compose", "-f", str(compose_file), "down", "-v"], 
                             capture_output=True, check=False)
            finally:
                os.chdir(original_cwd)
        
        subprocess.run(["docker", "stop", "sm-node"], capture_output=True, check=False)
        subprocess.run(["docker", "rm", "-f", "sm-node"], capture_output=True, check=False)
        print("  ✓ Containers removed")
    except Exception as e:
        print(f"  ⚠️  Warning: {e}")
    
    # Remove volumes
    print("\n[2/5] Removing Docker volumes...")
    try:
        result = subprocess.run(["docker", "volume", "ls", "-q", "--filter", "name=sm-node"], 
                              capture_output=True, text=True)
        volumes = result.stdout.strip().split('\n')
        for volume in volumes:
            if volume:
                subprocess.run(["docker", "volume", "rm", "-f", volume], capture_output=True, check=False)
        print("  ✓ Volumes removed")
    except Exception as e:
        print(f"  ⚠️  Warning: {e}")
    
    # Remove images
    print("\n[3/5] Removing Docker images...")
    try:
        subprocess.run(["docker", "rmi", "-f", "ghcr.io/zzedix/sm-node"], capture_output=True, check=False)
        subprocess.run(["docker", "rmi", "-f", "ghcr.io/zzedix/sm-node:latest"], capture_output=True, check=False)
        result = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", "ghcr.io/zzedix/sm-node"], 
                              capture_output=True, text=True)
        for tag in result.stdout.strip().split('\n'):
            if tag:
                subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, check=False)
        print("  ✓ Images removed")
    except Exception as e:
        print(f"  ⚠️  Warning: {e}")
    
    # Remove installation directory
    print("\n[4/5] Removing installation directory...")
    install_dirs = [Path("/opt/smite-node"), Path("/opt/sm-node"), Path("/usr/local/node")]
    for install_dir in install_dirs:
        if install_dir.exists():
            try:
                shutil.rmtree(install_dir)
                print(f"  ✓ Removed {install_dir}")
            except Exception as e:
                print(f"  ⚠️  Warning: Could not remove {install_dir}: {e}")
        else:
            print(f"  - {install_dir} does not exist")
    
    # Remove CLI script
    print("\n[5/5] Removing CLI script...")
    cli_path = Path("/usr/local/bin/sm-node")
    if cli_path.exists():
        try:
            cli_path.unlink()
            print("  ✓ Removed /usr/local/bin/sm-node")
        except Exception as e:
            print(f"  ⚠️  Warning: Could not remove CLI script: {e}")
    else:
        print("  - CLI script not found")
    
    print("\n" + "=" * 60)
    print("✅ Smite Node has been completely uninstalled!")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Smite Node CLI")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    
    subparsers.add_parser("status", help="Show node status")
    
    subparsers.add_parser("update", help="Update node (pull images and recreate)")
    
    subparsers.add_parser("restart", help="Restart node (recreate to pick up .env changes)")
    
    subparsers.add_parser("edit", help="Edit docker-compose.yml")
    
    subparsers.add_parser("edit-env", help="Edit .env file")
    
    logs_parser = subparsers.add_parser("logs", help="View logs")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow logs")
    
    subparsers.add_parser("uninstall", help="Completely remove Smite Node")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    if args.command == "status":
        cmd_status(args)
    elif args.command == "update":
        cmd_update(args)
    elif args.command == "restart":
        cmd_restart(args)
    elif args.command == "edit":
        cmd_edit(args)
    elif args.command == "edit-env":
        cmd_edit_env(args)
    elif args.command == "logs":
        cmd_logs(args)
    elif args.command == "uninstall":
        cmd_uninstall(args)


if __name__ == "__main__":
    main()

