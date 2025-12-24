"""WireGuard adapter for mesh networking"""
import logging
import subprocess
import os
from pathlib import Path
from typing import Dict, Any, Optional
import time

logger = logging.getLogger(__name__)


class WireGuardAdapter:
    """WireGuard mesh adapter - manages WireGuard interfaces and routing"""
    name = "wireguard"
    
    def __init__(self, config_dir: Optional[Path] = None):
        self.config_dir = config_dir or Path("/etc/smite-node/wireguard")
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.interfaces: Dict[str, str] = {}
        self._resolve_binary_paths()
    
    def _resolve_binary_paths(self):
        """Resolve WireGuard binary paths"""
        import shutil
        
        self.wg_binary = shutil.which("wg")
        if not self.wg_binary:
            for path in [Path("/usr/bin/wg"), Path("/usr/local/bin/wg")]:
                if path.exists():
                    self.wg_binary = str(path)
                    break
        
        self.wg_quick_binary = shutil.which("wg-quick")
        if not self.wg_quick_binary:
            for path in [Path("/usr/bin/wg-quick"), Path("/usr/local/bin/wg-quick")]:
                if path.exists():
                    self.wg_quick_binary = str(path)
                    break
        
        if not self.wg_binary or not self.wg_quick_binary:
            raise FileNotFoundError(
                "WireGuard binaries not found. Install wireguard-tools package."
            )
    
    def _get_interface_name(self, mesh_id: str) -> str:
        """Generate interface name for mesh"""
        return f"wg-{mesh_id[:8]}"
    
    def apply(self, mesh_id: str, spec: Dict[str, Any]):
        """Apply WireGuard mesh configuration"""
        interface_name = self._get_interface_name(mesh_id)
        config_path = self.config_dir / f"{interface_name}.conf"
        
        wg_config = spec.get("config")
        if not wg_config:
            raise ValueError("WireGuard config is required in spec")
        
        # Check if interface already exists and bring it down first
        if self._interface_exists(interface_name):
            logger.info(f"WireGuard interface {interface_name} already exists, bringing it down first")
            try:
                # Try wg-quick down first
                if config_path.exists():
                    result = subprocess.run(
                        [self.wg_quick_binary, "down", str(config_path)],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    if result.returncode != 0:
                        logger.warning(f"wg-quick down failed: {result.stderr}")
                
                # Also try direct ip link delete as fallback
                result = subprocess.run(
                    ["ip", "link", "delete", interface_name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode != 0 and "Cannot find device" not in result.stderr:
                    logger.warning(f"ip link delete failed: {result.stderr}")
                
                # Wait a bit for interface to be fully removed
                time.sleep(0.5)
                
                # Verify interface is gone
                if self._interface_exists(interface_name):
                    logger.warning(f"Interface {interface_name} still exists after cleanup, forcing removal")
                    # Force remove any remaining IP addresses
                    subprocess.run(
                        ["ip", "addr", "flush", "dev", interface_name],
                        check=False,
                        capture_output=True,
                        timeout=5
                    )
                    subprocess.run(
                        ["ip", "link", "set", interface_name, "down"],
                        check=False,
                        capture_output=True,
                        timeout=5
                    )
                    subprocess.run(
                        ["ip", "link", "delete", interface_name],
                        check=False,
                        capture_output=True,
                        timeout=5
                    )
                    time.sleep(0.5)
            except Exception as e:
                logger.warning(f"Error bringing down existing interface: {e}")
        
        # Extract AllowedIPs from config and clean up any existing routes
        allowed_ips = self._extract_allowed_ips(wg_config)
        for ip in allowed_ips:
            self._remove_route(ip)
        
        # Also check if the overlay IP is already assigned to another interface
        overlay_ip = None
        for line in wg_config.splitlines():
            if line.strip().startswith("Address = "):
                addr_line = line.split("=", 1)[1].strip()
                # Extract IP from "10.25.0.1/32"
                overlay_ip = addr_line.split("/")[0] if "/" in addr_line else addr_line
                break
        
        if overlay_ip:
            # Aggressively remove IP from any interface that might have it
            try:
                logger.info(f"Checking for existing IP assignment: {overlay_ip}")
                
                # Get list of all interfaces with their IPs
                result = subprocess.run(
                    ["ip", "-o", "addr", "show"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                # Find all interfaces that have this IP
                interfaces_with_ip = set()
                if overlay_ip in result.stdout:
                    for line in result.stdout.splitlines():
                        if overlay_ip in line and "inet" in line:
                            parts = line.split()
                            if len(parts) >= 2:
                                # Extract interface name (second field)
                                iface_with_ip = parts[1].strip()
                                interfaces_with_ip.add(iface_with_ip)
                                logger.warning(f"IP {overlay_ip} found on interface {iface_with_ip}, will remove")
                
                # Also check the target interface name specifically
                interfaces_with_ip.add(interface_name)
                
                # Also check common WireGuard interface name patterns
                for pattern in [f"wg-{mesh_id[:8]}", f"wg-{mesh_id[:8]}-*", "wg*"]:
                    # Try to find interfaces matching pattern
                    link_result = subprocess.run(
                        ["ip", "link", "show"],
                        capture_output=True,
                        text=True,
                        timeout=5
                    )
                    for line in link_result.stdout.splitlines():
                        if ":" in line and "wg" in line.lower():
                            parts = line.split(":", 2)
                            if len(parts) >= 2:
                                wg_iface = parts[1].strip().split("@")[0]
                                if wg_iface:
                                    interfaces_with_ip.add(wg_iface)
                
                # Remove IP from all found interfaces
                for iface in interfaces_with_ip:
                    # Try different CIDR formats and removal methods
                    for cidr_format in [f"{overlay_ip}/32", f"{overlay_ip}/128", overlay_ip]:
                        result = subprocess.run(
                            ["ip", "addr", "del", cidr_format, "dev", iface],
                            check=False,
                            capture_output=True,
                            text=True,
                            timeout=2
                        )
                        if result.returncode == 0:
                            logger.info(f"Successfully removed IP {overlay_ip} from interface {iface}")
                        elif "Cannot find device" not in result.stderr and "not found" not in result.stderr.lower():
                            logger.debug(f"Failed to remove {cidr_format} from {iface}: {result.stderr.strip()}")
                
                # Verify IP is actually removed by checking again
                verify_result = subprocess.run(
                    ["ip", "-o", "addr", "show"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
                if overlay_ip in verify_result.stdout:
                    logger.warning(f"IP {overlay_ip} still exists after cleanup, attempting force removal")
                    # Force remove from any remaining interface
                    for line in verify_result.stdout.splitlines():
                        if overlay_ip in line and "inet" in line:
                            parts = line.split()
                            if len(parts) >= 2:
                                remaining_iface = parts[1].strip()
                                logger.warning(f"Force removing IP {overlay_ip} from {remaining_iface}")
                                # Try ip addr flush as last resort
                                subprocess.run(
                                    ["ip", "addr", "flush", "dev", remaining_iface],
                                    check=False,
                                    capture_output=True,
                                    timeout=2
                                )
                                # Try removal again with all formats
                                for cidr_format in [f"{overlay_ip}/32", f"{overlay_ip}/128", overlay_ip]:
                                    subprocess.run(
                                        ["ip", "addr", "del", cidr_format, "dev", remaining_iface],
                                        check=False,
                                        capture_output=True,
                                        timeout=2
                                    )
                                        
            except Exception as e:
                logger.warning(f"Error during IP cleanup: {e}")
            
            # Wait a bit after cleanup to ensure IP is fully released
            time.sleep(0.5)
        
        # Write config file
        config_path.write_text(wg_config, encoding="utf-8")
        os.chmod(config_path, 0o600)
        
        try:
            subprocess.run(
                [self.wg_quick_binary, "up", str(config_path)],
                check=True,
                capture_output=True,
                text=True,
                timeout=10
            )
            logger.info(f"WireGuard interface {interface_name} brought up for mesh {mesh_id}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to bring up WireGuard interface: {e.stderr}")
            raise RuntimeError(f"Failed to start WireGuard: {e.stderr}")
        
        self.interfaces[mesh_id] = interface_name
        
        routes = spec.get("routes", [])
        if routes:
            self._setup_routes(interface_name, routes)
        
        self._enable_ip_forwarding()
    
    def _extract_allowed_ips(self, wg_config: str) -> list:
        """Extract AllowedIPs from WireGuard config"""
        allowed_ips = []
        for line in wg_config.splitlines():
            line = line.strip()
            if line.startswith("AllowedIPs"):
                # Extract IPs after = sign
                ips_str = line.split("=", 1)[1].strip() if "=" in line else ""
                # Split by comma and clean up
                for ip in ips_str.split(","):
                    ip = ip.strip()
                    if ip:
                        allowed_ips.append(ip)
        return allowed_ips
    
    def _remove_route(self, route: str):
        """Remove a route if it exists"""
        try:
            # Try to remove the route (ignore if it doesn't exist)
            subprocess.run(
                ["ip", "route", "del", route],
                check=False,
                capture_output=True,
                timeout=2
            )
        except Exception as e:
            logger.debug(f"Could not remove route {route}: {e}")
    
    def _interface_exists(self, interface_name: str) -> bool:
        """Check if WireGuard interface exists"""
        try:
            result = subprocess.run(
                ["ip", "link", "show", interface_name],
                capture_output=True,
                check=False
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _setup_routes(self, interface_name: str, routes: list):
        """Setup routes for remote LAN subnets"""
        for route in routes:
            try:
                # Check if route already exists
                result = subprocess.run(
                    ["ip", "route", "show", route, "dev", interface_name],
                    capture_output=True,
                    check=False
                )
                if result.returncode == 0:
                    logger.info(f"Route {route} already exists, skipping")
                    continue
                
                # Add route if it doesn't exist
                result = subprocess.run(
                    ["ip", "route", "add", route, "dev", interface_name],
                    check=False,
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    logger.info(f"Added route {route} via {interface_name}")
                else:
                    logger.warning(f"Failed to add route {route}: {result.stderr}")
            except Exception as e:
                logger.warning(f"Failed to add route {route}: {e}")
    
    def _enable_ip_forwarding(self):
        """Enable IPv4 forwarding"""
        try:
            with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
                f.write("1")
            logger.info("IPv4 forwarding enabled")
        except Exception as e:
            logger.warning(f"Failed to enable IPv4 forwarding: {e}")
    
    def remove(self, mesh_id: str):
        """Remove WireGuard mesh configuration"""
        if mesh_id not in self.interfaces:
            return
        
        interface_name = self.interfaces[mesh_id]
        config_path = self.config_dir / f"{interface_name}.conf"
        
        try:
            if config_path.exists():
                subprocess.run(
                    [self.wg_quick_binary, "down", str(config_path)],
                    check=False,
                    capture_output=True
                )
            logger.info(f"WireGuard interface {interface_name} brought down for mesh {mesh_id}")
        except Exception as e:
            logger.warning(f"Error bringing down WireGuard interface: {e}")
        
        if config_path.exists():
            try:
                config_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to remove config file: {e}")
        
        del self.interfaces[mesh_id]
    
    def status(self, mesh_id: str) -> Dict[str, Any]:
        """Get WireGuard mesh status"""
        if mesh_id not in self.interfaces:
            return {
                "active": False,
                "interface": None,
                "overlay_ip": None,
                "peers": []
            }
        
        interface_name = self.interfaces[mesh_id]
        overlay_ip = self._get_interface_ip(interface_name)
        
        try:
            result = subprocess.run(
                [self.wg_binary, "show", interface_name],
                capture_output=True,
                text=True,
                check=True
            )
            
            peers = self._parse_wg_status(result.stdout)
            
            return {
                "active": True,
                "interface": interface_name,
                "overlay_ip": overlay_ip,
                "peers": peers
            }
        except subprocess.CalledProcessError:
            return {
                "active": False,
                "interface": interface_name,
                "overlay_ip": overlay_ip,
                "peers": []
            }
    
    def _get_interface_ip(self, interface_name: str) -> Optional[str]:
        """Get IP address assigned to WireGuard interface"""
        try:
            import shutil
            ip_binary = shutil.which("ip") or "/usr/sbin/ip"
            result = subprocess.run(
                [ip_binary, "addr", "show", interface_name],
                capture_output=True,
                text=True,
                check=True
            )
            
            for line in result.stdout.splitlines():
                if "inet " in line:
                    parts = line.strip().split()
                    for part in parts:
                        if "/" in part and part.count('.') == 3:
                            return part.split('/')[0]
            
            return None
        except Exception as e:
            logger.warning(f"Failed to get interface IP: {e}")
            return None
    
    def _parse_wg_status(self, output: str) -> list:
        """Parse wg show output to extract peer information"""
        peers = []
        current_peer = None
        
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("peer:"):
                if current_peer:
                    peers.append(current_peer)
                current_peer = {"public_key": line.split(":", 1)[1].strip()}
            elif line.startswith("endpoint:") and current_peer:
                current_peer["endpoint"] = line.split(":", 1)[1].strip()
            elif line.startswith("allowed ips:") and current_peer:
                current_peer["allowed_ips"] = line.split(":", 1)[1].strip()
            elif line.startswith("latest handshake:") and current_peer:
                handshake = line.split(":", 1)[1].strip()
                if handshake and handshake != "(none)":
                    current_peer["last_handshake"] = handshake
                    current_peer["connected"] = True
                else:
                    current_peer["connected"] = False
        
        if current_peer:
            peers.append(current_peer)
        
        return peers

