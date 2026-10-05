"""
Application Constants and System Sentinel Definitions for AutoKT.
"""

import logging
from uuid import UUID
from sqlalchemy.orm import Session
from app.models.db_models import User

logger = logging.getLogger("autokt.constants")

# Fixed System User Sentinel UUID for background tasks and unauthenticated operations
SYSTEM_USER_ID = UUID("00000000-0000-0000-0000-000000000001")
SYSTEM_USER_EMAIL = "system@autokt.internal"


def ensure_system_user(db: Session) -> User:
    """
    Idempotently seed the system user sentinel in PostgreSQL.
    Used as foreign key owner for background/system-level ingestion tasks.
    """
    user = db.query(User).filter(User.id == SYSTEM_USER_ID).first()
    if not user:
        logger.info("Seeding system user sentinel (ID: %s)...", SYSTEM_USER_ID)
        user = User(
            id=SYSTEM_USER_ID,
            email=SYSTEM_USER_EMAIL,
            # SENTINEL — not a real credential. Never used for login. Replace when auth is wired up.
            hashed_password="SYSTEM_SENTINEL_NOT_FOR_LOGIN",
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user
