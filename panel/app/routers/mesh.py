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
    wireguard_port: Optional[int] = None  # Custom WireGuard port (local_port and remote_port will use this)


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
    
    # Validate wireguard_port if provided
    if mesh.wireguard_port is not None:
        if not (1 <= mesh.wireguard_port <= 65535):
            raise HTTPException(status_code=400, detail="wireguard_port must be between 1 and 65535")
    
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
        "wireguard_port": mesh.wireguard_port,  # Store custom WireGuard port
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
        "wireguard_port": mesh.wireguard_port,  # Store custom WireGuard port
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
    wireguard_port = mesh_config_data.get("wireguard_port")  # Get custom WireGuard port if set
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
    
    logger.info(f"Full mesh setup: {len(iran_nodes)} Iran node(s), {len(foreign_nodes)} Foreign node(s)")
    
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
    
    # Determine transports
    if transport == "both":
        transports_to_create = ["tcp", "udp"]
    else:
        transports_to_create = [transport]
    
    import hashlib
    from app.routers.tunnels import prepare_frp_spec_for_node
    
    # Step 1: Create FRP servers on ALL Iran nodes
    # Map: iran_node_id -> transport -> endpoint
    iran_node_endpoints = {}  # {iran_node_id: {transport: "ip:port", ...}, ...}
    
    # Generate shared WireGuard port (consistent across all Iran nodes for Foreign node compatibility)
    if wireguard_port is not None:
        shared_wg_port = wireguard_port
        logger.info(f"Using custom WireGuard port {shared_wg_port} for all Iran nodes")
    else:
        # Generate a single port based on mesh_id (not per-node) for consistency
        port_hash = int(hashlib.md5(f"{mesh_id}-wg-port".encode()).hexdigest()[:8], 16)
        shared_wg_port = 17000 + (port_hash % 1000)
        logger.info(f"Using generated shared WireGuard port {shared_wg_port} for all Iran nodes")
    
    for iran_node_id, iran_node, _ in iran_nodes:
        iran_node_ip = iran_node.node_metadata.get("ip_address")
        if not iran_node_ip:
            logger.warning(f"Iran node {iran_node_id} has no IP address, skipping")
            continue
        
        iran_node_endpoints[iran_node_id] = {}
        
        for trans in transports_to_create:
            # Generate unique bind_port for each Iran node (FRP server port)
            port_hash = int(hashlib.md5(f"{mesh_id}-{iran_node_id}-{trans}".encode()).hexdigest()[:8], 16)
            bind_port = 7000 + (port_hash % 1000)  # FRP bind_port remains random
            
            # Use shared WireGuard port for all Iran nodes
            wg_port = shared_wg_port
            logger.info(f"Iran node {iran_node_id}: bind_port={bind_port}, wg_port={wg_port}")
            
            tunnel_name = f"wg-mesh-{mesh_id[:8]}-{iran_node_id[:8]}-{trans}-server"
            
            # Create tunnel record
            tunnel = Tunnel(
                name=tunnel_name,
                core="frp",
                type=trans,
                node_id=iran_node_id,
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
            
            # Apply FRP server to Iran node
            try:
                server_spec = {"bind_port": bind_port}
                server_spec_prepared = prepare_frp_spec_for_node(server_spec, iran_node, request)
                server_spec_prepared["mode"] = "server"
                
                response = await node_client.send_to_node(
                    node_id=iran_node_id,
                    endpoint="/api/agent/tunnels/apply",
                    data={
                        "tunnel_id": tunnel.id,
                        "core": "frp",
                        "type": trans,
                        "spec": server_spec_prepared
                    }
                )
                if response.get("status") == "error":
                    raise RuntimeError(f"Failed to apply FRP server: {response.get('message')}")
                
                tunnel.status = "active"
                await db.commit()
                
                endpoint = f"{iran_node_ip}:{wg_port}"
                iran_node_endpoints[iran_node_id][trans] = endpoint
                logger.info(f"Created FRP {trans} server on Iran node {iran_node_id}: {endpoint}")
            except Exception as e:
                logger.error(f"Failed to create FRP server on Iran node {iran_node_id}: {e}", exc_info=True)
                tunnel.status = "error"
                tunnel.error_message = str(e)
                await db.commit()
                continue
    
    if not iran_node_endpoints:
        raise HTTPException(
            status_code=500,
            detail="Failed to create FRP servers on any Iran node"
        )
    
    # Step 2: Create FRP clients
    # - Foreign nodes connect to ALL Iran servers with UNIQUE remote_ports (enables Foreign-to-Foreign)
    # - Iran nodes connect to OTHER Iran servers with shared_wg_port (for Iran-to-Iran connectivity)
    logger.info(f"Creating FRP clients: {len(foreign_nodes)} Foreign node(s) and {len(iran_nodes)} Iran node(s) connecting to {len(iran_nodes)} Iran server(s)")
    
    # Store unique remote_ports for Foreign nodes: foreign_node_remote_ports[node_id][iran_node_id][transport] = remote_port
    foreign_node_remote_ports = {}  # Only for Foreign nodes - enables Foreign-to-Foreign connectivity
    
    # Create FRP clients for Foreign nodes with unique remote_ports
    for foreign_node_id, foreign_node, _ in foreign_nodes:
        foreign_node_remote_ports[foreign_node_id] = {}
        
        for iran_node_id, iran_node, _ in iran_nodes:
            iran_node_ip = iran_node.node_metadata.get("ip_address")
            if not iran_node_ip:
                logger.warning(f"Iran node {iran_node_id} has no IP address, skipping")
                continue
            
            if iran_node_id not in iran_node_endpoints:
                continue
            
            foreign_node_remote_ports[foreign_node_id][iran_node_id] = {}
            
            for trans in transports_to_create:
                if trans not in iran_node_endpoints[iran_node_id]:
                    continue
                
                # Get the Iran server's endpoint and ports
                endpoint = iran_node_endpoints[iran_node_id][trans]
                iran_ip, _ = endpoint.rsplit(":", 1)
                
                # Calculate bind_port the same way as server (unique per Iran node)
                port_hash = int(hashlib.md5(f"{mesh_id}-{iran_node_id}-{trans}".encode()).hexdigest()[:8], 16)
                bind_port = 7000 + (port_hash % 1000)
                
                # Generate UNIQUE remote_port for each Foreign node on each Iran server
                # This enables Foreign-to-Foreign connectivity (each Foreign node has unique endpoint)
                remote_port_hash = int(hashlib.md5(f"{mesh_id}-{foreign_node_id}-{iran_node_id}-{trans}".encode()).hexdigest()[:8], 16)
                unique_remote_port = 18000 + (remote_port_hash % 1000)  # Different port range from shared_wg_port
                foreign_node_remote_ports[foreign_node_id][iran_node_id][trans] = unique_remote_port
                
                # WireGuard listens on shared_wg_port (local_port), but remote_port is unique per Foreign node
                local_port = shared_wg_port
                
                tunnel_name = f"wg-mesh-{mesh_id[:8]}-{foreign_node_id[:8]}-to-{iran_node_id[:8]}-{trans}-client"
                
                # Create tunnel record
                tunnel = Tunnel(
                    name=tunnel_name,
                    core="frp",
                    type=trans,
                    node_id=foreign_node_id,
                    spec={
                        "mode": "client",
                        "server_addr": iran_ip,
                        "server_port": bind_port,
                        "type": trans,
                        "local_ip": "127.0.0.1",
                        "local_port": local_port,
                        "remote_port": unique_remote_port,  # Unique per Foreign node
                    },
                    status="pending"
                )
                db.add(tunnel)
                await db.commit()
                await db.refresh(tunnel)
                
                # Apply FRP client to Foreign node
                try:
                    client_spec = {
                        "mode": "client",
                        "server_addr": iran_ip,
                        "server_port": bind_port,
                        "type": trans,
                        "local_ip": "127.0.0.1",
                        "local_port": local_port,
                        "remote_port": unique_remote_port,  # Unique per Foreign node
                    }
                    
                    response = await node_client.send_to_node(
                        node_id=foreign_node_id,
                        endpoint="/api/agent/tunnels/apply",
                        data={
                            "tunnel_id": tunnel.id,
                            "core": "frp",
                            "type": trans,
                            "spec": client_spec
                        }
                    )
                    if response.get("status") == "error":
                        raise RuntimeError(f"Failed to apply FRP client: {response.get('message')}")
                    
                    tunnel.status = "active"
                    await db.commit()
                    
                    logger.info(f"Created FRP {trans} client on Foreign node {foreign_node_id} connecting to Iran {iran_node_id}: {iran_ip}:{bind_port} -> remote_port={unique_remote_port}, local_port={local_port}")
                except Exception as e:
                    logger.error(f"Failed to create FRP client on Foreign node {foreign_node_id} to Iran {iran_node_id}: {e}", exc_info=True)
                    tunnel.status = "error"
                    tunnel.error_message = str(e)
                    await db.commit()
                    continue
    
    # Create FRP clients for Iran nodes connecting to other Iran servers (use shared_wg_port for Iran-to-Iran)
    for iran_node_id, iran_node, _ in iran_nodes:
        for other_iran_node_id, other_iran_node, _ in iran_nodes:
            # Skip if connecting to itself
            if iran_node_id == other_iran_node_id:
                continue
            
            other_iran_node_ip = other_iran_node.node_metadata.get("ip_address")
            if not other_iran_node_ip:
                logger.warning(f"Iran node {other_iran_node_id} has no IP address, skipping")
                continue
            
            if other_iran_node_id not in iran_node_endpoints:
                continue
            
            for trans in transports_to_create:
                if trans not in iran_node_endpoints[other_iran_node_id]:
                    continue
                
                endpoint = iran_node_endpoints[other_iran_node_id][trans]
                iran_ip, _ = endpoint.rsplit(":", 1)
                
                # Calculate bind_port the same way as server (unique per Iran node)
                port_hash = int(hashlib.md5(f"{mesh_id}-{other_iran_node_id}-{trans}".encode()).hexdigest()[:8], 16)
                bind_port = 7000 + (port_hash % 1000)
                
                # Iran nodes use shared_wg_port for both local_port and remote_port (Iran-to-Iran)
                wg_port = shared_wg_port
                
                tunnel_name = f"wg-mesh-{mesh_id[:8]}-{iran_node_id[:8]}-to-{other_iran_node_id[:8]}-{trans}-client"
                
                # Create tunnel record
                tunnel = Tunnel(
                    name=tunnel_name,
                    core="frp",
                    type=trans,
                    node_id=iran_node_id,
                    spec={
                        "mode": "client",
                        "server_addr": iran_ip,
                        "server_port": bind_port,
                        "type": trans,
                        "local_ip": "127.0.0.1",
                        "local_port": wg_port,
                        "remote_port": wg_port,
                    },
                    status="pending"
                )
                db.add(tunnel)
                await db.commit()
                await db.refresh(tunnel)
                
                # Apply FRP client to Iran node
                try:
                    client_spec = {
                        "mode": "client",
                        "server_addr": iran_ip,
                        "server_port": bind_port,
                        "type": trans,
                        "local_ip": "127.0.0.1",
                        "local_port": wg_port,
                        "remote_port": wg_port,
                    }
                    
                    response = await node_client.send_to_node(
                        node_id=iran_node_id,
                        endpoint="/api/agent/tunnels/apply",
                        data={
                            "tunnel_id": tunnel.id,
                            "core": "frp",
                            "type": trans,
                            "spec": client_spec
                        }
                    )
                    if response.get("status") == "error":
                        raise RuntimeError(f"Failed to apply FRP client: {response.get('message')}")
                    
                    tunnel.status = "active"
                    await db.commit()
                    
                    logger.info(f"Created FRP {trans} client on Iran node {iran_node_id} connecting to Iran {other_iran_node_id}: {endpoint}")
                except Exception as e:
                    logger.error(f"Failed to create FRP client on Iran node {iran_node_id} to Iran {other_iran_node_id}: {e}", exc_info=True)
                    tunnel.status = "error"
                    tunnel.error_message = str(e)
                    await db.commit()
                    continue
    
    # Step 3: Map endpoints for WireGuard peer configuration
    # For each node, determine which endpoint to use for each peer:
    # - If peer is Iran node: Use that Iran node's FRP server endpoint directly (Iran nodes connect directly)
    # - If peer is Foreign node: Use any Iran server endpoint (Iran server forwards to Foreign node's local_port)
    frp_endpoints = {}  # {node_id: {peer_id: {transport: endpoint, ...}, ...}, ...}
    
    for node_id, node_config in mesh_configs.items():
        frp_endpoints[node_id] = {}
        
        node_result = await db.execute(
            select(Node).where(Node.id == node_id)
        )
        node = node_result.scalar_one_or_none()
        if not node:
            continue
        
        # Get all peers for this node
        for peer_id, peer_config in mesh_configs.items():
            if peer_id == node_id:
                continue
            
            peer_result = await db.execute(
                select(Node).where(Node.id == peer_id)
            )
            peer_node = peer_result.scalar_one_or_none()
            if not peer_node:
                continue
            
            peer_role = peer_node.node_metadata.get("role", "iran")
            
            # Determine endpoint for this peer
            peer_endpoint_map = {}
            
            if peer_role == "iran":
                # Peer is Iran node: Use its own FRP server endpoint directly
                if peer_id in iran_node_endpoints:
                    peer_endpoint_map = iran_node_endpoints[peer_id].copy()
                    logger.info(f"Node {node_id} -> Iran peer {peer_id}: using Iran's FRP server endpoint")
            else:
                # Peer is Foreign node: Use Foreign node's unique remote_port on an Iran server
                # Each Foreign node has a unique remote_port on each Iran server for Foreign-to-Foreign connectivity
                # We can use any Iran server's IP with the Foreign node's unique remote_port
                if peer_id in foreign_node_remote_ports and iran_nodes:
                    first_iran_id, first_iran_node, _ = iran_nodes[0]
                    first_iran_ip = first_iran_node.node_metadata.get("ip_address")
                    if first_iran_ip and first_iran_id in foreign_node_remote_ports[peer_id]:
                        # Build endpoint map with Foreign peer's unique remote_ports
                        for trans in transports_to_create:
                            if trans in foreign_node_remote_ports[peer_id][first_iran_id]:
                                unique_remote_port = foreign_node_remote_ports[peer_id][first_iran_id][trans]
                                peer_endpoint_map[trans] = f"{first_iran_ip}:{unique_remote_port}"
                        logger.info(f"Node {node_id} -> Foreign peer {peer_id}: using Foreign's unique remote_port on Iran server {first_iran_id}")
            
            if peer_endpoint_map:
                frp_endpoints[node_id][peer_id] = peer_endpoint_map
    
    # Step 4: Apply WireGuard configuration to all nodes
    for node_id, node_config in mesh_configs.items():
        if node_id not in frp_endpoints:
            logger.warning(f"No FRP endpoints mapped for node {node_id}, skipping WireGuard config")
            continue
        
        node_result = await db.execute(
            select(Node).where(Node.id == node_id)
        )
        node = node_result.scalar_one_or_none()
        if not node:
            continue
        
        node_role = node.node_metadata.get("role", "iran")
        
        # Get peer endpoints for WireGuard config
        peer_endpoints = frp_endpoints[node_id]
        
        # Determine listen port for all nodes
        # All nodes (Iran and Foreign) should listen on the port where FRP forwards (local_port = shared_wg_port)
        # This allows FRP to forward traffic to WireGuard on all nodes
        listen_port = shared_wg_port
        logger.info(f"Node {node_id} (role: {node_role}) WireGuard will listen on port {listen_port} (shared FRP local_port)")
        
        # Generate WireGuard config
        wg_config = wireguard_mesh_manager.generate_wireguard_config(
            node_config,
            peer_endpoints,
            listen_port=listen_port
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
            logger.info(f"Applying WireGuard mesh to node {node_id} (role: {node_role}, listen_port: {listen_port})")
            response = await node_client.send_to_node(
                node_id=node_id,
                endpoint="/api/agent/mesh/apply",
                data={
                    "mesh_id": mesh_id,
                    "spec": spec
                }
            )
            if response.get("status") == "error":
                error_msg = response.get("message", "Unknown error")
                logger.error(f"Failed to apply mesh to node {node_id}: {error_msg}")
                raise RuntimeError(f"Failed to apply WireGuard to node {node_id}: {error_msg}")
            else:
                logger.info(f"Successfully applied WireGuard mesh to node {node_id}")
        except Exception as e:
            logger.error(f"Error applying mesh to node {node_id}: {e}", exc_info=True)
            raise
    
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

