"""
Smite Panel - Central Controller
"""
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models import Tunnel, Node, CoreResetConfig

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers import nodes, tunnels, panel, status, logs, auth, core_health, mesh, overlay
from app.node_server import NodeServer
from app.frp_server import frp_server_manager
from app.node_client import NodeClient
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    await init_db()
    
    h2_server = NodeServer()
    await h2_server.start()
    app.state.h2_server = h2_server
    
    try:
        cert_path = Path(settings.node_cert_path)
        if not cert_path.is_absolute():
            cert_path = Path(os.getcwd()) / cert_path
        
        if not cert_path.exists() or cert_path.stat().st_size == 0:
            logger.info("Generating CA certificate for Iran nodes on startup...")
            h2_server.cert_path = str(cert_path)
            h2_server.key_path = str(cert_path.parent / "ca.key")
            await h2_server._generate_certs(common_name="Smite CA")
            logger.info(f"CA certificate generated at {cert_path}")
    except Exception as e:
        logger.warning(f"Failed to generate CA certificate on startup: {e}")
    
    try:
        server_cert_path = Path(settings.node_server_cert_path)
        if not server_cert_path.is_absolute():
            server_cert_path = Path(os.getcwd()) / server_cert_path
        
        if not server_cert_path.exists() or server_cert_path.stat().st_size == 0:
            logger.info("Generating CA certificate for foreign servers on startup...")
            h2_server.cert_path = str(server_cert_path)
            h2_server.key_path = str(server_cert_path.parent / "ca-server.key")
            await h2_server._generate_certs(common_name="Smite Server CA")
            logger.info(f"Server CA certificate generated at {server_cert_path}")
    except Exception as e:
        logger.warning(f"Failed to generate server CA certificate on startup: {e}")
    
    app.state.frp_server_manager = frp_server_manager
    
    await _restore_node_tunnels()
    
    reset_task = asyncio.create_task(_auto_reset_scheduler(app))
    app.state.reset_task = reset_task
    
    yield
    
    if hasattr(app.state, 'reset_task'):
        app.state.reset_task.cancel()
        try:
            await app.state.reset_task
        except asyncio.CancelledError:
            pass
    
    if hasattr(app.state, 'h2_server'):
        await app.state.h2_server.stop()




async def _restore_frp_servers():
    """Restore FRP servers for active tunnels on startup"""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Tunnel).where(Tunnel.status == "active"))
            tunnels = result.scalars().all()
            
            for tunnel in tunnels:
                if tunnel.core != "frp":
                    continue
                
                bind_port = tunnel.spec.get("bind_port", 7000)
                token = tunnel.spec.get("token")
                
                if not bind_port:
                    continue
                
                try:
                    frp_server_manager.start_server(
                        tunnel_id=tunnel.id,
                        bind_port=int(bind_port),
                        token=token
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to restore FRP server for tunnel %s: %s",
                        tunnel.id,
                        exc,
                    )
    except Exception as exc:
        logger.error("Error restoring FRP servers: %s", exc)


