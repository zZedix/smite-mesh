"""Agent API endpoints"""
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Dict, Any
import logging

router = APIRouter()
logger = logging.getLogger(__name__)



class TunnelApply(BaseModel):
    tunnel_id: str
    core: str
    type: str
    spec: Dict[str, Any]


class TunnelRemove(BaseModel):
    tunnel_id: str


@router.post("/tunnels/apply")
async def apply_tunnel(data: TunnelApply, request: Request):
    """Apply tunnel configuration"""
    logger = logging.getLogger(__name__)
    adapter_manager = request.app.state.adapter_manager
    
    logger.info(f"Applying tunnel {data.tunnel_id}: core={data.core}, type={data.type}")
    try:
        await adapter_manager.apply_tunnel(
            tunnel_id=data.tunnel_id,
            tunnel_core=data.core,
            spec=data.spec
        )
        logger.info(f"Tunnel {data.tunnel_id} applied successfully")
        return {"status": "success", "message": "Tunnel applied"}
    except Exception as e:
        logger.error(f"Failed to apply tunnel {data.tunnel_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tunnels/remove")
async def remove_tunnel(data: TunnelRemove, request: Request):
    """Remove tunnel"""
    adapter_manager = request.app.state.adapter_manager
    
    try:
        await adapter_manager.remove_tunnel(data.tunnel_id)
        return {"status": "success", "message": "Tunnel removed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tunnels/status")
async def get_tunnel_status(tunnel_id: str, request: Request):
    """Get tunnel status"""
    adapter_manager = request.app.state.adapter_manager
    
    try:
        status = await adapter_manager.get_tunnel_status(tunnel_id)
        
        # For Backhaul tunnels, also check if process is actually running
        tunnel = adapter_manager.active_tunnels.get(tunnel_id)
        if tunnel and hasattr(tunnel, 'name') and tunnel.name == 'backhaul':
            backhaul_status = tunnel.status(tunnel_id)
            status.update(backhaul_status)
        
        return {"status": "success", "data": status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
async def get_status(request: Request):
    """Get node status"""
    adapter_manager = request.app.state.adapter_manager
    
    return {
        "status": "ok",
        "active_tunnels": len(adapter_manager.active_tunnels),
        "tunnels": list(adapter_manager.active_tunnels.keys())
    }


class MeshApply(BaseModel):
    mesh_id: str
    spec: Dict[str, Any]


class MeshRemove(BaseModel):
    mesh_id: str


@router.post("/mesh/apply")
async def apply_mesh(data: MeshApply, request: Request):
    """Apply WireGuard mesh configuration"""
    if not hasattr(request.app.state, 'wireguard_adapter'):
        from app.wireguard_adapter import WireGuardAdapter
        request.app.state.wireguard_adapter = WireGuardAdapter()
    
    adapter = request.app.state.wireguard_adapter
    
    try:
        adapter.apply(data.mesh_id, data.spec)
        return {"status": "success", "message": "Mesh applied"}
    except Exception as e:
        logger.error(f"Failed to apply mesh {data.mesh_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mesh/remove")
async def remove_mesh(data: MeshRemove, request: Request):
    """Remove WireGuard mesh"""
    if not hasattr(request.app.state, 'wireguard_adapter'):
        return {"status": "success", "message": "Mesh not found"}
    
    adapter = request.app.state.wireguard_adapter
    
    try:
        adapter.remove(data.mesh_id)
        return {"status": "success", "message": "Mesh removed"}
    except Exception as e:
        logger.error(f"Failed to remove mesh {data.mesh_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mesh/{mesh_id}/status")
async def get_mesh_status(mesh_id: str, request: Request):
    """Get WireGuard mesh status"""
    if not hasattr(request.app.state, 'wireguard_adapter'):
        return {"status": "error", "message": "WireGuard adapter not initialized"}
    
    adapter = request.app.state.wireguard_adapter
    
    try:
        status = adapter.status(mesh_id)
        return {"status": "success", "data": status}
    except Exception as e:
        logger.error(f"Failed to get mesh status {mesh_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


class OverlayAssign(BaseModel):
    overlay_ip: str
    interface_name: str = "wg0"


@router.post("/overlay/assign")
async def assign_overlay_ip(data: OverlayAssign, request: Request):
    """Assign overlay IP to WireGuard interface"""
    from app.overlay_manager import overlay_manager
    
    try:
        if not overlay_manager.ensure_interface_exists(data.interface_name):
            raise HTTPException(status_code=500, detail="Failed to create WireGuard interface")
        
        success = overlay_manager.assign_ip(data.overlay_ip, data.interface_name)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to assign overlay IP")
        
        return {
            "status": "success",
            "overlay_ip": data.overlay_ip,
            "interface_name": data.interface_name
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to assign overlay IP: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/overlay/remove")
async def remove_overlay_ip(request: Request):
    """Remove overlay IP from interface"""
    from app.overlay_manager import overlay_manager
    
    try:
        success = overlay_manager.remove_ip()
        if not success:
            raise HTTPException(status_code=500, detail="Failed to remove overlay IP")
        
        return {"status": "success", "message": "Overlay IP removed"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to remove overlay IP: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/overlay/status")
async def get_overlay_status(request: Request):
    """Get current overlay IP status"""
    from app.overlay_manager import overlay_manager
    
    try:
        current_ip = overlay_manager.get_current_ip()
        return {
            "status": "success",
            "overlay_ip": current_ip,
            "interface_name": overlay_manager.interface_name
        }
    except Exception as e:
        logger.error(f"Failed to get overlay status: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

