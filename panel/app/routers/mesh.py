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
    
    logger.info(f"Applying mesh {mesh_id} with transport={transport}, nodes={list(mesh_configs.keys())}")
    
    node_client = NodeClient()
    backhaul_endpoints = {}
    
    # Separate Iran and Foreign nodes
    iran_nodes = []
    foreign_nodes = []
    
    for node_id, node_config in mesh_configs.items():
        node_result = await db.execute(
            select(Node).where(Node.id == node_id)
        )
        node = node_result.scalar_one_or_none()
        if not node:
            logger.warning(f"Node {node_id} not found, skipping")
            continue
        
        node_role = node.node_metadata.get("role", "iran")
        if node_role == "iran":
            iran_nodes.append((node_id, node, node_config))
        else:
            foreign_nodes.append((node_id, node, node_config))
    
    if not iran_nodes:
        raise HTTPException(
            status_code=400,
            detail="At least one Iran node is required for mesh. Iran nodes run Backhaul servers."
        )
    
    if len(iran_nodes) > 1:
        logger.warning(f"Multiple Iran nodes found ({len(iran_nodes)}). Using first one as primary Backhaul server.")
    
    # Use first Iran node as the Backhaul server hub
    primary_iran_id, primary_iran_node, _ = iran_nodes[0]
    
    transports_to_create = ["tcp", "udp"] if transport == "both" else [transport]
    
    # Create Backhaul servers on Iran nodes only
    iran_endpoints = {}
    for iran_id, iran_node, iran_config in iran_nodes:
        node_endpoints = {}
        for trans in transports_to_create:
            logger.info(f"Creating Backhaul {trans} server on Iran node {iran_id} in mesh {mesh_id}")
            backhaul_endpoint = await _ensure_backhaul_server(
                mesh_id, iran_id, iran_node, iran_config, db, request, node_client, trans
            )
            if backhaul_endpoint:
                logger.info(f"Backhaul {trans} server endpoint for Iran node {iran_id}: {backhaul_endpoint}")
                node_endpoints[trans] = backhaul_endpoint
            else:
                logger.warning(f"Failed to create Backhaul {trans} server on Iran node {iran_id}")
        
        if node_endpoints:
            iran_endpoints[iran_id] = node_endpoints
    
    if not iran_endpoints:
        raise HTTPException(
            status_code=500,
            detail="Failed to create Backhaul servers on Iran nodes"
        )
    
    # Use primary Iran node's endpoints for all nodes (including other Iran nodes and Foreign nodes)
    primary_iran_endpoints = iran_endpoints.get(primary_iran_id, {})
    if not primary_iran_endpoints:
        primary_iran_endpoints = list(iran_endpoints.values())[0]
    
    # Create Backhaul clients on Foreign nodes connecting to Iran
    for foreign_id, foreign_node, foreign_config in foreign_nodes:
        for trans in transports_to_create:
            logger.info(f"Creating Backhaul {trans} client on Foreign node {foreign_id} connecting to Iran {primary_iran_id}")
            iran_endpoint = primary_iran_endpoints.get(trans)
            if iran_endpoint:
                await _ensure_backhaul_client(
                    mesh_id, foreign_id, foreign_node, primary_iran_node, iran_endpoint, db, request, node_client, trans
                )
        
        # Foreign nodes use Iran's endpoints for WireGuard
        backhaul_endpoints[foreign_id] = primary_iran_endpoints
    
    # Primary Iran node uses its own endpoints (it's the server)
    backhaul_endpoints[primary_iran_id] = primary_iran_endpoints
    
    # Other Iran nodes also use primary Iran's endpoints
    for iran_id, _, _ in iran_nodes:
        if iran_id != primary_iran_id:
            backhaul_endpoints[iran_id] = primary_iran_endpoints
    
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


