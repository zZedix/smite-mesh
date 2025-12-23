"""Overlay IP Management (IPAM) API endpoints"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
import logging
from datetime import datetime

from app.database import get_db
from app.models import OverlayPool, OverlayAssignment, Node
from app.ipam_manager import ipam_manager
from app.node_client import NodeClient

router = APIRouter()
logger = logging.getLogger(__name__)


class PoolCreate(BaseModel):
    cidr: str
    description: Optional[str] = None


class PoolResponse(BaseModel):
    id: str
    cidr: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime


class AssignmentResponse(BaseModel):
    node_id: str
    node_name: str
    overlay_ip: str
    interface_name: str
    assigned_at: Optional[datetime]


class StatusResponse(BaseModel):
    pool_exists: bool
    cidr: Optional[str] = None
    description: Optional[str] = None
    total_ips: int
    assigned_ips: int
    available_ips: int
    utilization: float
    exhausted: bool = False
    error: Optional[str] = None


class AssignIPRequest(BaseModel):
    preferred_ip: Optional[str] = None
    interface_name: str = "wg0"


@router.post("/pool", response_model=PoolResponse)
async def create_or_update_pool(
    pool: PoolCreate,
    db: AsyncSession = Depends(get_db)
):
    """Create or update overlay IP pool"""
    try:
        import ipaddress
        ipaddress.ip_network(pool.cidr, strict=False)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid CIDR: {str(e)}")
    
    db_pool = await ipam_manager.get_or_create_pool(db, pool.cidr, pool.description)
    
    if db_pool.description != pool.description:
        db_pool.description = pool.description
        await db.commit()
        await db.refresh(db_pool)
    
    return db_pool


@router.get("/pool", response_model=Optional[PoolResponse])
async def get_pool(db: AsyncSession = Depends(get_db)):
    """Get overlay IP pool"""
    pool = await ipam_manager.get_pool(db)
    return pool


@router.delete("/pool")
async def delete_pool(db: AsyncSession = Depends(get_db)):
    """Delete overlay IP pool and all assignments"""
    pool = await ipam_manager.get_pool(db)
    if not pool:
        raise HTTPException(status_code=404, detail="No overlay pool found")
    
    assignments_result = await db.execute(select(OverlayAssignment))
    assignments = assignments_result.scalars().all()
    
    for assignment in assignments:
        node_result = await db.execute(select(Node).where(Node.id == assignment.node_id))
        node = node_result.scalar_one_or_none()
        if node and node.node_metadata:
            node.node_metadata.pop("overlay_ip", None)
            await db.commit()
        await db.delete(assignment)
    
    await db.delete(pool)
    await db.commit()
    
    return {"status": "success", "message": "Pool and all assignments deleted"}


@router.post("/assign/{node_id}")
async def assign_ip(
    node_id: str,
    request: AssignIPRequest,
    db: AsyncSession = Depends(get_db)
):
    """Assign overlay IP to a node"""
    node_result = await db.execute(select(Node).where(Node.id == node_id))
    node = node_result.scalar_one_or_none()
    
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    
    allocated_ip = await ipam_manager.allocate_ip(
        db,
        node_id,
        request.preferred_ip,
        request.interface_name
    )
    
    if not allocated_ip:
        raise HTTPException(status_code=500, detail="Failed to allocate IP. Pool may be exhausted.")
    
    node_result = await db.execute(select(Node).where(Node.id == node_id))
    node = node_result.scalar_one_or_none()
    if node and node.node_metadata:
        node.node_metadata["overlay_ip"] = allocated_ip
        await db.commit()
    
    node_client = NodeClient()
    try:
        response = await node_client.send_to_node(
            node_id=node_id,
            endpoint="/api/agent/overlay/assign",
            data={
                "overlay_ip": allocated_ip,
                "interface_name": request.interface_name
            }
        )
        if response.get("status") == "error":
            logger.warning(f"Failed to apply overlay IP to node {node_id}: {response.get('message')}")
    except Exception as e:
        logger.warning(f"Error applying overlay IP to node {node_id}: {e}")
    
    return {
        "status": "success",
        "node_id": node_id,
        "overlay_ip": allocated_ip,
        "interface_name": request.interface_name
    }


@router.put("/assign/{node_id}")
async def update_assignment(
    node_id: str,
    request: AssignIPRequest,
    db: AsyncSession = Depends(get_db)
):
    """Update overlay IP assignment (manual override)"""
    if not request.preferred_ip:
        raise HTTPException(status_code=400, detail="preferred_ip is required for update")
    
    node_result = await db.execute(select(Node).where(Node.id == node_id))
    node = node_result.scalar_one_or_none()
    
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    
    success = await ipam_manager.update_node_ip(
        db,
        node_id,
        request.preferred_ip,
        request.interface_name
    )
    
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update IP. Check if IP is valid and available.")
    
    node_client = NodeClient()
    try:
        response = await node_client.send_to_node(
            node_id=node_id,
            endpoint="/api/agent/overlay/assign",
            data={
                "overlay_ip": request.preferred_ip,
                "interface_name": request.interface_name
            }
        )
        if response.get("status") == "error":
            logger.warning(f"Failed to apply overlay IP to node {node_id}: {response.get('message')}")
    except Exception as e:
        logger.warning(f"Error applying overlay IP to node {node_id}: {e}")
    
    return {
        "status": "success",
        "node_id": node_id,
        "overlay_ip": request.preferred_ip,
        "interface_name": request.interface_name
    }


@router.delete("/release/{node_id}")
async def release_ip(
    node_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Release overlay IP from a node"""
    success = await ipam_manager.release_ip(db, node_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="No overlay IP assigned to this node")
    
    node_client = NodeClient()
    try:
        await node_client.send_to_node(
            node_id=node_id,
            endpoint="/api/agent/overlay/remove",
            data={}
        )
    except Exception as e:
        logger.warning(f"Error removing overlay IP from node {node_id}: {e}")
    
    return {"status": "success", "message": "Overlay IP released"}


