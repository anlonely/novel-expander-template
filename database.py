from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
import config
import os
import logging

logger = logging.getLogger(__name__)

os.makedirs(config.DATA_DIR, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{config.DB_PATH}"
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


async def init_db():
    """创建所有表（仅创建不存在的表）"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def migrate_db():
    """为已有表添加新增的列（安全迁移，列已存在则跳过）"""
    migrations = [
        # Novel 表新增列
        ("novels", "global_summary", "TEXT"),
        # Chapter 表新增列
        ("chapters", "expanded_content_prev", "TEXT"),
        ("chapters", "summary", "TEXT"),
        ("chapters", "skipped", "BOOLEAN DEFAULT 0"),
        # ExpandTask 表新增列
        ("expand_tasks", "failed_chapters", "INTEGER DEFAULT 0"),
        ("expand_tasks", "skipped_chapters", "INTEGER DEFAULT 0"),
        ("expand_tasks", "last_completed_index", "INTEGER DEFAULT -1"),
        ("expand_tasks", "failed_chapter_ids_json", "TEXT"),
        ("expand_tasks", "quality", "TEXT DEFAULT 'balanced'"),
        ("expand_tasks", "queue_priority", "INTEGER DEFAULT 0"),
        ("expand_tasks", "queued_at", "DATETIME"),
    ]

    async with engine.begin() as conn:
        for table, col_name, col_type in migrations:
            try:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")
                )
                logger.info(f"Migration: added {table}.{col_name}")
            except Exception:
                pass  # 列已存在，安全跳过


async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
