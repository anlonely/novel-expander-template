from sqlalchemy import Column, Integer, String, Text, Float, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime


class Novel(Base):
    __tablename__ = "novels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=False)
    original_filename = Column(String(500))
    # 全局角色/设定摘要（跨章节累积，供扩写参考）
    global_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chapters = relationship(
        "Chapter",
        back_populates="novel",
        cascade="all, delete-orphan",
        order_by="Chapter.sort_order",
    )


class Chapter(Base):
    __tablename__ = "chapters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False)
    title = Column(String(500), nullable=False)
    sort_order = Column(Integer, nullable=False)
    original_content = Column(Text, nullable=False)
    expanded_content = Column(Text, nullable=True)
    # 扩写前的备份（用于撤销，每次扩写前保存上一版）
    expanded_content_prev = Column(Text, nullable=True)
    # 章节摘要（用于给后续章节提供上下文，持久化避免重复生成）
    summary = Column(Text, nullable=True)
    # 是否已跳过（无需扩写的章节）
    skipped = Column(Boolean, default=False)
    status = Column(String(50), default="pending")
    # pending, analyzing, expanding, completed, failed, skipped
    progress = Column(Float, default=0.0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    novel = relationship("Novel", back_populates="chapters")


class ExpandTask(Base):
    __tablename__ = "expand_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    novel_id = Column(Integer, ForeignKey("novels.id"), nullable=False)
    status = Column(String(50), default="queued")
    # queued, running, pausing, paused, completed, failed, cancelled, interrupted
    model = Column(String(100), default="grok-4.20-auto")
    mode = Column(String(50), default="one_pass")  # one_pass, detailed
    quality = Column(String(50), default="balanced")  # balanced, nuanced, unleashed
    progress = Column(Float, default=0.0)
    total_chapters = Column(Integer, default=0)
    completed_chapters = Column(Integer, default=0)
    # 失败章节数（独立计数，不与 completed 混淆）
    failed_chapters = Column(Integer, default=0)
    # 跳过的章节数（无需扩写）
    skipped_chapters = Column(Integer, default=0)
    current_chapter_title = Column(String(500), nullable=True)
    chapter_ids_json = Column(Text, nullable=True)
    use_expanded_as_base = Column(Boolean, default=False)
    # 恢复点：最后成功完成的章节在任务列表中的索引
    last_completed_index = Column(Integer, default=-1)
    # 失败的章节 ID 列表（JSON 数组）
    failed_chapter_ids_json = Column(Text, nullable=True)
    # Queue ordering. Larger values run first; created_at remains the real enqueue time.
    queue_priority = Column(Integer, default=0)
    queued_at = Column(DateTime, default=datetime.utcnow)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
