"""
Smite Node - Lightweight Agent
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.routers import agent
from app.panel_client import PanelClient
from app.core_adapters import AdapterManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _enable_ip_forwarding():
    """Enable IPv4 forwarding at startup"""
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "w") as f:
            f.write("1")
        logger.info("IPv4 forwarding enabled at startup")
    except Exception as e:
        logger.warning(f"Failed to enable IPv4 forwarding at startup: {e}")
        logger.warning("IP forwarding may need to be enabled on the host system")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    # Enable IP forwarding at startup (required for mesh networking)
    _enable_ip_forwarding()
    
    h2_client = PanelClient()
    try:
        await h2_client.start()
        app.state.h2_client = h2_client
        
        try:
            await h2_client.register_with_panel()
        except Exception as e:
            logger.warning(f"Could not register with panel: {e}")
            logger.warning("Node will continue running but manual registration may be needed")
    except Exception as e:
        logger.error(f"Failed to start Panel client: {e}")
        logger.error("Node API will still be available, but panel connection will not work")
        logger.error("Make sure CA certificate is available at the configured path")
        app.state.h2_client = None
    
    adapter_manager = AdapterManager()
    app.state.adapter_manager = adapter_manager
    
    try:
        await adapter_manager.restore_tunnels()
    except Exception as e:
        logger.error(f"Failed to restore tunnels on startup: {e}", exc_info=True)
    
    yield
    if hasattr(app.state, 'h2_client') and app.state.h2_client:
        try:
            await app.state.h2_client.stop()
        except:
            pass
    if hasattr(app.state, 'adapter_manager'):
        await app.state.adapter_manager.cleanup()


app = FastAPI(
    title="Smite Node",
    description="Lightweight Tunnel Agent",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(agent.router, prefix="/api/agent", tags=["agent"])


@app.get("/")
async def root():
    return {"status": "ok", "service": "smite-node"}


if __name__ == "__main__":
    import uvicorn
    try:
        uvicorn.run(app, host="0.0.0.0", port=settings.node_api_port)
    except Exception as e:
        logger.error(f"Failed to start server: {e}", exc_info=True)
        raise

