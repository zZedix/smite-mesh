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
        self.obfuscator_processes: Dict[str, Dict[str, subprocess.Popen]] = {}  # mesh_id -> {peer_key: process}
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
        
        # Check for wg-obfuscator (optional - available if installed)
        self.wg_obfuscator_binary = shutil.which("wg-obfuscator")
        if not self.wg_obfuscator_binary:
            for path in [Path("/usr/bin/wg-obfuscator"), Path("/usr/local/bin/wg-obfuscator")]:
                if path.exists():
                    self.wg_obfuscator_binary = str(path)
                    break
        if self.wg_obfuscator_binary:
            logger.info(f"wg-obfuscator found at {self.wg_obfuscator_binary}")
        else:
            logger.debug("wg-obfuscator not found (optional, obfuscation will be disabled)")
    
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
        # Also check for any existing config file and bring it down
        if config_path.exists():
            logger.info(f"Existing WireGuard config found at {config_path}, bringing it down first")
            try:
                result = subprocess.run(
                    [self.wg_quick_binary, "down", str(config_path)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    logger.info(f"Successfully brought down existing WireGuard config")
                else:
                    logger.warning(f"wg-quick down failed: {result.stderr}")
                time.sleep(0.3)
            except Exception as e:
                logger.warning(f"Error bringing down existing config: {e}")
        
        if self._interface_exists(interface_name):
            logger.info(f"WireGuard interface {interface_name} already exists, bringing it down first")
            try:
                # Try wg-quick down first (even if config doesn't exist, try with interface name)
                result = subprocess.run(
                    [self.wg_quick_binary, "down", interface_name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                
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
        
        # Clean up any existing obfuscator processes for this mesh
        self._cleanup_obfuscator_processes(mesh_id)
        
        # Apply wg-obfuscator if available and modify config
        if self.wg_obfuscator_binary:
            try:
                wg_config = self._apply_obfuscation(mesh_id, wg_config)
                logger.info("Applied wg-obfuscator to WireGuard config")
            except Exception as e:
                logger.warning(f"Failed to apply wg-obfuscator, continuing without obfuscation: {e}")
        
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
        # Clean up obfuscator processes first
        self._cleanup_obfuscator_processes(mesh_id)
        
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
        
        # Clean up obfuscator config files
        import glob
        obfuscator_config_pattern = str(self.config_dir / f"obfuscator-{mesh_id[:8]}-*.conf")
        for config_file in glob.glob(obfuscator_config_pattern):
            try:
                os.unlink(config_file)
            except Exception as e:
                logger.debug(f"Failed to remove obfuscator config {config_file}: {e}")
        
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
    
    def _apply_obfuscation(self, mesh_id: str, wg_config: str) -> str:
        """Apply wg-obfuscator to WireGuard config - modify endpoints to use obfuscator"""
        import hashlib
        import re
        
        if not self.wg_obfuscator_binary:
            return wg_config
        
        # Initialize obfuscator processes dict for this mesh
        if mesh_id not in self.obfuscator_processes:
            self.obfuscator_processes[mesh_id] = {}
        
        # Parse WireGuard config to find peer sections
        lines = wg_config.splitlines()
        modified_lines = []
        current_peer_key = None
        current_peer_endpoint = None
        in_peer_section = False
        peer_lines = []
        
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            
            # Detect start of peer section
            if line.startswith("[Peer]"):
                # Process previous peer if any
                if current_peer_key and current_peer_endpoint:
                    modified_lines.extend(self._process_peer_with_obfuscator(
                        mesh_id, current_peer_key, current_peer_endpoint, peer_lines
                    ))
                else:
                    modified_lines.extend(peer_lines)
                
                # Start new peer
                peer_lines = [lines[i]]
                in_peer_section = True
                current_peer_key = None
                current_peer_endpoint = None
                i += 1
                continue
            
            if in_peer_section:
                peer_lines.append(lines[i])
                
                # Extract public key (used as peer identifier)
                if line.startswith("PublicKey = "):
                    current_peer_key = line.split("=", 1)[1].strip()
                
                # Extract endpoint
                elif line.startswith("Endpoint = "):
                    current_peer_endpoint = line.split("=", 1)[1].strip()
                
                # Check if this is the end of peer section (next [Peer] or end of file)
                if i == len(lines) - 1:
                    # Last line, process this peer
                    if current_peer_key and current_peer_endpoint:
                        modified_lines.extend(self._process_peer_with_obfuscator(
                            mesh_id, current_peer_key, current_peer_endpoint, peer_lines
                        ))
                    else:
                        modified_lines.extend(peer_lines)
                    in_peer_section = False
                
                i += 1
            else:
                modified_lines.append(lines[i])
                i += 1
        
        return "\n".join(modified_lines)
    
    def _process_peer_with_obfuscator(
        self, mesh_id: str, peer_key: str, endpoint: str, peer_lines: list
    ) -> list:
        """Process a peer endpoint through wg-obfuscator"""
        import hashlib
        import re
        
        # Parse endpoint (format: IP:port or [IPv6]:port)
        endpoint_match = re.match(r'^\[?([^\]]+)\]?:(\d+)$', endpoint.strip())
        if not endpoint_match:
            logger.warning(f"Could not parse endpoint {endpoint}, skipping obfuscation")
            return peer_lines
        
        real_host = endpoint_match.group(1)
        real_port = int(endpoint_match.group(2))
        
        # Generate unique local port for this peer's obfuscator
        port_hash = int(hashlib.md5(f"{mesh_id}-{peer_key}-{endpoint}".encode()).hexdigest()[:8], 16)
        local_port = 19000 + (port_hash % 5000)  # Use ports 19000-23999
        
        # Create obfuscator config
        obfuscator_config_path = self.config_dir / f"obfuscator-{mesh_id[:8]}-{peer_key[:8]}.conf"
        
        # Generate source port (for static bindings)
        source_port_hash = int(hashlib.md5(f"{mesh_id}-{peer_key}-source".encode()).hexdigest()[:8], 16)
        source_port = 24000 + (source_port_hash % 1000)  # Use ports 24000-24999
        
        # wg-obfuscator client config: only [client] section, use 'listen' not 'bind-addr'
        obfuscator_config = f"""[client]
listen = 127.0.0.1:{local_port}
server-endpoint = {real_host}:{real_port}
source-lport = {source_port}
"""
        
        try:
            obfuscator_config_path.write_text(obfuscator_config, encoding="utf-8")
            os.chmod(obfuscator_config_path, 0o600)
            
            # Start wg-obfuscator process
            process = subprocess.Popen(
                [self.wg_obfuscator_binary, "-c", str(obfuscator_config_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True
            )
            
            # Store process for cleanup
            self.obfuscator_processes[mesh_id][peer_key] = process
            
            # Give obfuscator a moment to start
            time.sleep(0.2)
            
            # Check if process is still running
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                logger.error(f"wg-obfuscator failed to start: {stderr.decode()}")
                raise RuntimeError(f"wg-obfuscator process died: {stderr.decode()}")
            
            logger.info(f"Started wg-obfuscator for peer {peer_key[:8]}... on localhost:{local_port} -> {real_host}:{real_port}")
            
            # Modify peer_lines to use localhost:local_port
            modified_peer_lines = []
            for line in peer_lines:
                if line.strip().startswith("Endpoint = "):
                    modified_peer_lines.append(f"Endpoint = 127.0.0.1:{local_port}")
                else:
                    modified_peer_lines.append(line)
            
            return modified_peer_lines
            
        except Exception as e:
            logger.error(f"Failed to start wg-obfuscator for peer {peer_key[:8]}...: {e}", exc_info=True)
            # Return original lines if obfuscation fails
            return peer_lines
    
    def _cleanup_obfuscator_processes(self, mesh_id: str):
        """Stop and clean up obfuscator processes for a mesh"""
        if mesh_id not in self.obfuscator_processes:
            return
        
        for peer_key, process in self.obfuscator_processes[mesh_id].items():
            try:
                if process.poll() is None:  # Process is still running
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    logger.info(f"Stopped wg-obfuscator process for peer {peer_key[:8]}...")
            except Exception as e:
                logger.warning(f"Error stopping obfuscator process for peer {peer_key[:8]}...: {e}")
        
        del self.obfuscator_processes[mesh_id]

