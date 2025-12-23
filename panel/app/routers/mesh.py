"""WireGuard Mesh API endpoints"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging
from datetime import datetime

from app.database import get_db
from app.models import WireGuardMesh, Node, Tunnel
from app.wireguard_mesh_manager import wireguard_mesh_manager
from app.node_client import NodeClient
from app.ipam_manager import ipam_manager

router = APIRouter()
logger = logging.getLogger(__name__)


class MeshCreate(BaseModel):
    name: str
    node_ids: List[str]
    lan_subnets: Dict[str, str]
    overlay_subnet: Optional[str] = None
    topology: str = "full-mesh"
    mtu: int = 1280
    transport: str = "both"


class MeshResponse(BaseModel):
    id: str
    name: str
    topology: str
    overlay_subnet: str
    mtu: int
    status: str
    created_at: datetime
    updated_at: datetime
    mesh_config: Dict[str, Any]


@router.post("/create", response_model=MeshResponse)
async def create_mesh(
    mesh: MeshCreate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Create a new WireGuard mesh"""
    if mesh.topology not in ["full-mesh", "hub-spoke"]:
        raise HTTPException(status_code=400, detail="Topology must be 'full-mesh' or 'hub-spoke'")
    
    if mesh.transport not in ["tcp", "udp", "both"]:
        raise HTTPException(status_code=400, detail="Transport must be 'tcp', 'udp', or 'both'")
    
    if len(mesh.node_ids) < 2:
        raise HTTPException(status_code=400, detail="At least 2 nodes required for mesh")
    
    nodes_result = await db.execute(
        select(Node).where(Node.id.in_(mesh.node_ids))
    )
    nodes = nodes_result.scalars().all()
    
    if len(nodes) != len(mesh.node_ids):
        raise HTTPException(status_code=404, detail="One or more nodes not found")
    
    pool = await ipam_manager.get_pool(db)
    if not pool:
        raise HTTPException(
            status_code=400,
            detail="No overlay IP pool configured. Please create an overlay pool first."
        )
    
    overlay_subnet = mesh.overlay_subnet or pool.cidr
    
    if overlay_subnet != pool.cidr:
        raise HTTPException(
            status_code=400,
            detail=f"Overlay subnet must match IPAM pool CIDR: {pool.cidr}"
        )
    
    node_configs = []
    node_ipam_ips = {}
    
    for node in nodes:
        node_id = node.id
        lan_subnet = mesh.lan_subnets.get(node_id, "")
        
        overlay_ip = await ipam_manager.get_node_ip(db, node_id)
        if not overlay_ip:
            overlay_ip = await ipam_manager.allocate_ip(db, node_id)
            if not overlay_ip:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to allocate overlay IP for node {node.name}. Pool may be exhausted."
                )
        
        node_ipam_ips[node_id] = overlay_ip
        node_configs.append({
            "node_id": node_id,
            "name": node.name,
            "lan_subnet": lan_subnet,
            "overlay_ip": overlay_ip
        })
    
    try:
        mesh_configs = wireguard_mesh_manager.create_mesh_config(
            mesh_id="",  # Will be set after DB insert
            nodes=node_configs,
            overlay_subnet=overlay_subnet,
            topology=mesh.topology,
            mtu=mesh.mtu
        )
    except Exception as e:
        logger.error(f"Failed to create mesh config: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to create mesh config: {str(e)}")
    
    mesh_config_data = {
        "transport": mesh.transport,
        "nodes": mesh_configs
    }
    
    db_mesh = WireGuardMesh(
        name=mesh.name,
        topology=mesh.topology,
        overlay_subnet=overlay_subnet,
        mtu=mesh.mtu,
        status="pending",
        mesh_config=mesh_config_data
    )
    db.add(db_mesh)
    await db.commit()
    await db.refresh(db_mesh)
    
    mesh_configs = wireguard_mesh_manager.create_mesh_config(
        mesh_id=db_mesh.id,
        nodes=node_configs,
        overlay_subnet=overlay_subnet,
        topology=mesh.topology,
        mtu=mesh.mtu
    )
    mesh_config_data = {
        "transport": mesh.transport,
        "nodes": mesh_configs
    }
    db_mesh.mesh_config = mesh_config_data
    await db.commit()
    await db.refresh(db_mesh)
    
    return db_mesh


