import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, JSON, Boolean, Integer, ForeignKey
from app.core.database import Base


class Project(Base):
    __tablename__ = "projects"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String(50), default="draft")  # draft, running, completed, failed
    config = Column(JSON, default=dict)  # LLM config, module configs
    dag = Column(JSON, default=dict)  # serialized DAG
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 0.7.0 专家模式：Series 关联
    # - series_id NULL + is_episode False → 老独立项目（simple/advanced）
    # - series_id 非空 + is_episode False → Series 的总项目（master）
    # - series_id 非空 + is_episode True  → 子项目（Episode）
    series_id = Column(String(36), ForeignKey("series.id", ondelete="SET NULL"), nullable=True, index=True)
    is_episode = Column(Boolean, default=False, nullable=False)
    episode_index = Column(Integer, nullable=True)  # 第几集（1-based），便于排序
