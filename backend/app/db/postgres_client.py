from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from app.core.config import settings

# Create synchronous SQLAlchemy engine with pre-ping connection check
engine = create_engine(
    settings.database_url_sync,
    pool_pre_ping=True,
)

# Session factory for generating database sessions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency yielding a SQLAlchemy database Session per request.
    Closes session automatically upon request completion.

    # TODO: Add passlib[bcrypt] or argon2-cffi when auth is implemented.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
