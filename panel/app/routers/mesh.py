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
    frp_endpoints = {}
    
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
        logger.info(f"Node {node_id} ({node.name}) has role: {node_role}")
        if node_role == "iran":
            iran_nodes.append((node_id, node, node_config))
            logger.info(f"Added node {node_id} to Iran nodes list")
        else:
            foreign_nodes.append((node_id, node, node_config))
            logger.info(f"Added node {node_id} to Foreign nodes list")
    
    if not iran_nodes:
        raise HTTPException(
            status_code=400,
            detail="At least one Iran node is required for mesh. Iran nodes run FRP servers."
        )
    
    if len(iran_nodes) > 1:
        logger.warning(f"Multiple Iran nodes found ({len(iran_nodes)}). Using first one as primary FRP server.")
    
    # Use first Iran node as the FRP server hub
    primary_iran_id, primary_iran_node, _ = iran_nodes[0]
    
    # Create ONLY 2 tunnels total (like manual tunnel creation):
    # - 1 TCP tunnel (Iran server + Foreign client) - ONE tunnel record
    # - 1 UDP tunnel (Iran server + Foreign client) - ONE tunnel record
    if transport == "both":
        transports_to_create = ["tcp", "udp"]  # Only 2 tunnels total
    else:
        transports_to_create = [transport]  # 1 tunnel
    
    # Get first Foreign node (like manual tunnel creation)
    if not foreign_nodes:
        raise HTTPException(
            status_code=400,
            detail="At least one Foreign node is required for mesh"
        )
    foreign_node_id, foreign_node, _ = foreign_nodes[0]
    
    # Clean up old mesh tunnels
    old_tunnels = await db.execute(
        select(Tunnel).where(
            Tunnel.name.like(f"wg-mesh-{mesh_id[:8]}%")
        )
    )
    for old_tunnel in old_tunnels.scalars().all():
        logger.info(f"Deleting old tunnel {old_tunnel.id}")
        try:
            if old_tunnel.node_id:
                await node_client.send_to_node(
                    node_id=old_tunnel.node_id,
                    endpoint="/api/agent/tunnels/remove",
                    data={"tunnel_id": old_tunnel.id}
                )
        except Exception as e:
            logger.warning(f"Error removing old tunnel: {e}")
        await db.delete(old_tunnel)
    await db.commit()
    
    # Create tunnels like manual creation - ONE tunnel record per transport with both server and client
    iran_frp_endpoints = {}
    for trans in transports_to_create:
        logger.info(f"Creating FRP {trans} tunnel (Iran server + Foreign client) for mesh {mesh_id}")
        
        # Generate ports
        import hashlib
        port_hash = int(hashlib.md5(f"{mesh_id}-{primary_iran_id}-{trans}".encode()).hexdigest()[:8], 16)
        bind_port = 7000 + (port_hash % 1000)
        wg_port = 17000 + (port_hash % 1000)
        
        iran_node_ip = primary_iran_node.node_metadata.get("ip_address")
        if not iran_node_ip:
            logger.warning(f"Iran node {primary_iran_id} has no IP address")
            continue
        
        # Create ONE tunnel record (like manual tunnel creation)
        # The tunnel creation logic will handle both server (Iran) and client (Foreign)
        tunnel_name = f"wg-mesh-{mesh_id[:8]}-{trans}"
        
        # Use the same logic as manual tunnel creation
        tunnel = Tunnel(
            name=tunnel_name,
            core="frp",
            type=trans,
            node_id=primary_iran_id,  # Iran node
            spec={
                "bind_port": bind_port,
                "remote_port": wg_port,
                "local_port": wg_port,
                "local_ip": "127.0.0.1",
            },
            status="pending"
        )
        db.add(tunnel)
        await db.commit()
        await db.refresh(tunnel)
        
        # Use the tunnel creation logic to apply both server and client
        try:
            from app.routers.tunnels import create_tunnel
            from app.schemas.tunnel import TunnelCreate
            
            # This will create both server on Iran and client on Foreign automatically
            # Just like manual tunnel creation does
            tunnel_create = TunnelCreate(
                name=tunnel_name,
                core="frp",
                type=trans,
                node_id=primary_iran_id,
                iran_node_id=primary_iran_id,
                foreign_node_id=foreign_node_id,
                spec={
                    "bind_port": bind_port,
                    "remote_port": wg_port,
                    "local_port": wg_port,
                    "local_ip": "127.0.0.1",
                }
            )
            
            # The create_tunnel function will handle both server and client
            # But we already created the tunnel, so we need to use the apply logic
            # Let's use the existing tunnel apply endpoint logic instead
            
            # Get the tunnel creation logic from tunnels.py
            # It will automatically create server on Iran and client on Foreign
            from app.routers.tunnels import prepare_frp_spec_for_node
            
            # Prepare specs like manual creation does
            server_spec = {"bind_port": bind_port}
            client_spec = {
                "server_addr": iran_node_ip,
                "server_port": bind_port,
                "type": trans,
                "local_ip": "127.0.0.1",
                "local_port": wg_port,
                "remote_port": wg_port,
            }
            
            # Apply server on Iran
            server_spec_prepared = await prepare_frp_spec_for_node(server_spec, primary_iran_node, request)
            response = await node_client.send_to_node(
                node_id=primary_iran_id,
                endpoint="/api/agent/tunnels/apply",
                data={
                    "tunnel_id": tunnel.id,
                    "core": "frp",
                    "type": trans,
                    "spec": {"mode": "server", **server_spec_prepared}
                }
            )
            
            # Apply client on Foreign
            client_spec_prepared = await prepare_frp_spec_for_node(client_spec, foreign_node, request)
            response = await node_client.send_to_node(
                node_id=foreign_node_id,
                endpoint="/api/agent/tunnels/apply",
                data={
                    "tunnel_id": tunnel.id,
                    "core": "frp",
                    "type": trans,
                    "spec": {"mode": "client", **client_spec_prepared}
                }
            )
            
            tunnel.status = "active"
            await db.commit()
            
            frp_endpoint = f"{iran_node_ip}:{wg_port}"
            iran_frp_endpoints[trans] = frp_endpoint
            logger.info(f"Created FRP {trans} tunnel {tunnel.id}: endpoint={frp_endpoint}")
        except Exception as e:
            logger.error(f"Failed to apply FRP {trans} tunnel: {e}", exc_info=True)
            tunnel.status = "error"
            await db.commit()
            continue
    
    if not iran_frp_endpoints:
        raise HTTPException(
            status_code=500,
            detail="Failed to create FRP tunnels"
        )
    
    # All nodes use Iran's FRP endpoints for WireGuard
    for node_id in mesh_configs.keys():
        frp_endpoints[node_id] = iran_frp_endpoints
    
    for node_id, node_config in mesh_configs.items():
        if node_id not in frp_endpoints:
            logger.warning(f"No FRP endpoint for node {node_id}, skipping WireGuard config")
            continue
        
        peer_endpoints = {}
        for peer_id, peer_eps in frp_endpoints.items():
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


