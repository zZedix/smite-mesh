"""WireGuard Mesh Manager - Handles mesh creation, key generation, and configuration"""
import logging
import subprocess
import ipaddress
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path

logger = logging.getLogger(__name__)


class WireGuardMeshManager:
    """Manages WireGuard mesh networks over Smite Backhaul"""
    
    def __init__(self):
        self._wg_binary = None
    
    def _resolve_wg_binary(self) -> Path:
        """Resolve WireGuard binary path (lazy loading)"""
        if self._wg_binary is not None:
            return self._wg_binary
        
        import shutil
        wg_path = shutil.which("wg")
        if wg_path:
            self._wg_binary = Path(wg_path)
            return self._wg_binary
        for path in [Path("/usr/bin/wg"), Path("/usr/local/bin/wg")]:
            if path.exists():
                self._wg_binary = path
                return self._wg_binary
        raise FileNotFoundError("WireGuard 'wg' binary not found. Install wireguard-tools package.")
    
    def generate_keypair(self) -> Tuple[str, str]:
        """Generate WireGuard private/public key pair"""
        wg_binary = self._resolve_wg_binary()
        try:
            private_key_proc = subprocess.run(
                [str(wg_binary), "genkey"],
                capture_output=True,
                text=True,
                check=True
            )
            private_key = private_key_proc.stdout.strip()
            
            public_key_proc = subprocess.run(
                [str(wg_binary), "pubkey"],
                input=private_key,
                capture_output=True,
                text=True,
                check=True
            )
            public_key = public_key_proc.stdout.strip()
            
            return private_key, public_key
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to generate WireGuard keypair: {e}")
            raise RuntimeError(f"WireGuard key generation failed: {e.stderr}")
    
    def create_mesh_config(
        self,
        mesh_id: str,
        nodes: List[Dict[str, Any]],
        overlay_subnet: str,
        topology: str = "full-mesh",
        mtu: int = 1280
    ) -> Dict[str, Any]:
        """
        Create mesh configuration for all nodes using IPAM-assigned overlay IPs
        
        Args:
            mesh_id: Unique mesh identifier
            nodes: List of node dicts with 'node_id', 'name', 'lan_subnet', 'overlay_ip'
            overlay_subnet: WireGuard overlay subnet (e.g., "10.250.0.0/24")
            topology: "full-mesh" or "hub-spoke"
            mtu: MTU for WireGuard interface
        
        Returns:
            Dict mapping node_id to its WireGuard configuration
        """
        try:
            overlay_net = ipaddress.ip_network(overlay_subnet, strict=False)
        except ValueError as e:
            raise ValueError(f"Invalid overlay subnet: {overlay_subnet}")
        
        node_configs = {}
        node_keys = {}
        node_ips = {}
        
        for node in nodes:
            node_id = node["node_id"]
            private_key, public_key = self.generate_keypair()
            node_keys[node_id] = {
                "private_key": private_key,
                "public_key": public_key
            }
            
            overlay_ip = node.get("overlay_ip")
            if not overlay_ip:
                raise ValueError(f"Node {node_id} missing overlay_ip from IPAM")
            
            try:
                ip_obj = ipaddress.ip_address(overlay_ip)
                if ip_obj not in overlay_net:
                    raise ValueError(f"Node {node_id} overlay IP {overlay_ip} not in subnet {overlay_subnet}")
            except ValueError as e:
                raise ValueError(f"Invalid overlay IP for node {node_id}: {e}")
            
            node_ips[node_id] = overlay_ip
        
        mesh_config = {
            "mesh_id": mesh_id,
            "overlay_subnet": overlay_subnet,
            "topology": topology,
            "mtu": mtu,
            "nodes": {}
        }
        
        for node in nodes:
            node_id = node["node_id"]
            node_name = node.get("name", node_id)
            lan_subnet = node.get("lan_subnet", "")
            
            peers = []
            if topology == "full-mesh":
                for peer_node in nodes:
                    if peer_node["node_id"] == node_id:
                        continue
                    peer_id = peer_node["node_id"]
                    peer_lan_subnet = peer_node.get("lan_subnet", "")
                    
                    peer_config = {
                        "node_id": peer_id,
                        "public_key": node_keys[peer_id]["public_key"],
                        "overlay_ip": node_ips[peer_id],
                        "lan_subnet": peer_lan_subnet
                    }
                    peers.append(peer_config)
            elif topology == "hub-spoke":
                hub_node_id = nodes[0]["node_id"]
                if node_id == hub_node_id:
                    for peer_node in nodes[1:]:
                        peer_id = peer_node["node_id"]
                        peer_lan_subnet = peer_node.get("lan_subnet", "")
                        peer_config = {
                            "node_id": peer_id,
                            "public_key": node_keys[peer_id]["public_key"],
                            "overlay_ip": node_ips[peer_id],
                            "lan_subnet": peer_lan_subnet
                        }
                        peers.append(peer_config)
                else:
                    peer_lan_subnet = nodes[0].get("lan_subnet", "")
                    peer_config = {
                        "node_id": hub_node_id,
                        "public_key": node_keys[hub_node_id]["public_key"],
                        "overlay_ip": node_ips[hub_node_id],
                        "lan_subnet": peer_lan_subnet
                    }
                    peers.append(peer_config)
            
            node_config = {
                "node_id": node_id,
                "node_name": node_name,
                "private_key": node_keys[node_id]["private_key"],
                "public_key": node_keys[node_id]["public_key"],
                "overlay_ip": node_ips[node_id],
                "lan_subnet": lan_subnet,
                "peers": peers,
                "mtu": mtu
            }
            
            mesh_config["nodes"][node_id] = node_config
            node_configs[node_id] = node_config
        
        return node_configs
    
    def generate_wireguard_config(
        self,
        node_config: Dict[str, Any],
        backhaul_endpoints: Dict[str, Any]
    ) -> str:
        """
        Generate WireGuard configuration file content for a node using IPAM-assigned overlay IP
        
        Args:
            node_config: Node configuration from create_mesh_config (includes overlay_ip from IPAM)
            backhaul_endpoints: Dict mapping peer node_id to endpoint(s)
                - If string: single endpoint (IP:port)
                - If dict: {"tcp": "IP:port", "udp": "IP:port"} for dual transport
        
        Returns:
            WireGuard config file content
        """
        overlay_ip = node_config.get('overlay_ip')
        if not overlay_ip:
            raise ValueError("Node config missing overlay_ip from IPAM")
        
        lines = ["[Interface]"]
        lines.append(f"PrivateKey = {node_config['private_key']}")
        lines.append(f"Address = {overlay_ip}/32")
        lines.append(f"MTU = {node_config['mtu']}")
        lines.append("")
        
        for peer in node_config["peers"]:
            peer_id = peer["node_id"]
            endpoints = backhaul_endpoints.get(peer_id)
            if not endpoints:
                logger.warning(f"No backhaul endpoint for peer {peer_id}, skipping")
                continue
            
            allowed_ips = [f"{peer['overlay_ip']}/32"]
            if peer.get("lan_subnet"):
                allowed_ips.append(peer["lan_subnet"])
            allowed_ips_str = ', '.join(allowed_ips)
            
            if isinstance(endpoints, dict) and "udp" in endpoints and "tcp" in endpoints:
                # When both UDP and TCP are available, use UDP (preferred for WireGuard)
                # WireGuard doesn't support duplicate peers with the same public key,
                # so we only create one [Peer] block with the UDP endpoint
                endpoint = endpoints['udp']
                logger.info(f"Both UDP and TCP endpoints available for peer {peer_id}, using UDP: {endpoint}")
                lines.append("[Peer]")
                lines.append(f"PublicKey = {peer['public_key']}")
                lines.append(f"AllowedIPs = {allowed_ips_str}")
                lines.append(f"Endpoint = {endpoint}")
                lines.append("PersistentKeepalive = 25")
                lines.append("")
            else:
                endpoint = endpoints.get("udp") if isinstance(endpoints, dict) else endpoints
                if not endpoint and isinstance(endpoints, dict):
                    endpoint = endpoints.get("tcp")
                
                if endpoint:
                    lines.append("[Peer]")
                    lines.append(f"PublicKey = {peer['public_key']}")
                    lines.append(f"AllowedIPs = {allowed_ips_str}")
                    lines.append(f"Endpoint = {endpoint}")
                    lines.append("PersistentKeepalive = 25")
                    lines.append("")
        
        return "\n".join(lines)
    
    def get_peer_routes(self, node_config: Dict[str, Any]) -> List[str]:
        """Get routing commands for remote LAN subnets"""
        routes = []
        for peer in node_config["peers"]:
            if peer.get("lan_subnet"):
                routes.append(peer["lan_subnet"])
        return routes


wireguard_mesh_manager = WireGuardMeshManager()

