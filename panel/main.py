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
from app.models import Tunnel, Node

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import init_db
from app.routers import nodes, tunnels, panel, status, logs, auth, usage
from app.hysteria2_server import Hysteria2Server
from app.gost_forwarder import gost_forwarder
from app.rathole_server import rathole_server_manager
from app.backhaul_manager import backhaul_manager
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def gost_usage_reporting_task(app: FastAPI):
    """Periodic task to collect and report GOST tunnel usage"""
    usage_tracking = {}  # Track previous usage for each tunnel
    
    while True:
        try:
            await asyncio.sleep(60)  # Report every minute
            
            gost_forwarder = app.state.gost_forwarder
            if not gost_forwarder:
                continue
            
            async with AsyncSessionLocal() as db:
                # Get all active GOST tunnels (GOST tunnels have core="xray" and type in ["tcp", "udp", "ws", "grpc", "tcpmux"])
                result = await db.execute(
                    select(Tunnel).where(
                        Tunnel.status == "active",
                        Tunnel.core == "xray",
                        Tunnel.type.in_(["tcp", "udp", "ws", "grpc", "tcpmux"])
                    )
                )
                tunnels = result.scalars().all()
                
                for tunnel in tunnels:
                    try:
                        usage_mb = gost_forwarder.get_usage_mb(tunnel.id)
                        previous_mb = usage_tracking.get(tunnel.id, 0.0)
                        
                        if usage_mb > previous_mb:
                            incremental_mb = usage_mb - previous_mb
                            usage_tracking[tunnel.id] = usage_mb
                            
                            incremental_bytes = int(incremental_mb * 1024 * 1024)
                            
                            if incremental_bytes > 0:
                                logger.debug(f"Reporting GOST usage for tunnel {tunnel.id}: {incremental_mb:.2f} MB (total: {usage_mb:.2f} MB)")
                                # Update tunnel usage in database
                                tunnel.used_mb = (tunnel.used_mb or 0.0) + incremental_mb
                                
                                # Create usage record
                                from app.models import Usage
                                usage_record = Usage(
                                    tunnel_id=tunnel.id,
                                    node_id=None,  # GOST tunnels don't have a node
                                    bytes_used=incremental_bytes
                                )
                                db.add(usage_record)
                                
                                # Check quota
                                if tunnel.quota_mb > 0 and tunnel.used_mb >= tunnel.quota_mb:
                                    tunnel.status = "error"
                                    logger.warning(f"GOST tunnel {tunnel.id} quota exceeded: {tunnel.used_mb:.2f} MB >= {tunnel.quota_mb:.2f} MB")
                                
                                await db.commit()
                                await db.refresh(tunnel)
                        elif previous_mb == 0.0 and usage_mb > 0:
                            # First report - send initial usage
                            usage_tracking[tunnel.id] = usage_mb
                            initial_bytes = int(usage_mb * 1024 * 1024)
                            if initial_bytes > 0:
                                logger.debug(f"Reporting initial GOST usage for tunnel {tunnel.id}: {usage_mb:.2f} MB")
                                tunnel.used_mb = (tunnel.used_mb or 0.0) + usage_mb
                                
                                from app.models import Usage
                                usage_record = Usage(
                                    tunnel_id=tunnel.id,
                                    node_id=None,
                                    bytes_used=initial_bytes
                                )
                                db.add(usage_record)
                                await db.commit()
                                await db.refresh(tunnel)
                    except Exception as e:
                        logger.warning(f"Failed to report GOST usage for tunnel {tunnel.id}: {e}", exc_info=True)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in GOST usage reporting task: {e}", exc_info=True)
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    await init_db()
    
    h2_server = Hysteria2Server()
    await h2_server.start()
    app.state.h2_server = h2_server
    
    try:
        cert_path = Path(settings.hysteria2_cert_path)
        if not cert_path.is_absolute():
            cert_path = Path(os.getcwd()) / cert_path
        
        if not cert_path.exists() or cert_path.stat().st_size == 0:
            logger.info("Generating CA certificate on startup...")
            h2_server.cert_path = str(cert_path)
            h2_server.key_path = str(cert_path.parent / "ca.key")
            await h2_server._generate_certs()
            logger.info(f"CA certificate generated at {cert_path}")
    except Exception as e:
        logger.warning(f"Failed to generate CA certificate on startup: {e}")
    
    app.state.gost_forwarder = gost_forwarder
    
    app.state.rathole_server_manager = rathole_server_manager
    app.state.backhaul_manager = backhaul_manager
    
    await _restore_forwards()
    
    await _restore_rathole_servers()
    await _restore_backhaul_servers()
    
    # Start GOST usage reporting task
    gost_usage_task = asyncio.create_task(gost_usage_reporting_task(app))
    app.state.gost_usage_task = gost_usage_task
    
    yield
    
    # Cancel GOST usage reporting task
    if hasattr(app.state, 'gost_usage_task'):
        app.state.gost_usage_task.cancel()
        try:
            await app.state.gost_usage_task
        except asyncio.CancelledError:
            pass
    
    if hasattr(app.state, 'h2_server'):
        await app.state.h2_server.stop()
    
    gost_forwarder.cleanup_all()
    
    rathole_server_manager.cleanup_all()
    backhaul_manager.cleanup_all()