async def _ensure_frp_server(
    mesh_id: str,
    node_id: str,
    node: Node,
    db: AsyncSession,
    request: Request,
    node_client: NodeClient,
    transport: str = "udp"
) -> Optional[str]:
    """Ensure FRP server exists on Iran node, return endpoint"""
    import hashlib
    
    if transport not in ["tcp", "udp"]:
        transport = "udp"
    
    tunnel_name = f"wg-mesh-{mesh_id[:8]}-{transport}-{node_id[:8]}"
    
    existing_result = await db.execute(
        select(Tunnel).where(
            Tunnel.name == tunnel_name,
            Tunnel.core == "frp",
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
        logger.info(f"Deleting existing FRP server tunnel {existing_tunnel.id} for node {node_id}")
        try:
            await node_client.send_to_node(
                node_id=node_id,
                endpoint="/api/agent/tunnels/remove",
                data={"tunnel_id": existing_tunnel.id}
            )
        except Exception as e:
            logger.warning(f"Error removing old tunnel from node {node_id}: {e}")
        await db.delete(existing_tunnel)
        await db.commit()
    
    # Generate unique bind_port for FRP server
    port_hash = int(hashlib.md5(f"{mesh_id}-{node_id}-{transport}".encode()).hexdigest()[:8], 16)
    bind_port = 7000 + (port_hash % 1000)
    
    # WireGuard port - remote_port and local_port must be the same for FRP tunnel to work
    # This is where WireGuard will listen and where clients will connect
    wg_port = 17000 + (port_hash % 1000)
    
    spec = {
        "mode": "server",
        "bind_port": bind_port,
        "remote_port": wg_port,  # Store for WireGuard config
        "local_port": wg_port,   # Same as remote_port (required for FRP)
    }
    
    logger.info(f"Creating FRP server on Iran node: bind_port={bind_port}, transport={transport}, wg_port={wg_port} (remote_port=local_port={wg_port})")
    
    tunnel = Tunnel(
        name=tunnel_name,
        core="frp",
        type=transport,
        node_id=node_id,
        spec=spec,
        status="active"
    )
    db.add(tunnel)
    await db.commit()
    await db.refresh(tunnel)
    
    try:
        logger.info(f"Applying FRP server tunnel {tunnel.id} to node {node_id} ({node_ip})")
        response = await node_client.send_to_node(
            node_id=node_id,
            endpoint="/api/agent/tunnels/apply",
            data={
                "tunnel_id": tunnel.id,
                "core": "frp",
                "type": transport,
                "spec": spec
            }
        )
        if response.get("status") == "error":
            logger.error(f"Failed to create FRP server tunnel {tunnel.id} on node {node_id}: {response.get('message')}")
            return None
        logger.info(f"Successfully applied FRP server tunnel {tunnel.id} to node {node_id}")
    except Exception as e:
        logger.error(f"Error creating FRP server tunnel {tunnel.id} on node {node_id}: {e}", exc_info=True)
        return None
    
    # Return endpoint: node_ip:wg_port (where WireGuard will connect)
    # remote_port and local_port are the same (wg_port) for FRP tunnel to work
    return f"{node_ip}:{wg_port}"


async def _ensure_frp_client(
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
    """Ensure FRP client exists on Foreign node connecting to Iran server"""
    import hashlib
    
    if transport not in ["tcp", "udp"]:
        transport = "udp"
    
    tunnel_name = f"wg-mesh-{mesh_id[:8]}-{transport}-{foreign_node_id[:8]}"
    
    existing_result = await db.execute(
        select(Tunnel).where(
            Tunnel.name == tunnel_name,
            Tunnel.core == "frp",
            Tunnel.type == transport,
            Tunnel.node_id == foreign_node_id
        )
    )
    existing_tunnel = existing_result.scalar_one_or_none()
    
    if existing_tunnel:
        logger.info(f"Deleting existing FRP client tunnel {existing_tunnel.id} for Foreign node {foreign_node_id}")
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
    
    # Parse Iran endpoint to get IP and wg_port (remote_port = local_port for FRP to work)
    if ":" in iran_endpoint:
        iran_ip, wg_port_str = iran_endpoint.rsplit(":", 1)
        try:
            wg_port = int(wg_port_str)
        except ValueError:
            logger.error(f"Invalid wg_port in Iran endpoint: {wg_port_str}")
            return
    else:
        logger.error(f"Invalid Iran endpoint format: {iran_endpoint}")
        return
    
    # Calculate server_port the same way as in _ensure_frp_server
    iran_port_hash = int(hashlib.md5(f"{mesh_id}-{iran_node.id}-{transport}".encode()).hexdigest()[:8], 16)
    server_port = 7000 + (iran_port_hash % 1000)
    
    # For FRP tunnel to work: remote_port and local_port must be the same
    # This is where WireGuard will listen on Iran and where Foreign's WireGuard will connect
    spec = {
        "mode": "client",
        "transport": transport,
        "server_addr": iran_ip,
        "server_port": server_port,
        "type": transport,
        "local_ip": "127.0.0.1",
        "local_port": wg_port,   # Same as remote_port (required for FRP)
        "remote_port": wg_port,  # Same as local_port (required for FRP)
    }
    
    logger.info(f"Creating FRP client on Foreign node {foreign_node_id} connecting to Iran {iran_ip}:{server_port}, remote_port=local_port={wg_port}")
    
    tunnel = Tunnel(
        name=tunnel_name,
        core="frp",
        type=transport,
        node_id=foreign_node_id,
        spec=spec,
        status="active"
    )
    db.add(tunnel)
    await db.commit()
    await db.refresh(tunnel)
    
    try:
        logger.info(f"Applying FRP client tunnel {tunnel.id} to Foreign node {foreign_node_id}")
        response = await node_client.send_to_node(
            node_id=foreign_node_id,
            endpoint="/api/agent/tunnels/apply",
            data={
                "tunnel_id": tunnel.id,
                "core": "frp",
                "type": transport,
                "spec": spec
            }
        )
        if response.get("status") == "error":
            logger.error(f"Failed to create FRP client tunnel {tunnel.id} on Foreign node {foreign_node_id}: {response.get('message')}")
        else:
            logger.info(f"Successfully applied FRP client tunnel {tunnel.id} to Foreign node {foreign_node_id}")
    except Exception as e:
        logger.error(f"Error creating FRP client tunnel {tunnel.id} on Foreign node {foreign_node_id}: {e}", exc_info=True)


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
    
    # For WireGuard mesh, Backhaul server only needs to listen on control_port
    # No port forwarding needed - WireGuard connects directly to control_port
    spec = {
        "mode": "server",
        "transport": transport,
        "bind_addr": f"0.0.0.0:{control_port}",
        "control_port": control_port,
    }
    
    logger.info(f"Creating Backhaul server on Iran node: control_port={control_port}, transport={transport}")
    
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

