"""Overlay IP Manager for Node Agent"""
import logging
import subprocess
from pathlib import Path
from typing import Optional
import shutil

logger = logging.getLogger(__name__)


class OverlayManager:
    """Manages overlay IP assignment on WireGuard interface"""
    
    def __init__(self):
        self.interface_name = "wg0"
        self.current_ip: Optional[str] = None
    
    def assign_ip(self, overlay_ip: str, interface_name: str = "wg0", cidr: int = 32) -> bool:
        """
        Assign overlay IP to WireGuard interface
        
        Args:
            overlay_ip: IP address to assign
            interface_name: WireGuard interface name (default: wg0)
            cidr: CIDR prefix length (default: 32 for single IP)
        
        Returns:
            True if successful, False otherwise
        """
        try:
            ip_binary = shutil.which("ip")
            if not ip_binary:
                ip_binary = "/usr/sbin/ip"
            
            if self.current_ip:
                self.remove_ip(interface_name)
            
            cmd = [ip_binary, "addr", "add", f"{overlay_ip}/{cidr}", "dev", interface_name]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            
            if result.returncode != 0:
                if "File exists" in result.stderr or "already exists" in result.stderr.lower():
                    logger.info(f"IP {overlay_ip} already assigned to {interface_name}")
                    self.current_ip = overlay_ip
                    self.interface_name = interface_name
                    return True
                logger.error(f"Failed to assign IP: {result.stderr}")
                return False
            
            self.current_ip = overlay_ip
            self.interface_name = interface_name
            logger.info(f"Assigned overlay IP {overlay_ip} to {interface_name}")
            return True
            
        except Exception as e:
            logger.error(f"Error assigning overlay IP: {e}", exc_info=True)
            return False
    
    def remove_ip(self, interface_name: Optional[str] = None) -> bool:
        """
        Remove overlay IP from interface
        
        Args:
            interface_name: Interface name (uses current if not provided)
        
        Returns:
            True if successful, False otherwise
        """
        if not self.current_ip:
            return True
        
        interface = interface_name or self.interface_name
        
        try:
            ip_binary = shutil.which("ip")
            if not ip_binary:
                ip_binary = "/usr/sbin/ip"
            
            cmd = [ip_binary, "addr", "del", f"{self.current_ip}/32", "dev", interface]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            
            if result.returncode != 0:
                if "Cannot find" in result.stderr or "not found" in result.stderr.lower():
                    logger.info(f"IP {self.current_ip} not found on {interface}, may already be removed")
                else:
                    logger.warning(f"Failed to remove IP: {result.stderr}")
            
            self.current_ip = None
            logger.info(f"Removed overlay IP from {interface}")
            return True
            
        except Exception as e:
            logger.error(f"Error removing overlay IP: {e}", exc_info=True)
            return False
    
    def get_current_ip(self, interface_name: str = "wg0") -> Optional[str]:
        """Get current overlay IP from interface"""
        try:
            ip_binary = shutil.which("ip")
            if not ip_binary:
                ip_binary = "/usr/sbin/ip"
            
            cmd = [ip_binary, "addr", "show", interface_name]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            
            for line in result.stdout.splitlines():
                if "inet " in line:
                    parts = line.strip().split()
                    for part in parts:
                        if "/" in part and part.count('.') == 3:
                            ip = part.split('/')[0]
                            self.current_ip = ip
                            self.interface_name = interface_name
                            return ip
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting current IP: {e}")
            return None
    
    def ensure_interface_exists(self, interface_name: str = "wg0") -> bool:
        """Ensure WireGuard interface exists (create if needed)"""
        try:
            ip_binary = shutil.which("ip")
            if not ip_binary:
                ip_binary = "/usr/sbin/ip"
            
            cmd = [ip_binary, "link", "show", interface_name]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            
            if result.returncode == 0:
                return True
            
            wg_binary = shutil.which("wg")
            if not wg_binary:
                logger.error("WireGuard 'wg' binary not found")
                return False
            
            cmd = [wg_binary, "quick", "up", interface_name]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            
            if result.returncode == 0:
                logger.info(f"Created WireGuard interface {interface_name}")
                return True
            
            logger.warning(f"Interface {interface_name} does not exist and could not be created")
            return False
            
        except Exception as e:
            logger.error(f"Error ensuring interface exists: {e}", exc_info=True)
            return False


overlay_manager = OverlayManager()