@router.post("/{mesh_id}/apply")
async def apply_mesh(
    mesh_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Apply mesh configuration to all nodes"""
    result = await db.execute(
        select(WireGuardMesh).where(WireGuardMesh.id == mesh_id)
    )
    mesh = result.scalar_one_or_none()
    
    if not mesh:
        raise HTTPException(status_code=404, detail="Mesh not found")
    
    mesh_config_data = mesh.mesh_config
    if not mesh_config_data:
        raise HTTPException(status_code=400, detail="Mesh configuration not found")
    
    transport = mesh_config_data.get("transport", "udp")
    mesh_configs = mesh_config_data.get("nodes", {})
    
    if not mesh_configs:
        raise HTTPException(status_code=400, detail="Mesh node configuration not found")
    
    node_client = NodeClient()
    backhaul_endpoints = {}
    
    transports_to_create = ["tcp", "udp"] if transport == "both" else [transport]
    
    for node_id, node_config in mesh_configs.items():
        node_result = await db.execute(
            select(Node).where(Node.id == node_id)
        )
        node = node_result.scalar_one_or_none()
        if not node:
            logger.warning(f"Node {node_id} not found, skipping")
            continue
        
        node_endpoints = {}
        for trans in transports_to_create:
            backhaul_endpoint = await _ensure_backhaul_tunnel(
                mesh_id, node_id, node, node_config, db, request, node_client, trans
            )
            if backhaul_endpoint:
                node_endpoints[trans] = backhaul_endpoint
        
        if node_endpoints:
            backhaul_endpoints[node_id] = node_endpoints
    
    for node_id, node_config in mesh_configs.items():
        if node_id not in backhaul_endpoints:
            logger.warning(f"No backhaul endpoint for node {node_id}, skipping WireGuard config")
            continue
        
        peer_endpoints = {}
        for peer_id, peer_eps in backhaul_endpoints.items():
            if peer_id != node_id:
                peer_endpoints[peer_id] = peer_eps
        
        wg_config = wireguard_mesh_manager.generate_wireguard_config(
            node_config,
            peer_endpoints
        )
        
        routes = wireguard_mesh_manager.get_peer_routes(node_config)
        
        overlay_ip = await ipam_manager.get_node_ip(db, node_id)
        if not overlay_ip:
            logger.warning(f"Node {node_id} has no IPAM overlay IP, mesh may not work correctly")
        
        spec = {
            "config": wg_config,
            "routes": routes,
            "overlay_ip": overlay_ip
        }
        
        try:
            response = await node_client.send_to_node(
                node_id=node_id,
                endpoint="/api/agent/mesh/apply",
                data={
                    "mesh_id": mesh_id,
                    "spec": spec
                }
            )
            if response.get("status") == "error":
                logger.error(f"Failed to apply mesh to node {node_id}: {response.get('message')}")
        except Exception as e:
            logger.error(f"Error applying mesh to node {node_id}: {e}", exc_info=True)
    
    mesh.status = "active"
    await db.commit()
    
    return {"status": "success", "message": "Mesh applied to all nodes"}


async def _ensure_backhaul_tunnel(
    mesh_id: str,
    node_id: str,
    node: Node,
    node_config: Dict[str, Any],
    db: AsyncSession,
    request: Request,
    node_client: NodeClient,
    transport: str = "udp"
) -> Optional[str]:
    """Ensure Backhaul server exists for node, return endpoint"""
    import hashlib
    
    if transport not in ["tcp", "udp"]:
        transport = "udp"
    
    tunnel_name = f"wg-mesh-{mesh_id[:8]}-{transport}-{node_id[:8]}"
    
    existing_result = await db.execute(
        select(Tunnel).where(
            Tunnel.name == tunnel_name,
            Tunnel.core == "backhaul",
            Tunnel.type == transport,
            Tunnel.node_id == node_id
        )
    )
    existing_tunnel = existing_result.scalar_one_or_none()
    
    node_ip = node.node_metadata.get("ip_address")
    if not node_ip:
        logger.warning(f"Node {node_id} has no IP address")
        return None
    
    if existing_tunnel:
        control_port = existing_tunnel.spec.get("control_port") or existing_tunnel.spec.get("listen_port")
        if control_port:
            return f"{node_ip}:{control_port}"
    
    port_hash = int(hashlib.md5(f"{mesh_id}-{node_id}-{transport}".encode()).hexdigest()[:8], 16)
    base_port = 3080 if transport == "udp" else 4080
    control_port = base_port + (port_hash % 1000)
    
    spec = {
        "mode": "server",
        "transport": transport,
        "bind_addr": f"0.0.0.0:{control_port}",
        "control_port": control_port,
        "listen_port": control_port,
        "ports": [str(control_port)]
    }
    
    tunnel = Tunnel(
        name=tunnel_name,
        core="backhaul",
        type=transport,
        node_id=node_id,
        spec=spec,
        status="active"
    )
    db.add(tunnel)
    await db.commit()
    await db.refresh(tunnel)
    
    try:
        response = await node_client.send_to_node(
            node_id=node_id,
            endpoint="/api/agent/tunnels/apply",
            data={
                "tunnel_id": tunnel.id,
                "core": "backhaul",
                "type": transport,
                "spec": spec
            }
        )
        if response.get("status") == "error":
            logger.error(f"Failed to create backhaul tunnel: {response.get('message')}")
            return None
    except Exception as e:
        logger.error(f"Error creating backhaul tunnel: {e}", exc_info=True)
        return None
    
    return f"{node_ip}:{control_port}"


@router.get("/{mesh_id}/status")
async def get_mesh_status(
    mesh_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Get mesh status from all nodes"""
    result = await db.execute(
        select(WireGuardMesh).where(WireGuardMesh.id == mesh_id)
    )
    mesh = result.scalar_one_or_none()
    
    if not mesh:
        raise HTTPException(status_code=404, detail="Mesh not found")
    
    mesh_config_data = mesh.mesh_config or {}
    mesh_configs = mesh_config_data.get("nodes", {}) if isinstance(mesh_config_data, dict) and "nodes" in mesh_config_data else mesh_config_data
    
    node_client = NodeClient()
    node_statuses = {}
    
    for node_id in mesh_configs.keys():
        try:
            response = await node_client.send_to_node(
                node_id=node_id,
                endpoint=f"/api/agent/mesh/{mesh_id}/status",
                method="GET"
            )
            node_data = response.get("data", {})
            
            # Get node info
            node_result = await db.execute(
                select(Node).where(Node.id == node_id)
            )
            node = node_result.scalar_one_or_none()
            
            # Get LAN subnet from mesh config
            node_config = mesh_configs.get(node_id, {})
            if isinstance(node_config, dict):
                lan_subnet = node_config.get("lan_subnet", "")
                if lan_subnet:
                    node_data["lan_subnet"] = lan_subnet
                node_data["node_name"] = node.name if node else node_id
            
            overlay_ip = await ipam_manager.get_node_ip(db, node_id)
            if overlay_ip:
                node_data["overlay_ip"] = overlay_ip
            
            node_statuses[node_id] = node_data
        except Exception as e:
            logger.error(f"Error getting status from node {node_id}: {e}")
            node_statuses[node_id] = {"error": str(e)}
    
    return {
        "mesh_id": mesh_id,
        "mesh_name": mesh.name,
        "status": mesh.status,
        "nodes": node_statuses
    }


@router.post("/{mesh_id}/rotate-keys")
async def rotate_mesh_keys(
    mesh_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Rotate WireGuard keys for mesh"""
    result = await db.execute(
        select(WireGuardMesh).where(WireGuardMesh.id == mesh_id)
    )
    mesh = result.scalar_one_or_none()
    
    if not mesh:
        raise HTTPException(status_code=404, detail="Mesh not found")
    
    old_config_data = mesh.mesh_config or {}
    old_transport = old_config_data.get("transport", "udp") if isinstance(old_config_data, dict) else "udp"
    old_configs = old_config_data.get("nodes", old_config_data) if isinstance(old_config_data, dict) and "nodes" in old_config_data else old_config_data
    
    node_configs = []
    for node_id, node_config in old_configs.items():
        if not isinstance(node_config, dict):
            continue
        
        node_result = await db.execute(
            select(Node).where(Node.id == node_id)
        )
        node = node_result.scalar_one_or_none()
        if not node:
            continue
        
        node_configs.append({
            "node_id": node_id,
            "name": node.name,
            "lan_subnet": node_config.get("lan_subnet", "")
        })
    
    try:
        new_configs = wireguard_mesh_manager.create_mesh_config(
            mesh_id=mesh_id,
            nodes=node_configs,
            overlay_subnet=mesh.overlay_subnet,
            topology=mesh.topology,
            mtu=mesh.mtu
        )
        mesh_config_data = {
            "transport": old_transport,
            "nodes": new_configs
        }
        mesh.mesh_config = mesh_config_data
        await db.commit()
        
        return {"status": "success", "message": "Keys rotated, re-apply mesh to update nodes"}
    except Exception as e:
        logger.error(f"Failed to rotate keys: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to rotate keys: {str(e)}")


@router.get("", response_model=List[MeshResponse])
async def list_meshes(db: AsyncSession = Depends(get_db)):
    """List all meshes"""
    result = await db.execute(select(WireGuardMesh))
    meshes = result.scalars().all()
    return meshes


@router.delete("/{mesh_id}")
async def delete_mesh(
    mesh_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Delete mesh and cleanup"""
    result = await db.execute(
        select(WireGuardMesh).where(WireGuardMesh.id == mesh_id)
    )
    mesh = result.scalar_one_or_none()
    
    if not mesh:
        raise HTTPException(status_code=404, detail="Mesh not found")
    
    mesh_config_data = mesh.mesh_config or {}
    mesh_configs = mesh_config_data.get("nodes", {}) if isinstance(mesh_config_data, dict) and "nodes" in mesh_config_data else mesh_config_data
    node_client = NodeClient()
    
    for node_id in mesh_configs.keys():
        try:
            await node_client.send_to_node(
                node_id=node_id,
                endpoint="/api/agent/mesh/remove",
                data={"mesh_id": mesh_id}
            )
        except Exception as e:
            logger.warning(f"Error removing mesh from node {node_id}: {e}")
    
    tunnel_result = await db.execute(
        select(Tunnel).where(
            (Tunnel.name.like(f"wg-mesh-{mesh_id[:8]}%")) |
            (Tunnel.name.like(f"wg-mesh-{mesh_id[:8]}-tcp-%")) |
            (Tunnel.name.like(f"wg-mesh-{mesh_id[:8]}-udp-%"))
        )
    )
    tunnels = tunnel_result.scalars().all()
    for tunnel in tunnels:
        await db.delete(tunnel)
    
    await db.delete(mesh)
    await db.commit()
    
    return {"status": "success", "message": "Mesh deleted"}

