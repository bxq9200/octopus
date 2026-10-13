import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, JSON, ForeignKey
from app.core.database import Base


class WorkflowNode(Base):
    __tablename__ = "workflow_nodes"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    project_id = Column(String(36), ForeignKey("projects.id"), nullable=False)
    type = Column(String(100), nullable=False)  # text_generation, image_generation, video_generation, merge
    label = Column(String(255), nullable=False)
    position = Column(JSON, default=dict)  # {x, y}
    params = Column(JSON, default=dict)  # module input params
    inputs = Column(JSON, default=list)  # connected input node ids
    outputs = Column(JSON, default=list)  # connected output node ids
    status = Column(String(50), default="pending")  # pending, running, done, error, skipped
    result = Column(JSON, nullable=True)  # execution result
    error_msg = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
