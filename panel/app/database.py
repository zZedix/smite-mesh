"""Database setup and session management"""
import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import settings

Base = declarative_base()

if settings.db_type == "sqlite":
    db_url = f"sqlite+aiosqlite:///{settings.db_path}"
else:
    raise ValueError(f"Unsupported DB type: {settings.db_type}")

engine = create_async_engine(db_url, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """Initialize database tables"""
    if settings.db_type == "sqlite":
        os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """Database session dependency"""
    async with AsyncSessionLocal() as session:
        yield session

