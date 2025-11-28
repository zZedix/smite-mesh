"""Status API endpoints"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
import psutil

from app.database import get_db
from app.models import Tunnel, Node


router = APIRouter()

VERSION = "0.1.0"


@router.get("/version")
async def get_version():
    """Get panel version from git tag, VERSION file, Docker image label, or environment"""
    import os
    import subprocess
    from pathlib import Path
    
    # Try to get version from git tag (works in both Docker and local)
    try:
        # Try git describe to get the latest tag
        result = subprocess.run(
            ["git", "describe", "--tags", "--always", "--dirty"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd="/app"
        )
        if result.returncode == 0:
            git_version = result.stdout.strip()
            # Clean up git describe output (e.g., "v0.1.0-5-gabc123" -> "v0.1.0")
            # Or if it's just a tag, use it directly
            if git_version and not git_version.startswith("fatal"):
                # Remove commit hash and dirty marker if present
                version = git_version.split("-")[0].lstrip("v")
                if version and version not in ["next", "latest", "main", "master"]:
                    return {"version": version}
    except:
        pass
    
    # Try VERSION file (created during Docker build)
    version_file = Path("/app/VERSION")
    if version_file.exists():
        try:
            version = version_file.read_text().strip()
            if version and version not in ["next", "latest"]:
                return {"version": version.lstrip("v")}
        except:
            pass
    
    # Try Docker image labels
    smite_version = os.getenv("SMITE_VERSION", "")
    if smite_version in ["next", "latest"]:
        try:
            import json
            cgroup_path = Path("/proc/self/cgroup")
            if cgroup_path.exists():
                with open(cgroup_path) as f:
                    for line in f:
                        if "docker" in line or "containerd" in line:
                            container_id = line.split("/")[-1].strip()
                            result = subprocess.run(
                                ["docker", "inspect", container_id],
                                capture_output=True,
                                text=True,
                                timeout=2
                            )
                            if result.returncode == 0:
                                data = json.loads(result.stdout)
                                if data and len(data) > 0:
                                    labels = data[0].get("Config", {}).get("Labels", {})
                                    version = labels.get("smite.version") or labels.get("org.opencontainers.image.version", "")
                                    if version and version not in ["next", "latest"]:
                                        return {"version": version.lstrip("v")}
                            break
        except:
            pass
        
        return {"version": smite_version}
    
    if smite_version:
        version = smite_version.lstrip("v")
    else:
        version = VERSION
    
    return {"version": version}


@router.get("")
async def get_status(db: AsyncSession = Depends(get_db)):
    """Get system status"""
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    
    tunnel_result = await db.execute(select(func.count(Tunnel.id)))
    total_tunnels = tunnel_result.scalar() or 0
    
    active_tunnels_result = await db.execute(
        select(func.count(Tunnel.id)).where(Tunnel.status == "active")
    )
    active_tunnels = active_tunnels_result.scalar() or 0
    
    node_result = await db.execute(select(func.count(Node.id)))
    total_nodes = node_result.scalar() or 0
    
    active_nodes_result = await db.execute(
        select(func.count(Node.id)).where(Node.status == "active")
    )
    active_nodes = active_nodes_result.scalar() or 0
    
    return {
        "system": {
            "cpu_percent": cpu_percent,
            "memory_percent": memory.percent,
            "memory_total_gb": memory.total / (1024**3),
            "memory_used_gb": memory.used / (1024**3),
        },
        "tunnels": {
            "total": total_tunnels,
            "active": active_tunnels,
        },
        "nodes": {
            "total": total_nodes,
            "active": active_nodes,
        }
    }

