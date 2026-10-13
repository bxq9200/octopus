"""Series 模型（0.7.0 专家模式）

漫剧分集制作：一个 Series 含 1 个总项目 + N 个 Episode 子项目。
- 总项目（is_episode=False）：剧本通读 + 分集协商 + 视觉开发 + 筹备
- Episode 子项目（is_episode=True）：每集独立跑剧本/分镜/视频/合成

资产共享策略（0.7.0）：首次拷贝 + 同步开关
- 总项目 art 全 confirmed 后冻结到 series.shared_assets
- 创建 Episode 时深拷贝 shared_assets 到 Episode 的 art 节点
- 改总项目时弹"是否同步到未确认子项目"对话框
"""
import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Text, JSON
from app.core.database import Base


class Series(Base):
    __tablename__ = "series"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # draft / breakdown / visual_dev / producing / done
    status = Column(String(50), default="draft", nullable=False)

    # 总项目 ID — 视觉开发与协商发生地（创建时回填，避免循环外键）
    master_project_id = Column(String(36), nullable=True)

    # 用户上传剧本原文（≤ 5 MB），director.parse_script_file 的输入
    script_raw = Column(Text, nullable=True)

    # director.parse_script_file 产物：{logline, genre, characters, scene_outline, est_total_min, ...}
    script_summary = Column(JSON, default=dict)

    # 分集结构（多轮协商后定）：[{ep_id, title, story_arc, scene_range, est_duration_min}]
    episodes_breakdown = Column(JSON, default=list)

    # 总项目 art 全 confirmed 后冻结的共享资产：
    # {characters[], scenes[], props[], art_main_result, frozen_at}
    # Episode 创建时从这里深拷贝
    shared_assets = Column(JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
