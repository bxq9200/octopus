from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.core.config import settings

Base = declarative_base()

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    future=True,
)

async_session = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # 0.7.0：老 DB 加新列（metadata.create_all 不会改已有表）
        await conn.run_sync(_ensure_project_columns_v070)


def _ensure_project_columns_v070(sync_conn):
    """0.7.0：给 projects 表补 series_id / is_episode / episode_index 三列。
    SQLite 支持 ALTER TABLE ADD COLUMN。"""
    from sqlalchemy import text
    try:
        existing = {row[1] for row in sync_conn.exec_driver_sql("PRAGMA table_info(projects)").fetchall()}
    except Exception:
        return  # 不是 sqlite 或表不存在，跳过
    additions = []
    if "series_id" not in existing:
        additions.append("ALTER TABLE projects ADD COLUMN series_id VARCHAR(36)")
    if "is_episode" not in existing:
        additions.append("ALTER TABLE projects ADD COLUMN is_episode BOOLEAN DEFAULT 0 NOT NULL")
    if "episode_index" not in existing:
        additions.append("ALTER TABLE projects ADD COLUMN episode_index INTEGER")
    for sql in additions:
        try:
            sync_conn.exec_driver_sql(sql)
        except Exception as e:
            # 已有列 / 其他无害错误：log 一下继续
            import logging
            logging.getLogger(__name__).warning("ALTER 失败（可能已存在）: %s — %s", sql, e)
    # 兜底：把 NULL 的 is_episode 行设 0（老项目数据）
    try:
        sync_conn.exec_driver_sql("UPDATE projects SET is_episode = 0 WHERE is_episode IS NULL")
    except Exception:
        pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
