"""Nodes API endpoints"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from datetime import datetime
from pydantic import BaseModel
import httpx

from app.database import get_db
from app.models import Node
from app.node_client import NodeClient


router = APIRouter()


class NodeCreate(BaseModel):
    name: str
    ip_address: str
    api_port: int = 8888
    metadata: dict = {}


class NodeResponse(BaseModel):
    id: str
    name: str
    fingerprint: str
    status: str
    registered_at: datetime
    last_seen: datetime
    metadata: dict
    
    class Config:
        from_attributes = True
    


@router.post("", response_model=NodeResponse)
async def create_node(node: NodeCreate, db: AsyncSession = Depends(get_db)):
    """Register a new node"""
    import hashlib
    
    fingerprint_data = f"{node.ip_address}:{node.api_port}".encode()
    fingerprint = hashlib.sha256(fingerprint_data).hexdigest()[:16]
    
    result = await db.execute(select(Node).where(Node.fingerprint == fingerprint))
    existing = result.scalar_one_or_none()
    
    metadata = node.metadata.copy() if node.metadata else {}
    metadata["api_address"] = f"http://{node.ip_address}:{node.api_port}"
    metadata["ip_address"] = node.ip_address
    metadata["api_port"] = node.api_port
    
    incoming_role = node.metadata.get("role", "iran") if node.metadata else "iran"
    if incoming_role not in ["iran", "foreign"]:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid role '{incoming_role}'. Role must be either 'iran' or 'foreign'"
        )
    metadata["role"] = incoming_role
    
    if existing:
        existing_role = existing.node_metadata.get("role", "iran") if existing.node_metadata else "iran"
        if existing_role != incoming_role:
            raise HTTPException(
                status_code=409,
                detail=f"Node with this fingerprint already exists with role '{existing_role}'. "
                       f"Cannot register as '{incoming_role}'. "
                       f"Each node must have a consistent role."
            )
        
        existing.last_seen = datetime.utcnow()
        existing.status = "active"
        existing.node_metadata.update(metadata)
        existing.node_metadata["role"] = existing_role
        await db.commit()
        await db.refresh(existing)
        return NodeResponse(
            id=existing.id,
            name=existing.name,
            fingerprint=existing.fingerprint,
            status=existing.status,
            registered_at=existing.registered_at,
            last_seen=existing.last_seen,
            metadata=existing.node_metadata or {}
        )
    
    db_node = Node(
        name=node.name,
        fingerprint=fingerprint,
        status="active",
        node_metadata=metadata
    )
    db.add(db_node)
    await db.commit()
    await db.refresh(db_node)
    
    from app.ipam_manager import ipam_manager
    overlay_ip = await ipam_manager.allocate_ip(db, db_node.id)
    if overlay_ip:
        metadata["overlay_ip"] = overlay_ip
        db_node.node_metadata = metadata
        await db.commit()
        await db.refresh(db_node)
    
    return NodeResponse(
        id=db_node.id,
        name=db_node.name,
        fingerprint=db_node.fingerprint,
        status=db_node.status,
        registered_at=db_node.registered_at,
        last_seen=db_node.last_seen,
        metadata=db_node.node_metadata or {}
    )


@router.get("", response_model=List[NodeResponse])
async def list_nodes(db: AsyncSession = Depends(get_db)):
    """List all nodes with connection state"""
    result = await db.execute(select(Node))
    nodes = result.scalars().all()
    
    client = NodeClient()
    node_responses = []
    
    for node in nodes:
        connection_status = "failed"
        try:
            response = await client.get_tunnel_status(node.id, "")
            if response and response.get("status") == "ok":
                connection_status = "connected"
            else:
                error_msg = response.get("message", "Node disconnected") if response else "Node not responding"
                if "timeout" in error_msg.lower() or "connection" in error_msg.lower():
                    connection_status = "reconnecting"
                else:
                    connection_status = "failed"
        except httpx.ConnectError:
            connection_status = "connecting"
        except httpx.TimeoutException:
            connection_status = "reconnecting"
        except Exception:
            connection_status = "failed"
        
        metadata = node.node_metadata.copy() if node.node_metadata else {}
        metadata["connection_status"] = connection_status
        
        node_responses.append(NodeResponse(
            id=node.id,
            name=node.name,
            fingerprint=node.fingerprint,
            status=node.status,
            registered_at=node.registered_at,
            last_seen=node.last_seen,
            metadata=metadata
        ))
    
    return node_responses


@router.get("/{node_id}", response_model=NodeResponse)
async def get_node(node_id: str, db: AsyncSession = Depends(get_db)):
    """Get node by ID"""
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    return NodeResponse(
        id=node.id,
        name=node.name,
        fingerprint=node.fingerprint,
        status=node.status,
        registered_at=node.registered_at,
        last_seen=node.last_seen,
        metadata=node.node_metadata or {}
    )


@router.delete("/{node_id}")
async def delete_node(node_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a node"""
    result = await db.execute(select(Node).where(Node.id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    
    await db.delete(node)
    await db.commit()
    return {"status": "deleted"}

