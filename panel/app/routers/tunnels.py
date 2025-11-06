"""Tunnels API endpoints"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from datetime import datetime
from pydantic import BaseModel
import logging
import sys

from app.database import get_db
from app.models import Tunnel, Node
from app.hysteria2_client import Hysteria2Client


router = APIRouter()
logger = logging.getLogger(__name__)


class TunnelCreate(BaseModel):
    name: str
    core: str
    type: str
    node_id: str | None = None
    spec: dict


class TunnelUpdate(BaseModel):
    name: str | None = None
    spec: dict | None = None


class TunnelResponse(BaseModel):
    id: str
    name: str
    core: str
    type: str
    node_id: str
    spec: dict
    status: str
    error_message: str | None = None
    revision: int
    created_at: datetime
    updated_at: datetime
    
    class Config:
        from_attributes = True


@router.post("", response_model=TunnelResponse)
async def create_tunnel(tunnel: TunnelCreate, request: Request, db: AsyncSession = Depends(get_db)):
    """Create a new tunnel and auto-apply it"""
    from app.hysteria2_client import Hysteria2Client
    
    logger.info(f"Creating tunnel: name={tunnel.name}, type={tunnel.type}, core={tunnel.core}, node_id={tunnel.node_id}")
    
    node = None
    if tunnel.node_id:
        result = await db.execute(select(Node).where(Node.id == tunnel.node_id))
        node = result.scalar_one_or_none()
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")
    elif tunnel.core == "rathole":
        raise HTTPException(status_code=400, detail="Node is required for Rathole tunnels")
    
    db_tunnel = Tunnel(
        name=tunnel.name,
        core=tunnel.core,
        type=tunnel.type,
        node_id=tunnel.node_id or "",
        spec=tunnel.spec,
        status="pending"
    )
    db.add(db_tunnel)
    await db.commit()
    await db.refresh(db_tunnel)
    
    try:
        needs_gost_forwarding = db_tunnel.type in ["tcp", "udp", "ws", "grpc", "tcpmux"] and db_tunnel.core == "xray"
        needs_rathole_server = db_tunnel.core == "rathole"
        needs_node_apply = db_tunnel.core == "rathole"
        
        logger.info(f"Tunnel {db_tunnel.id}: needs_gost_forwarding={needs_gost_forwarding}, needs_rathole_server={needs_rathole_server}")
        
        if needs_rathole_server:
            remote_addr = db_tunnel.spec.get("remote_addr")
            token = db_tunnel.spec.get("token")
            proxy_port = db_tunnel.spec.get("remote_port") or db_tunnel.spec.get("listen_port")
            
            if remote_addr and ":" in remote_addr:
                rathole_port = remote_addr.split(":")[1]
                try:
                    if int(rathole_port) == 8000:
                        db_tunnel.status = "error"
                        db_tunnel.error_message = "Rathole server cannot use port 8000 (panel API port). Use a different port like 23333."
                        await db.commit()
                        await db.refresh(db_tunnel)
                        return db_tunnel
                except ValueError:
                    pass
            
            if remote_addr and token and proxy_port and hasattr(request.app.state, 'rathole_server_manager'):
                try:
                    logger.info(f"Starting Rathole server for tunnel {db_tunnel.id}: remote_addr={remote_addr}, token={token}, proxy_port={proxy_port}")
                    request.app.state.rathole_server_manager.start_server(
                        tunnel_id=db_tunnel.id,
                        remote_addr=remote_addr,
                        token=token,
                        proxy_port=int(proxy_port)
                    )
                    logger.info(f"Successfully started Rathole server for tunnel {db_tunnel.id}")
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Failed to start Rathole server for tunnel {db_tunnel.id}: {error_msg}", exc_info=True)
                    db_tunnel.status = "error"
                    db_tunnel.error_message = f"Rathole server error: {error_msg}"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
            else:
                missing = []
                if not remote_addr:
                    missing.append("remote_addr")
                if not token:
                    missing.append("token")
                if not proxy_port:
                    missing.append("proxy_port")
                if not hasattr(request.app.state, 'rathole_server_manager'):
                    missing.append("rathole_server_manager")
                logger.warning(f"Tunnel {db_tunnel.id}: Missing required fields for Rathole server: {missing}")
                if not remote_addr or not token or not proxy_port:
                    db_tunnel.status = "error"
                    db_tunnel.error_message = f"Missing required fields for Rathole: {missing}"
                    await db.commit()
                    await db.refresh(db_tunnel)
                    return db_tunnel
        
        if needs_node_apply:
            client = Hysteria2Client()
            if not node.node_metadata.get("api_address"):
                node.node_metadata["api_address"] = f"http://{node.node_metadata.get('ip_address', node.fingerprint)}:{node.node_metadata.get('api_port', 8888)}"
                await db.commit()
            
            logger.info(f"Applying tunnel {db_tunnel.id} to node {node.id}")
            response = await client.send_to_node(
                node_id=node.id,
                endpoint="/api/agent/tunnels/apply",
                data={
                    "tunnel_id": db_tunnel.id,
                    "core": db_tunnel.core,
                    "type": db_tunnel.type,
                    "spec": db_tunnel.spec
                }
            )
            
            if response.get("status") == "error":
                db_tunnel.status = "error"
                error_msg = response.get("message", "Unknown error from node")
                db_tunnel.error_message = f"Node error: {error_msg}"
                logger.error(f"Tunnel {db_tunnel.id}: {error_msg}")
                if needs_rathole_server and hasattr(request.app.state, 'rathole_server_manager'):
                    try:
                        request.app.state.rathole_server_manager.stop_server(db_tunnel.id)
                    except:
                        pass
                await db.commit()
                await db.refresh(db_tunnel)
                return db_tunnel
            
            if response.get("status") != "success":
                db_tunnel.status = "error"
                db_tunnel.error_message = "Failed to apply tunnel to node. Check node connection."
                logger.error(f"Tunnel {db_tunnel.id}: Failed to apply to node")
                if needs_rathole_server and hasattr(request.app.state, 'rathole_server_manager'):
                    try:
                        request.app.state.rathole_server_manager.stop_server(db_tunnel.id)
                    except:
                        pass
                await db.commit()
                await db.refresh(db_tunnel)
                return db_tunnel
        
        db_tunnel.status = "active"
        
        try:
            
            if needs_gost_forwarding:
                listen_port = db_tunnel.spec.get("listen_port")
                forward_to = db_tunnel.spec.get("forward_to")
                
                if not forward_to:
                    remote_ip = db_tunnel.spec.get("remote_ip", "127.0.0.1")
                    remote_port = db_tunnel.spec.get("remote_port", 8080)
                    forward_to = f"{remote_ip}:{remote_port}"
                
                panel_port = listen_port or db_tunnel.spec.get("remote_port")
                
                if panel_port and forward_to and hasattr(request.app.state, 'gost_forwarder'):
                    try:
                        logger.info(f"Starting gost forwarding for tunnel {db_tunnel.id}: {db_tunnel.type}://:{panel_port} -> {forward_to}")
                        request.app.state.gost_forwarder.start_forward(
                            tunnel_id=db_tunnel.id,
                            local_port=int(panel_port),
                            forward_to=forward_to,
                            tunnel_type=db_tunnel.type
                        )
                        logger.info(f"Successfully started gost forwarding for tunnel {db_tunnel.id}")
                    except Exception as e:
                        error_msg = str(e)
                        logger.error(f"Failed to start gost forwarding for tunnel {db_tunnel.id}: {error_msg}", exc_info=True)
                        db_tunnel.status = "error"
                        db_tunnel.error_message = f"Gost forwarding error: {error_msg}"
                else:
                    missing = []
                    if not panel_port:
                        missing.append("panel_port")
                    if not forward_to:
                        missing.append("forward_to")
                    if not hasattr(request.app.state, 'gost_forwarder'):
                        missing.append("gost_forwarder")
                    logger.warning(f"Tunnel {db_tunnel.id}: Missing required fields: {missing}")
                    if not forward_to:
                        error_msg = "forward_to is required for gost tunnels"
                        db_tunnel.status = "error"
                        db_tunnel.error_message = error_msg
            
        except Exception as e:
            logger.error(f"Exception in forwarding setup for tunnel {db_tunnel.id}: {e}", exc_info=True)
        
        await db.commit()
        await db.refresh(db_tunnel)
    except Exception as e:
        logger.error(f"Exception in tunnel creation for {db_tunnel.id}: {e}", exc_info=True)
        error_msg = str(e)
        db_tunnel.status = "error"
        db_tunnel.error_message = f"Tunnel creation error: {error_msg}"
        await db.commit()
        await db.refresh(db_tunnel)
    
    return db_tunnel


@router.get("", response_model=List[TunnelResponse])
async def list_tunnels(db: AsyncSession = Depends(get_db)):
    """List all tunnels"""
    result = await db.execute(select(Tunnel))
    tunnels = result.scalars().all()
    return tunnels


@router.get("/{tunnel_id}", response_model=TunnelResponse)
async def get_tunnel(tunnel_id: str, db: AsyncSession = Depends(get_db)):
    """Get tunnel by ID"""
    result = await db.execute(select(Tunnel).where(Tunnel.id == tunnel_id))
    tunnel = result.scalar_one_or_none()
    if not tunnel:
        raise HTTPException(status_code=404, detail="Tunnel not found")
    return tunnel


@router.put("/{tunnel_id}", response_model=TunnelResponse)
async def update_tunnel(
    tunnel_id: str,
    tunnel_update: TunnelUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """Update a tunnel and re-apply if spec changed"""
    from app.hysteria2_client import Hysteria2Client
    
    result = await db.execute(select(Tunnel).where(Tunnel.id == tunnel_id))
    tunnel = result.scalar_one_or_none()
    if not tunnel:
        raise HTTPException(status_code=404, detail="Tunnel not found")
    
    spec_changed = tunnel_update.spec is not None and tunnel_update.spec != tunnel.spec
    
    if tunnel_update.name is not None:
        tunnel.name = tunnel_update.name
    if tunnel_update.spec is not None:
        tunnel.spec = tunnel_update.spec
    
    tunnel.revision += 1
    tunnel.updated_at = datetime.utcnow()
    
    await db.commit()
    await db.refresh(tunnel)
    
    if spec_changed:
        try:
            needs_gost_forwarding = tunnel.type in ["tcp", "udp", "ws", "grpc", "tcpmux"] and tunnel.core == "xray"
            needs_rathole_server = tunnel.core == "rathole"
            needs_node_apply = tunnel.core == "rathole"
            
            if needs_gost_forwarding:
                listen_port = tunnel.spec.get("listen_port")
                forward_to = tunnel.spec.get("forward_to")
                
                if not forward_to:
                    remote_ip = tunnel.spec.get("remote_ip", "127.0.0.1")
                    remote_port = tunnel.spec.get("remote_port", 8080)
                    forward_to = f"{remote_ip}:{remote_port}"
                
                panel_port = listen_port or tunnel.spec.get("remote_port")
                
                if panel_port and forward_to and hasattr(request.app.state, 'gost_forwarder'):
                    try:
                        request.app.state.gost_forwarder.stop_forward(tunnel.id)
                        import time
                        time.sleep(0.5)
                        logger.info(f"Restarting gost forwarding for tunnel {tunnel.id}: {tunnel.type}://:{panel_port} -> {forward_to}")
                        request.app.state.gost_forwarder.start_forward(
                            tunnel_id=tunnel.id,
                            local_port=int(panel_port),
                            forward_to=forward_to,
                            tunnel_type=tunnel.type
                        )
                        tunnel.status = "active"
                        tunnel.error_message = None
                        logger.info(f"Successfully restarted gost forwarding for tunnel {tunnel.id}")
                    except Exception as e:
                        error_msg = str(e)
                        logger.error(f"Failed to restart gost forwarding for tunnel {tunnel.id}: {error_msg}", exc_info=True)
                        tunnel.status = "error"
                        tunnel.error_message = f"Gost forwarding error: {error_msg}"
                else:
                    if not forward_to:
                        tunnel.status = "error"
                        tunnel.error_message = "forward_to is required for gost tunnels"
            
            elif needs_rathole_server:
                if hasattr(request.app.state, 'rathole_server_manager'):
                    remote_addr = tunnel.spec.get("remote_addr")
                    token = tunnel.spec.get("token")
                    proxy_port = tunnel.spec.get("remote_port") or tunnel.spec.get("listen_port")
                    
                    if remote_addr and token and proxy_port:
                        try:
                            request.app.state.rathole_server_manager.stop_server(tunnel.id)
                            request.app.state.rathole_server_manager.start_server(
                                tunnel_id=tunnel.id,
                                remote_addr=remote_addr,
                                token=token,
                                proxy_port=int(proxy_port)
                            )
                            tunnel.status = "active"
                            tunnel.error_message = None
                        except Exception as e:
                            logger.error(f"Failed to restart Rathole server: {e}")
                            tunnel.status = "error"
                            tunnel.error_message = f"Rathole server error: {str(e)}"
            
            if needs_node_apply and tunnel.node_id:
                result = await db.execute(select(Node).where(Node.id == tunnel.node_id))
                node = result.scalar_one_or_none()
                if node:
                    client = Hysteria2Client()
                    try:
                        response = await client.send_to_node(
                            node_id=node.id,
                            endpoint="/api/agent/tunnels/apply",
                            data={
                                "tunnel_id": tunnel.id,
                                "core": tunnel.core,
                                "type": tunnel.type,
                                "spec": tunnel.spec
                            }
                        )
                        
                        if response.get("status") == "success":
                            tunnel.status = "active"
                            tunnel.error_message = None
                        else:
                            tunnel.status = "error"
                            tunnel.error_message = f"Node error: {response.get('message', 'Unknown error')}"
                    except Exception as e:
                        logger.error(f"Failed to re-apply tunnel to node: {e}")
                        tunnel.status = "error"
                        tunnel.error_message = f"Node error: {str(e)}"
            
            await db.commit()
            await db.refresh(tunnel)
        except Exception as e:
            logger.error(f"Failed to re-apply tunnel: {e}", exc_info=True)
            tunnel.status = "error"
            tunnel.error_message = f"Re-apply error: {str(e)}"
            await db.commit()
            await db.refresh(tunnel)
    
    return tunnel


@router.post("/{tunnel_id}/apply")
async def apply_tunnel(tunnel_id: str, db: AsyncSession = Depends(get_db)):
    """Apply tunnel configuration to node"""
    result = await db.execute(select(Tunnel).where(Tunnel.id == tunnel_id))
    tunnel = result.scalar_one_or_none()
    if not tunnel:
        raise HTTPException(status_code=404, detail="Tunnel not found")
    
    result = await db.execute(select(Node).where(Node.id == tunnel.node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    
    client = Hysteria2Client()
    try:
        if not node.node_metadata.get("api_address"):
            node.node_metadata["api_address"] = f"http://{node.fingerprint}:8888"
            await db.commit()
        
        response = await client.send_to_node(
            node_id=node.id,
            endpoint="/api/agent/tunnels/apply",
            data={
                "tunnel_id": tunnel.id,
                "core": tunnel.core,
                "type": tunnel.type,
                "spec": tunnel.spec
            }
        )
        
        if response.get("status") == "success":
            tunnel.status = "active"
            await db.commit()
            return {"status": "applied", "message": "Tunnel applied successfully"}
        else:
            error_msg = response.get("message", "Failed to apply tunnel")
            tunnel.status = "error"
            await db.commit()
            raise HTTPException(status_code=500, detail=error_msg)
    except HTTPException:
        raise
    except Exception as e:
        tunnel.status = "error"
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Failed to apply tunnel: {str(e)}")


@router.delete("/{tunnel_id}")
async def delete_tunnel(tunnel_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Delete a tunnel"""
    result = await db.execute(select(Tunnel).where(Tunnel.id == tunnel_id))
    tunnel = result.scalar_one_or_none()
    if not tunnel:
        raise HTTPException(status_code=404, detail="Tunnel not found")
    
    needs_gost_forwarding = tunnel.type in ["tcp", "udp", "ws", "grpc"] and tunnel.core == "xray"
    needs_rathole_server = tunnel.core == "rathole"
    
    if needs_gost_forwarding:
        if hasattr(request.app.state, 'gost_forwarder'):
            try:
                request.app.state.gost_forwarder.stop_forward(tunnel.id)
            except Exception as e:
                import logging
                logging.error(f"Failed to stop gost forwarding: {e}")
    
    elif needs_rathole_server:
        if hasattr(request.app.state, 'rathole_server_manager'):
            try:
                request.app.state.rathole_server_manager.stop_server(tunnel.id)
            except Exception as e:
                import logging
                logging.error(f"Failed to stop Rathole server: {e}")
    
    if tunnel.status == "active":
        result = await db.execute(select(Node).where(Node.id == tunnel.node_id))
        node = result.scalar_one_or_none()
        if node:
            client = Hysteria2Client()
            try:
                await client.send_to_node(
                    node_id=node.id,
                    endpoint="/api/agent/tunnels/remove",
                    data={"tunnel_id": tunnel.id}
                )
            except:
                pass
    
    await db.delete(tunnel)
    await db.commit()
    return {"status": "deleted"}


