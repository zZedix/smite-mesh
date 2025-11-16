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
from app.hysteria2_client import Hysteria2Client
from app.core_adapters import AdapterManager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    h2_client = Hysteria2Client()
    try:
        await h2_client.start()
        app.state.h2_client = h2_client
        
        try:
            await h2_client.register_with_panel()
        except Exception as e:
            logger.warning(f"Could not register with panel: {e}")
            logger.warning("Node will continue running but manual registration may be needed")
    except Exception as e:
        logger.error(f"Failed to start Hysteria2 client: {e}")
        logger.error("Node API will still be available, but panel connection will not work")
        logger.error("Make sure CA certificate is available at the configured path")
        app.state.h2_client = None
    
    adapter_manager = AdapterManager()
    app.state.adapter_manager = adapter_manager
    
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
        uvicorn.run(app, host="0.0.0.0", port=8888)
    except Exception as e:
        logger.error(f"Failed to start server: {e}", exc_info=True)
        raise

