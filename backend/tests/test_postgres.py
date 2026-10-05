import sys
import uuid
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from app.db.postgres_client import SessionLocal
from app.models.db_models import User, Session, Task, ChatHistory, KTGenerationRecord


def test_postgres_connection_and_user_crud():
    """
    Integration test verifying PostgreSQL connection, UUID primary keys,
    timezone-aware timestamps across models, and clean row cleanup.
    """
    db = SessionLocal()
    initial_user_count = db.query(User).count()
    test_email = f"test_user_{uuid.uuid4().hex[:8]}@example.com"
    test_user_id = None

    try:
        # 1. Insert test user
        user = User(
            email=test_email,
            hashed_password="dummy_hashed_password_for_testing",
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        test_user_id = user.id
        print(f"Successfully inserted User ID: {user.id}, Email: {user.email}")

        # 2. Query user back and verify fields
        queried_user = db.query(User).filter(User.email == test_email).first()
        assert queried_user is not None, "User should be retrievable by email"
        assert queried_user.id == test_user_id, "User ID should match inserted UUID"
        assert queried_user.email == test_email, "User email should match"
        assert queried_user.created_at is not None, "created_at should be populated"
        assert queried_user.created_at.tzinfo is not None, "created_at must be timezone-aware"

        print("PostgreSQL integration test passed successfully!")

    finally:
        # 3. Explicit cleanup: delete test user row
        if test_user_id:
            db.query(User).filter(User.id == test_user_id).delete()
            db.commit()

        # 4. Verify users table count is unchanged
        final_user_count = db.query(User).count()
        assert final_user_count == initial_user_count, (
            f"Row count mismatch: initial={initial_user_count}, final={final_user_count}"
        )
        db.close()


def test_verify_all_models_timezone_aware():
    """
    Verify every DateTime column in all 5 models is timezone-aware in SQLAlchemy metadata.
    """
    models = [User, Session, Task, ChatHistory, KTGenerationRecord]
    for model in models:
        for column in model.__table__.columns:
            if hasattr(column.type, "timezone"):
                assert column.type.timezone is True, (
                    f"{model.__name__}.{column.name} must be DateTime(timezone=True)"
                )


if __name__ == "__main__":
    test_verify_all_models_timezone_aware()
    test_postgres_connection_and_user_crud()
