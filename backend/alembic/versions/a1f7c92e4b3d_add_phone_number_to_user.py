"""add phone number to user

Revision ID: a1f7c92e4b3d
Revises: 3debc2b55899
Create Date: 2026-08-12 09:40:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a1f7c92e4b3d"
down_revision = "3debc2b55899"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user",
        sa.Column("phone_number", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user", "phone_number")