async def _restore_node_tunnels():
    """Sync node-side tunnels with panel database after panel restart
    
    Note: Nodes restore their own tunnels on startup independently.
    This function syncs the panel's view with nodes, but tunnels will
    continue working even if panel is down or this sync fails.
    """
    try:
        logger.info("Starting to sync node-side tunnels with panel database...")
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Tunnel).where(Tunnel.status == "active"))
            tunnels = result.scalars().all()
            
            logger.info(f"Found {len(tunnels)} active tunnels to check for sync")
            
            reverse_tunnels = [t for t in tunnels if t.core == "frp"]
            
            if not reverse_tunnels:
                logger.info("No node-side tunnels to sync")
                return
            
            logger.info(f"Found {len(reverse_tunnels)} active reverse tunnels to sync")
            
            client = NodeClient()
            restored_count = 0
            failed_count = 0
            skipped_count = 0
            
            for tunnel in reverse_tunnels:
                try:
                    iran_node = None
                    foreign_node = None
                    
                    if tunnel.node_id:
                        result = await db.execute(select(Node).where(Node.id == tunnel.node_id))
                        iran_node = result.scalar_one_or_none()
                        if iran_node and iran_node.node_metadata.get("role") != "iran":
                            foreign_node = iran_node
                            iran_node = None
                    
                    if not foreign_node:
                        result = await db.execute(select(Node))
                        all_nodes = result.scalars().all()
                        foreign_nodes = [n for n in all_nodes if n.node_metadata and n.node_metadata.get("role") == "foreign"]
                        if foreign_nodes:
                            foreign_node = foreign_nodes[0]
                    
                    if not iran_node:
                        if tunnel.node_id:
                            result = await db.execute(select(Node).where(Node.id == tunnel.node_id))
                            iran_node = result.scalar_one_or_none()
                        if not iran_node:
                            result = await db.execute(select(Node))
                            all_nodes = result.scalars().all()
                            iran_nodes = [n for n in all_nodes if n.node_metadata and n.node_metadata.get("role") == "iran"]
                            if iran_nodes:
                                iran_node = iran_nodes[0]
                    
                    if not foreign_node or not iran_node:
                        logger.warning(f"Tunnel {tunnel.id}: Missing foreign or iran node, skipping sync (nodes will restore themselves)")
                        skipped_count += 1
                        continue
                    
                    server_spec = tunnel.spec.copy() if tunnel.spec else {}
                    server_spec["mode"] = "server"
                    
                    client_spec = tunnel.spec.copy() if tunnel.spec else {}
                    client_spec["mode"] = "client"
                    
                    # Prepare configs based on tunnel type (same logic as create_tunnel)
                    if tunnel.core == "frp":
                        # Generate unique bind_port to avoid conflicts
                        import hashlib
                        port_hash = int(hashlib.md5(tunnel.id.encode()).hexdigest()[:8], 16)
                        bind_port = server_spec.get("bind_port") or (7000 + (port_hash % 1000))
                        token = server_spec.get("token")
                        server_spec["bind_port"] = bind_port
                        if token:
                            server_spec["token"] = token
                        
                        iran_node_ip = iran_node.node_metadata.get("ip_address")
                        if not iran_node_ip:
                            logger.warning(f"Tunnel {tunnel.id}: Iran node has no IP address, skipping")
                            continue
                        client_spec["server_addr"] = iran_node_ip
                        client_spec["server_port"] = bind_port
                        if token:
                            client_spec["token"] = token
                        tunnel_type = tunnel.type.lower() if tunnel.type else "tcp"
                        if tunnel_type not in ["tcp", "udp"]:
                            tunnel_type = "tcp"  # Default to tcp if invalid
                        client_spec["type"] = tunnel_type
                        local_ip = client_spec.get("local_ip") or iran_node_ip
                        local_port = client_spec.get("local_port") or bind_port
                        client_spec["local_ip"] = local_ip
                        client_spec["local_port"] = local_port
                    else:
                        logger.warning(f"Tunnel {tunnel.id}: Unsupported core type {tunnel.core}, skipping")
                        skipped_count += 1
                        continue
                    
                    # Apply server config to iran node (Iran = SERVER)
                    if not iran_node.node_metadata.get("api_address"):
                        iran_node.node_metadata["api_address"] = f"http://{iran_node.node_metadata.get('ip_address', iran_node.fingerprint)}:{iran_node.node_metadata.get('api_port', 8888)}"
                        await db.commit()
                    
                    logger.info(f"Restoring tunnel {tunnel.id}: applying server config to iran node {iran_node.id}")
                    server_response = await client.send_to_node(
                        node_id=iran_node.id,
                        endpoint="/api/agent/tunnels/apply",
                        data={
                            "tunnel_id": tunnel.id,
                            "core": tunnel.core,
                            "type": tunnel.type,
                            "spec": server_spec
                        }
                    )
                    
                    if server_response.get("status") == "error":
                        error_msg = server_response.get("message", "Unknown error from iran node")
                        logger.error(f"Failed to restore tunnel {tunnel.id} on iran node {iran_node.id}: {error_msg}")
                        continue
                    
                    # Apply client config to foreign node (Foreign = CLIENT)
                    if not foreign_node.node_metadata.get("api_address"):
                        foreign_node.node_metadata["api_address"] = f"http://{foreign_node.node_metadata.get('ip_address', foreign_node.fingerprint)}:{foreign_node.node_metadata.get('api_port', 8888)}"
                        await db.commit()
                    
                    logger.info(f"Restoring tunnel {tunnel.id}: applying client config to foreign node {foreign_node.id}")
                    client_response = await client.send_to_node(
                        node_id=foreign_node.id,
                        endpoint="/api/agent/tunnels/apply",
                        data={
                            "tunnel_id": tunnel.id,
                            "core": tunnel.core,
                            "type": tunnel.type,
                            "spec": client_spec
                        }
                    )
                    
                    if client_response.get("status") == "error":
                        error_msg = client_response.get("message", "Unknown error from foreign node")
                        logger.error(f"Failed to restore tunnel {tunnel.id} on foreign node {foreign_node.id}: {error_msg}")
                        failed_count += 1
                    else:
                        logger.info(f"Successfully restored tunnel {tunnel.id} on both nodes")
                        restored_count += 1
                        
                except Exception as e:
                    logger.error(f"Failed to restore tunnel {tunnel.id}: {e}", exc_info=True)
                    failed_count += 1
            
            logger.info(f"Tunnel sync completed: {restored_count} synced, {failed_count} failed, {skipped_count} skipped out of {len(reverse_tunnels)} total")
            logger.info("Note: Nodes restore their own tunnels on startup, so tunnels work even if panel is down")
                    
    except Exception as e:
        logger.error(f"Error restoring node tunnels: {e}", exc_info=True)


