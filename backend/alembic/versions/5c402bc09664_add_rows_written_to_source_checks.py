"""add rows_written to source_checks

Revision ID: 5c402bc09664
Revises: 8d49e66c5095
Create Date: 2026-08-15 21:39:09.809717

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5c402bc09664'
down_revision: Union[str, Sequence[str], None] = '8d49e66c5095'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('source_checks', sa.Column('rows_written', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('source_checks', 'rows_written')
