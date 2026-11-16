"""Keep server-fetched page snapshots separate from client captures."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "entry_revisions",
        sa.Column("server_extract", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("entry_revisions", "server_extract")