@router.put("/{tunnel_id}", response_model=TunnelResponse)
async def update_tunnel(tunnel_id: str, tunnel: TunnelUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    """Update a tunnel"""
    from app.hysteria2_client import Hysteria2Client
    
    result = await db.execute(select(Tunnel).where(Tunnel.id == tunnel_id))
    db_tunnel = result.scalar_one_or_none()
    if not db_tunnel:
        raise HTTPException(status_code=404, detail="Tunnel not found")
    
    if tunnel.name is not None:
        db_tunnel.name = tunnel.name
    if tunnel.spec is not None:
        db_tunnel.spec = tunnel.spec
    
    db_tunnel.revision += 1
    db_tunnel.updated_at = datetime.utcnow()
    
    if tunnel.spec is not None:
        result = await db.execute(select(Node).where(Node.id == db_tunnel.node_id))
        node = result.scalar_one_or_none()
        if node:
            client = Hysteria2Client()
            try:
                old_needs_gost_forwarding = db_tunnel.type in ["tcp", "udp", "ws", "grpc"] and db_tunnel.core == "xray"
                old_needs_rathole_server = db_tunnel.core == "rathole"
                
                if old_needs_gost_forwarding:
                    if hasattr(request.app.state, 'gost_forwarder'):
                        try:
                            request.app.state.gost_forwarder.stop_forward(db_tunnel.id)
                        except:
                            pass
                elif old_needs_rathole_server:
                    if hasattr(request.app.state, 'rathole_server_manager'):
                        try:
                            request.app.state.rathole_server_manager.stop_server(db_tunnel.id)
                        except:
                            pass
                
                response = await client.send_to_node(
                    node_id=node.id,
                    endpoint="/api/agent/tunnels/apply",
                    data={
                        "tunnel_id": db_tunnel.id,
                        "core": db_tunnel.core,
                        "type": db_tunnel.type,
                        "spec": db_tunnel.spec
                    }
                )
                
                if response.get("status") == "success":
                    db_tunnel.status = "active"
                    needs_gost_forwarding = db_tunnel.type in ["tcp", "udp", "ws", "grpc"] and db_tunnel.core == "xray"
                    needs_rathole_server = db_tunnel.core == "rathole"
                    
                    if needs_gost_forwarding:
                        forward_to = db_tunnel.spec.get("forward_to")
                        if not forward_to:
                            remote_ip = db_tunnel.spec.get("remote_ip", "127.0.0.1")
                            remote_port = db_tunnel.spec.get("remote_port", 8080)
                            forward_to = f"{remote_ip}:{remote_port}"
                        
                        panel_port = db_tunnel.spec.get("listen_port") or db_tunnel.spec.get("remote_port")
                        
                        if panel_port and forward_to and hasattr(request.app.state, 'gost_forwarder'):
                            try:
                                request.app.state.gost_forwarder.start_forward(
                                    tunnel_id=db_tunnel.id,
                                    local_port=int(panel_port),
                                    forward_to=forward_to,
                                    tunnel_type=db_tunnel.type
                                )
                            except Exception as e:
                                import logging
                                logging.error(f"Failed to start gost forwarding: {e}")
                    
                    elif needs_rathole_server:
                        remote_addr = db_tunnel.spec.get("remote_addr")
                        token = db_tunnel.spec.get("token")
                        proxy_port = db_tunnel.spec.get("remote_port") or db_tunnel.spec.get("listen_port")
                        
                        if remote_addr and token and proxy_port and hasattr(request.app.state, 'rathole_server_manager'):
                            try:
                                success = request.app.state.rathole_server_manager.start_server(
                                    tunnel_id=db_tunnel.id,
                                    remote_addr=remote_addr,
                                    token=token,
                                    proxy_port=int(proxy_port)
                                )
                                if not success:
                                    db_tunnel.status = "error"
                            except Exception as e:
                                import logging
                                logging.error(f"Failed to start Rathole server: {e}")
                                db_tunnel.status = "error"
                else:
                    db_tunnel.status = "error"
            except Exception as e:
                db_tunnel.status = "error"
                import logging
                logging.error(f"Failed to reapply tunnel: {e}")
    
    await db.commit()
    await db.refresh(db_tunnel)
    return db_tunnel