async def _auto_reset_scheduler(app: FastAPI):
    """Background task to auto-reset cores based on timer configuration"""
    from datetime import datetime, timedelta
    from app.routers.core_health import _reset_core
    
    while True:
        try:
            await asyncio.sleep(60)
            
            async with AsyncSessionLocal() as db:
                result = await db.execute(select(CoreResetConfig).where(CoreResetConfig.enabled == True))
                configs = result.scalars().all()
                
                now = datetime.utcnow()
                
                for config in configs:
                    if not config.next_reset:
                        continue
                    
                    if now >= config.next_reset:
                        try:
                            logger.info(f"Auto-resetting {config.core} core (interval: {config.interval_minutes} minutes)")
                            
                            config.last_reset = now
                            config.next_reset = now + timedelta(minutes=config.interval_minutes)
                            await db.commit()
                            await db.refresh(config)  # Ensure config is refreshed after commit
                            
                            await _reset_core(config.core, app, db)
                            
                            logger.info(f"Auto-reset completed for {config.core}, next reset at {config.next_reset}")
                        except Exception as e:
                            logger.error(f"Error in auto-reset for {config.core}: {e}", exc_info=True)
                            await db.rollback()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in auto-reset scheduler: {e}", exc_info=True)
            await asyncio.sleep(60)


app = FastAPI(
    title="Smite Panel",
    description="Tunneling Control Panel",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.docs_enabled else None,
    redoc_url="/redoc" if settings.docs_enabled else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(panel.router, prefix="/api/panel", tags=["panel"])
app.include_router(nodes.router, prefix="/api/nodes", tags=["nodes"])
app.include_router(tunnels.router, prefix="/api/tunnels", tags=["tunnels"])
app.include_router(status.router, prefix="/api/status", tags=["status"])
app.include_router(logs.router, prefix="/api/logs", tags=["logs"])
app.include_router(core_health.router, prefix="/api/core-health", tags=["core-health"])
app.include_router(mesh.router, prefix="/api/mesh", tags=["mesh"])
app.include_router(overlay.router, prefix="/api/overlay", tags=["overlay"])

static_dir = os.path.join(os.path.dirname(__file__), "static")
static_path = Path(static_dir)

if static_path.exists() and (static_path / "index.html").exists():
    app.mount("/static", StaticFiles(directory=static_path), name="static-assets")
    
    from fastapi.responses import FileResponse
    
    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        """Serve frontend for all non-API routes"""
        if full_path.startswith("api/") or full_path.startswith("docs") or full_path.startswith("redoc") or full_path.startswith("openapi.json"):
            raise HTTPException(status_code=404)
        
        file_path = static_path / full_path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        
        index_path = static_path / "index.html"
        if index_path.exists():
            return FileResponse(index_path)
        raise HTTPException(status_code=404)

@app.get("/")
async def root():
    """Root redirect"""
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    index_path = Path(static_dir) / "index.html"
    if index_path.exists():
        from fastapi.responses import FileResponse
        return FileResponse(index_path)
    return {"message": "Smite Panel API", "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    
    if settings.https_enabled:
        import ssl
        cert_path = Path(settings.https_cert_path).resolve()
        key_path = Path(settings.https_key_path).resolve()
        
        if not cert_path.exists() or not key_path.exists():
            logger.warning(f"HTTPS enabled but certificate files not found. Using HTTP.")
            uvicorn.run(app, host=settings.panel_host, port=settings.panel_port)
        else:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(str(cert_path), str(key_path))
            uvicorn.run(
                app,
                host=settings.panel_host,
                port=settings.panel_port,
                ssl_keyfile=str(key_path),
                ssl_certfile=str(cert_path)
            )
    else:
        uvicorn.run(app, host=settings.panel_host, port=settings.panel_port)
