"""Hysteria2 client for node to connect to panel"""
import asyncio
import httpx
import hashlib
import socket
import logging
from pathlib import Path
from typing import Optional
from app.config import settings

logger = logging.getLogger(__name__)


class Hysteria2Client:
    """Client connecting to panel via HTTPS"""
    
    def __init__(self):
        self.panel_address = settings.panel_address
        self.ca_path = Path(settings.panel_ca_path)
        self.client = None
        self.node_id = None
        self.fingerprint = None
        self.registered = False
    
    async def start(self):
        """Start client and connect to panel"""
        if not self.ca_path.exists():
            raise FileNotFoundError(f"CA certificate not found at {self.ca_path}")
        
        await self._generate_fingerprint()
        
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            verify=False
        )
        
        print(f"Node client ready, panel address: {self.panel_address}")
    
    async def stop(self):
        """Stop client"""
        if self.client:
            await self.client.aclose()
            self.client = None
    
    async def register_with_panel(self):
        """Auto-register with panel"""
        if not self.client:
            await self.start()
        
        if "://" in self.panel_address:
            protocol, rest = self.panel_address.split("://", 1)
            if ":" in rest:
                panel_host, panel_hysteria_port = rest.split(":", 1)
            else:
                panel_host = rest
                panel_hysteria_port = "443"
        else:
            protocol = "http"
            if ":" in self.panel_address:
                panel_host, panel_hysteria_port = self.panel_address.split(":", 1)
            else:
                panel_host = self.panel_address
                panel_hysteria_port = "443"
        
        panel_api_port = 8000
        
        panel_api_url = f"http://{panel_host}:{panel_api_port}"
        
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            node_ip = s.getsockname()[0]
            s.close()
        except:
            node_ip = "0.0.0.0"
        
        registration_data = {
            "name": settings.node_name,
            "ip_address": node_ip,
            "api_port": settings.node_api_port,
            "fingerprint": self.fingerprint,
            "metadata": {
                "api_address": f"http://{node_ip}:{settings.node_api_port}",
                "node_name": settings.node_name,
                "panel_address": self.panel_address
            }
        }
        
        try:
            url = f"{panel_api_url}/api/nodes"
            print(f"Registering with panel at {url}...")
            response = await self.client.post(url, json=registration_data, timeout=10.0)
            
            if response.status_code in [200, 201]:
                data = response.json()
                self.node_id = data.get("id")
                self.registered = True
                logger.info(f"Node registered successfully with ID: {self.node_id}")
                return True
            else:
                logger.error(f"Registration failed: {response.status_code} - {response.text}")
                return False
        except httpx.ConnectError as e:
            logger.error(f"Cannot connect to panel at {panel_api_url}: {str(e)}. Make sure panel is running and accessible")
            return False
        except Exception as e:
            logger.error(f"Registration error: {str(e)}")
            return False
    
    async def _generate_fingerprint(self):
        """Generate node fingerprint for identification"""
        import socket
        hostname = socket.gethostname()
        fingerprint_data = f"{hostname}-{settings.node_name}".encode()
        self.fingerprint = hashlib.sha256(fingerprint_data).hexdigest()[:16]
        print(f"Node fingerprint: {self.fingerprint}")
    
