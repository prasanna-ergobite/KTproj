"""add_task_result_column

Revision ID: 0002_add_task_result_column
Revises: 0001_initial_postgres_tables
Create Date: 2026-08-05 18:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0002_add_task_result_column'
down_revision: Union[str, None] = '0001_initial_postgres_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('tasks', 'result')
