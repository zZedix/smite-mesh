"""IP Address Management (IPAM) for Overlay IPs"""
import logging
import ipaddress
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import OverlayPool, OverlayAssignment, Node

logger = logging.getLogger(__name__)


class IPAMManager:
    """Manages overlay IP address allocation and assignment"""
    
    async def get_or_create_pool(self, db: AsyncSession, cidr: str, description: Optional[str] = None) -> OverlayPool:
        """Get existing pool or create new one"""
        result = await db.execute(
            select(OverlayPool).where(OverlayPool.cidr == cidr)
        )
        pool = result.scalar_one_or_none()
        
        if not pool:
            pool = OverlayPool(cidr=cidr, description=description)
            db.add(pool)
            await db.commit()
            await db.refresh(pool)
            logger.info(f"Created overlay pool: {cidr}")
        
        return pool
    
    async def get_pool(self, db: AsyncSession) -> Optional[OverlayPool]:
        """Get the overlay pool (assuming single pool for now)"""
        result = await db.execute(select(OverlayPool))
        return result.scalar_one_or_none()
    
    async def allocate_ip(
        self,
        db: AsyncSession,
        node_id: str,
        preferred_ip: Optional[str] = None,
        interface_name: str = "wg0"
    ) -> Optional[str]:
        """
        Allocate an overlay IP for a node
        
        Args:
            db: Database session
            node_id: Node ID to assign IP to
            preferred_ip: Optional preferred IP address
            interface_name: WireGuard interface name
        
        Returns:
            Allocated IP address or None if pool exhausted
        """
        pool = await self.get_pool(db)
        if not pool:
            logger.error("No overlay pool configured")
            return None
        
        try:
            network = ipaddress.ip_network(pool.cidr, strict=False)
        except ValueError as e:
            logger.error(f"Invalid CIDR in pool: {e}")
            return None
        
        existing_assignment = await db.execute(
            select(OverlayAssignment).where(OverlayAssignment.node_id == node_id)
        )
        existing = existing_assignment.scalar_one_or_none()
        
        if existing:
            logger.info(f"Node {node_id} already has overlay IP: {existing.overlay_ip}")
            return existing.overlay_ip
        
        if preferred_ip:
            try:
                ip = ipaddress.ip_address(preferred_ip)
                if ip not in network:
                    logger.warning(f"Preferred IP {preferred_ip} not in pool {pool.cidr}")
                    preferred_ip = None
                else:
                    existing_ip_check = await db.execute(
                        select(OverlayAssignment).where(OverlayAssignment.overlay_ip == preferred_ip)
                    )
                    if existing_ip_check.scalar_one_or_none():
                        logger.warning(f"Preferred IP {preferred_ip} already assigned")
                        preferred_ip = None
            except ValueError:
                logger.warning(f"Invalid preferred IP: {preferred_ip}")
                preferred_ip = None
        
        allocated_ip = preferred_ip
        
        if not allocated_ip:
            allocated_ip = await self._find_free_ip(db, network)
        
        if not allocated_ip:
            logger.error(f"No free IPs available in pool {pool.cidr}")
            return None
        
        assignment = OverlayAssignment(
            node_id=node_id,
            overlay_ip=allocated_ip,
            interface_name=interface_name
        )
        db.add(assignment)
        
        node_result = await db.execute(select(Node).where(Node.id == node_id))
        node = node_result.scalar_one_or_none()
        if node:
            if not node.node_metadata:
                node.node_metadata = {}
            node.node_metadata["overlay_ip"] = allocated_ip
            await db.commit()
            await db.refresh(assignment)
            logger.info(f"Allocated overlay IP {allocated_ip} to node {node_id} and updated node_metadata")
        else:
            await db.commit()
            await db.refresh(assignment)
            logger.info(f"Allocated overlay IP {allocated_ip} to node {node_id}")
        
        return allocated_ip
    
    async def _find_free_ip(self, db: AsyncSession, network: ipaddress.IPv4Network) -> Optional[str]:
        """Find first available IP in the network"""
        existing_result = await db.execute(select(OverlayAssignment))
        existing_assignments = existing_result.scalars().all()
        assigned_ips = {ipaddress.ip_address(assign.overlay_ip) for assign in existing_assignments}
        
        for host in network.hosts():
            if host not in assigned_ips:
                return str(host)
        
        return None
    
    async def release_ip(self, db: AsyncSession, node_id: str) -> bool:
        """Release overlay IP from a node"""
        result = await db.execute(
            select(OverlayAssignment).where(OverlayAssignment.node_id == node_id)
        )
        assignment = result.scalar_one_or_none()
        
        if assignment:
            ip = assignment.overlay_ip
            await db.delete(assignment)
            await db.commit()
            logger.info(f"Released overlay IP {ip} from node {node_id}")
            return True
        
        return False
    
    async def get_node_ip(self, db: AsyncSession, node_id: str) -> Optional[str]:
        """Get overlay IP for a node"""
        result = await db.execute(
            select(OverlayAssignment).where(OverlayAssignment.node_id == node_id)
        )
        assignment = result.scalar_one_or_none()
        return assignment.overlay_ip if assignment else None
    
    async def update_node_ip(
        self,
        db: AsyncSession,
        node_id: str,
        new_ip: str,
        interface_name: str = "wg0"
    ) -> bool:
        """Update overlay IP for a node (manual override)"""
        pool = await self.get_pool(db)
        if not pool:
            return False
        
        try:
            network = ipaddress.ip_network(pool.cidr, strict=False)
            ip = ipaddress.ip_address(new_ip)
            if ip not in network:
                logger.error(f"IP {new_ip} not in pool {pool.cidr}")
                return False
        except ValueError as e:
            logger.error(f"Invalid IP address: {e}")
            return False
        
        existing_ip_check = await db.execute(
            select(OverlayAssignment).where(
                OverlayAssignment.overlay_ip == new_ip,
                OverlayAssignment.node_id != node_id
            )
        )
        if existing_ip_check.scalar_one_or_none():
            logger.error(f"IP {new_ip} already assigned to another node")
            return False
        
        result = await db.execute(
            select(OverlayAssignment).where(OverlayAssignment.node_id == node_id)
        )
        assignment = result.scalar_one_or_none()
        
        if assignment:
            assignment.overlay_ip = new_ip
            assignment.interface_name = interface_name
        else:
            assignment = OverlayAssignment(
                node_id=node_id,
                overlay_ip=new_ip,
                interface_name=interface_name
            )
            db.add(assignment)
        
        await db.commit()
        await db.refresh(assignment)
        logger.info(f"Updated overlay IP for node {node_id} to {new_ip}")
        return True
    
    async def get_pool_status(self, db: AsyncSession) -> Dict[str, Any]:
        """Get overlay pool status and statistics"""
        pool = await self.get_pool(db)
        if not pool:
            return {
                "pool_exists": False,
                "total_ips": 0,
                "assigned_ips": 0,
                "available_ips": 0,
                "utilization": 0.0
            }
        
        try:
            network = ipaddress.ip_network(pool.cidr, strict=False)
            total_ips = network.num_addresses - 2
        except ValueError:
            return {
                "pool_exists": True,
                "cidr": pool.cidr,
                "total_ips": 0,
                "assigned_ips": 0,
                "available_ips": 0,
                "utilization": 0.0,
                "error": "Invalid CIDR"
            }
        
        result = await db.execute(select(OverlayAssignment))
        assignments = result.scalars().all()
        assigned_ips = len(assignments)
        available_ips = max(0, total_ips - assigned_ips)
        utilization = (assigned_ips / total_ips * 100) if total_ips > 0 else 0.0
        
        return {
            "pool_exists": True,
            "cidr": pool.cidr,
            "description": pool.description,
            "total_ips": total_ips,
            "assigned_ips": assigned_ips,
            "available_ips": available_ips,
            "utilization": round(utilization, 2),
            "exhausted": available_ips == 0
        }
    
    async def list_assignments(self, db: AsyncSession) -> List[Dict[str, Any]]:
        """List all overlay IP assignments with node information"""
        result = await db.execute(
            select(OverlayAssignment, Node).join(
                Node, OverlayAssignment.node_id == Node.id, isouter=True
            )
        )
        rows = result.all()
        
        assignments = []
        for assignment, node in rows:
            assignments.append({
                "node_id": assignment.node_id,
                "node_name": node.name if node else "Unknown",
                "overlay_ip": assignment.overlay_ip,
                "interface_name": assignment.interface_name,
                "assigned_at": assignment.assigned_at.isoformat() if assignment.assigned_at else None
            })
        
        return assignments


ipam_manager = IPAMManager()

