"""Nodes API endpoints"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from typing import List
from datetime import datetime
from pydantic import BaseModel

from app.database import get_db
from app.models import Node


router = APIRouter()


class NodeCreate(BaseModel):
    name: str
    ip_address: str
    api_port: int = 8888
    role: str
    metadata: dict = {}


class NodeResponse(BaseModel):
    id: str
    name: str
    fingerprint: str
    status: str
    registered_at: datetime
    last_seen: datetime
    metadata: dict
    role: str
    
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
    
    # Validate and set role (explicit from payload)
    incoming_role = node.role
    if incoming_role not in ["iran", "foreign"]:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid role '{incoming_role}'. Role must be either 'iran' or 'foreign'"
        )
    metadata["role"] = incoming_role
    
    if existing:
        # Check if existing node has a different role - this prevents conflicts
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
        # Update metadata but preserve role consistency
        existing.node_metadata.update(metadata)
        # Ensure role is preserved
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
            metadata=existing.node_metadata or {},
            role=existing_role
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
    return NodeResponse(
        id=db_node.id,
        name=db_node.name,
        fingerprint=db_node.fingerprint,
        status=db_node.status,
        registered_at=db_node.registered_at,
        last_seen=db_node.last_seen,
        metadata=db_node.node_metadata or {},
        role=incoming_role
    )


@router.get("", response_model=List[NodeResponse])
async def list_nodes(db: AsyncSession = Depends(get_db)):
    """List all nodes"""
    result = await db.execute(select(Node))
    nodes = result.scalars().all()
    return [
        NodeResponse(
            id=n.id,
            name=n.name,
            fingerprint=n.fingerprint,
            status=n.status,
            registered_at=n.registered_at,
            last_seen=n.last_seen,
            metadata=n.node_metadata or {},
            role=n.node_metadata.get("role", "iran") if n.node_metadata else "iran"
        )
        for n in nodes
    ]


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
        metadata=node.node_metadata or {},
        role=node.node_metadata.get("role", "iran") if node.node_metadata else "iran"
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