async def _ensure_backhaul_server(
    mesh_id: str,
    node_id: str,
    node: Node,
    node_config: Dict[str, Any],
    db: AsyncSession,
    request: Request,
    node_client: NodeClient,
    transport: str = "udp"
) -> Optional[str]:
    """Ensure Backhaul server exists on Iran node, return endpoint"""
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
        # Always delete and recreate Backhaul tunnels to ensure correct port configuration
        # This prevents issues with old tunnels that may have incorrect endpoints
        logger.info(f"Deleting existing Backhaul tunnel {existing_tunnel.id} for node {node_id} to ensure correct configuration")
        try:
            # Remove from node first
            await node_client.send_to_node(
                node_id=node_id,
                endpoint="/api/agent/tunnels/remove",
                data={"tunnel_id": existing_tunnel.id}
            )
        except Exception as e:
            logger.warning(f"Error removing old tunnel from node {node_id}: {e}")
        
        await db.delete(existing_tunnel)
        await db.commit()
        logger.info(f"Deleted old Backhaul tunnel {existing_tunnel.id}, will create new one")
    
    port_hash = int(hashlib.md5(f"{mesh_id}-{node_id}-{transport}".encode()).hexdigest()[:8], 16)
    base_port = 3080 if transport == "udp" else 4080
    control_port = base_port + (port_hash % 1000)
    # Public port must be DIFFERENT from control port
    # Target port should be the same as public port
    # Use a different base to ensure they're always in different ranges
    public_port = (base_port + 2000) + (port_hash % 1000)  # Different range from control_port
    
    # Ensure public_port is definitely different from control_port
    while public_port == control_port:
        public_port = control_port + 1000 + (port_hash % 100)
    
    # Final safety check
    assert public_port != control_port, f"Port conflict: control_port={control_port}, public_port={public_port}"
    
    spec = {
        "mode": "server",
        "transport": transport,
        "bind_addr": f"0.0.0.0:{control_port}",
        "control_port": control_port,
        "public_port": public_port,
        "target_host": "127.0.0.1",
        "target_port": public_port,  # Same as public port
        "ports": [f"{public_port}=127.0.0.1:{public_port}"]
    }
    
    logger.info(f"Creating Backhaul tunnel: control_port={control_port}, public_port={public_port}, target_port={public_port}")
    
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
        logger.info(f"Applying Backhaul tunnel {tunnel.id} to node {node_id} ({node_ip})")
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
            logger.error(f"Failed to create backhaul tunnel {tunnel.id} on node {node_id}: {response.get('message')}")
            return None
        logger.info(f"Successfully applied Backhaul tunnel {tunnel.id} to node {node_id}, endpoint: {node_ip}:{control_port}")
    except Exception as e:
        logger.error(f"Error creating backhaul tunnel {tunnel.id} on node {node_id}: {e}", exc_info=True)
        return None
    
    # WireGuard should connect to control_port (where Backhaul server listens), not public_port
    return f"{node_ip}:{control_port}"


async def _ensure_backhaul_client(
    mesh_id: str,
    foreign_node_id: str,
    foreign_node: Node,
    iran_node: Node,
    iran_endpoint: str,
    db: AsyncSession,
    request: Request,
    node_client: NodeClient,
    transport: str = "udp"
) -> None:
    """Ensure Backhaul client exists on Foreign node connecting to Iran server"""
    import hashlib
    
    if transport not in ["tcp", "udp"]:
        transport = "udp"
    
    tunnel_name = f"wg-mesh-{mesh_id[:8]}-{transport}-{foreign_node_id[:8]}"
    
    existing_result = await db.execute(
        select(Tunnel).where(
            Tunnel.name == tunnel_name,
            Tunnel.core == "backhaul",
            Tunnel.type == transport,
            Tunnel.node_id == foreign_node_id
        )
    )
    existing_tunnel = existing_result.scalar_one_or_none()
    
    if existing_tunnel:
        logger.info(f"Deleting existing Backhaul client tunnel {existing_tunnel.id} for Foreign node {foreign_node_id}")
        try:
            await node_client.send_to_node(
                node_id=foreign_node_id,
                endpoint="/api/agent/tunnels/remove",
                data={"tunnel_id": existing_tunnel.id}
            )
        except Exception as e:
            logger.warning(f"Error removing old tunnel from Foreign node {foreign_node_id}: {e}")
        
        await db.delete(existing_tunnel)
        await db.commit()
        logger.info(f"Deleted old Backhaul client tunnel {existing_tunnel.id}, will create new one")
    
    # Parse Iran endpoint to get IP and port
    if ":" in iran_endpoint:
        iran_ip, iran_port = iran_endpoint.rsplit(":", 1)
    else:
        logger.error(f"Invalid Iran endpoint format: {iran_endpoint}")
        return
    
    spec = {
        "mode": "client",
        "transport": transport,
        "remote_addr": iran_endpoint,
    }
    
    logger.info(f"Creating Backhaul client on Foreign node {foreign_node_id} connecting to Iran {iran_endpoint}")
    
    tunnel = Tunnel(
        name=tunnel_name,
        core="backhaul",
        type=transport,
        node_id=foreign_node_id,
        spec=spec,
        status="active"
    )
    db.add(tunnel)
    await db.commit()
    await db.refresh(tunnel)
    
    try:
        logger.info(f"Applying Backhaul client tunnel {tunnel.id} to Foreign node {foreign_node_id}")
        response = await node_client.send_to_node(
            node_id=foreign_node_id,
            endpoint="/api/agent/tunnels/apply",
            data={
                "tunnel_id": tunnel.id,
                "core": "backhaul",
                "type": transport,
                "spec": spec
            }
        )
        if response.get("status") == "error":
            logger.error(f"Failed to create Backhaul client tunnel {tunnel.id} on Foreign node {foreign_node_id}: {response.get('message')}")
        else:
            logger.info(f"Successfully applied Backhaul client tunnel {tunnel.id} to Foreign node {foreign_node_id}")
    except Exception as e:
        logger.error(f"Error creating Backhaul client tunnel {tunnel.id} on Foreign node {foreign_node_id}: {e}", exc_info=True)


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