@router.get("/status", response_model=StatusResponse)
async def get_status(db: AsyncSession = Depends(get_db)):
    """Get overlay pool status"""
    status = await ipam_manager.get_pool_status(db)
    return status


@router.get("/assignments", response_model=List[AssignmentResponse])
async def list_assignments(db: AsyncSession = Depends(get_db)):
    """List all overlay IP assignments"""
    assignments = await ipam_manager.list_assignments(db)
    return assignments


@router.get("/node/{node_id}")
async def get_node_ip(
    node_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get overlay IP for a specific node"""
    ip = await ipam_manager.get_node_ip(db, node_id)
    if not ip:
        raise HTTPException(status_code=404, detail="No overlay IP assigned to this node")
    
    result = await db.execute(select(OverlayAssignment).where(OverlayAssignment.node_id == node_id))
    assignment = result.scalar_one_or_none()
    
    return {
        "node_id": node_id,
        "overlay_ip": ip,
        "interface_name": assignment.interface_name if assignment else "wg0"
    }


@router.post("/sync")
async def sync_node_ips(db: AsyncSession = Depends(get_db)):
    """Sync overlay IPs to all nodes that don't have them assigned"""
    pool = await ipam_manager.get_pool(db)
    if not pool:
        raise HTTPException(status_code=404, detail="No overlay pool configured")
    
    nodes_result = await db.execute(select(Node))
    nodes = nodes_result.scalars().all()
    
    synced = 0
    errors = []
    
    for node in nodes:
        existing_assignment = await db.execute(
            select(OverlayAssignment).where(OverlayAssignment.node_id == node.id)
        )
        assignment = existing_assignment.scalar_one_or_none()
        
        if not assignment:
            overlay_ip = await ipam_manager.allocate_ip(db, node.id)
            if overlay_ip:
                synced += 1
                logger.info(f"Synced overlay IP {overlay_ip} to node {node.id}")
            else:
                errors.append(f"Failed to allocate IP for node {node.id}")
        else:
            if node.node_metadata and node.node_metadata.get("overlay_ip") != assignment.overlay_ip:
                if not node.node_metadata:
                    node.node_metadata = {}
                node.node_metadata["overlay_ip"] = assignment.overlay_ip
                await db.commit()
                synced += 1
                logger.info(f"Synced overlay IP {assignment.overlay_ip} to node {node.id} metadata")
    
    return {
        "status": "success",
        "synced": synced,
        "errors": errors
    }

