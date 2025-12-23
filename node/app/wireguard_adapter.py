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
        
        # Check if interface already exists and bring it down first
        if self._interface_exists(interface_name):
            logger.info(f"WireGuard interface {interface_name} already exists, bringing it down first")
            try:
                config_path = self.config_dir / f"{interface_name}.conf"
                if config_path.exists():
                    subprocess.run(
                        [self.wg_quick_binary, "down", str(config_path)],
                        check=False,
                        capture_output=True
                    )
                else:
                    # Interface exists but no config file, try to remove it directly
                    subprocess.run(
                        ["ip", "link", "delete", interface_name],
                        check=False,
                        capture_output=True
                    )
            except Exception as e:
                logger.warning(f"Error bringing down existing interface: {e}")
        
        config_path = self.config_dir / f"{interface_name}.conf"
        
        wg_config = spec.get("config")
        if not wg_config:
            raise ValueError("WireGuard config is required in spec")
        
        config_path.write_text(wg_config, encoding="utf-8")
        os.chmod(config_path, 0o600)
        
        try:
            subprocess.run(
                [self.wg_quick_binary, "up", str(config_path)],
                check=True,
                capture_output=True,
                text=True
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