async def _restore_forwards():
    """Restore forwarding for active tunnels on startup"""
    try:
        logger.info("Starting to restore forwarding for active tunnels...")
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Tunnel).where(Tunnel.status == "active"))
            tunnels = result.scalars().all()
            logger.info(f"Found {len(tunnels)} active tunnels to restore")
            
            for tunnel in tunnels:
                logger.info(f"Checking tunnel {tunnel.id}: type={tunnel.type}, core={tunnel.core}")
                needs_gost_forwarding = tunnel.type in ["tcp", "udp", "ws", "grpc", "tcpmux"] and tunnel.core == "xray"
                if not needs_gost_forwarding:
                    continue
                
                listen_port = tunnel.spec.get("listen_port")
                forward_to = tunnel.spec.get("forward_to")
                
                if not forward_to:
                    remote_ip = tunnel.spec.get("remote_ip", "127.0.0.1")
                    remote_port = tunnel.spec.get("remote_port", 8080)
                    forward_to = f"{remote_ip}:{remote_port}"
                
                panel_port = listen_port or tunnel.spec.get("remote_port")
                if not panel_port or not forward_to:
                    logger.warning(f"Tunnel {tunnel.id}: Missing panel_port or forward_to, skipping restore")
                    continue
                
                try:
                    logger.info(f"Restoring gost forwarding for tunnel {tunnel.id}: {tunnel.type}://:{panel_port} -> {forward_to}")
                    gost_forwarder.start_forward(
                        tunnel_id=tunnel.id,
                        local_port=int(panel_port),
                        forward_to=forward_to,
                        tunnel_type=tunnel.type
                    )
                    logger.info(f"Successfully restored gost forwarding for tunnel {tunnel.id}")
                except Exception as e:
                    logger.error(f"Failed to restore forwarding for tunnel {tunnel.id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Error restoring forwards: {e}")


async def _restore_rathole_servers():
    """Restore Rathole servers for active tunnels on startup"""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Tunnel).where(Tunnel.status == "active"))
            tunnels = result.scalars().all()
            
            for tunnel in tunnels:
                if tunnel.core != "rathole":
                    continue
                
                remote_addr = tunnel.spec.get("remote_addr")
                token = tunnel.spec.get("token")
                proxy_port = tunnel.spec.get("remote_port") or tunnel.spec.get("listen_port")
                
                if not remote_addr or not token or not proxy_port:
                    continue
                
                rathole_server_manager.start_server(
                    tunnel_id=tunnel.id,
                    remote_addr=remote_addr,
                    token=token,
                    proxy_port=int(proxy_port)
                )
    except Exception as e:
        logger.error(f"Error restoring Rathole servers: {e}")


async def _restore_backhaul_servers():
    """Restore Backhaul servers for active tunnels on startup"""
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Tunnel).where(Tunnel.status == "active"))
            tunnels = result.scalars().all()

            for tunnel in tunnels:
                if tunnel.core != "backhaul":
                    continue

                try:
                    backhaul_manager.start_server(tunnel.id, tunnel.spec or {})
                except Exception as exc:
                    logger.error(
                        "Failed to restore Backhaul server for tunnel %s: %s",
                        tunnel.id,
                        exc,
                    )
    except Exception as exc:
        logger.error("Error restoring Backhaul servers: %s", exc)


app = FastAPI(
    title="Smite Panel",
    description="Tunneling Control Panel",
    version="1.0.0",
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
app.include_router(usage.router, prefix="/api/usage", tags=["usage"])

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
