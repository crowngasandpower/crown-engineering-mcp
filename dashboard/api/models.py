"""SQLAlchemy models for the dashboard API."""

from sqlalchemy import Column, ForeignKey, Index, Integer, Text

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(Text, nullable=False, unique=True)
    display_name = Column(Text, nullable=False, default="")
    email = Column(Text, nullable=False, default="")
    is_engineer = Column(Integer, nullable=False, default=0)
    is_flag_admin = Column(Integer, nullable=False, default=0)
    is_deploy_admin = Column(Integer, nullable=False, default=0)
    is_admin = Column(Integer, nullable=False, default=0)
    first_login = Column(Text, nullable=False)
    last_login = Column(Text, nullable=False)


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Text, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(Text, nullable=False)
    expires_at = Column(Text, nullable=False)
    ip_address = Column(Text, nullable=False, default="")

    __table_args__ = (Index("idx_sessions_expires", "expires_at"),)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(Text, nullable=False)
    username = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    target = Column(Text, nullable=False)
    detail = Column(Text, nullable=False, default="")
    ip = Column(Text, nullable=False, default="")

    __table_args__ = (
        Index("idx_audit_timestamp", "timestamp"),
        Index("idx_audit_username", "username"),
    )
