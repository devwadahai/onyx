"""add write_agent_action_log

Revision ID: 8a42e399da8d
Revises: 77970041a87b
Create Date: 2026-08-24 00:00:00.000000

Audit trail for the Write-Capable Security Agent's admin-gated actions
(currently just action_confirm) -- see
spec/write-capable-security-agent-admin-gating.md in unifi-mcp-secure.
Records both successful executions and rejected non-admin attempts.
"""

from alembic import op
import sqlalchemy as sa

from onyx.db.enums import WriteAgentActionOutcome

# revision identifiers, used by Alembic.
revision = "8a42e399da8d"
down_revision = "77970041a87b"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "write_agent_action_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("user_email", sa.String(), nullable=True),
        sa.Column("tool_name", sa.String(), nullable=False),
        sa.Column("mcp_server_id", sa.Integer(), nullable=True),
        sa.Column("mcp_server_name", sa.String(), nullable=True),
        sa.Column(
            "outcome",
            sa.Enum(WriteAgentActionOutcome, native_enum=False),
            nullable=False,
        ),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["mcp_server_id"], ["mcp_server.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        op.f("ix_write_agent_action_log_user_id"),
        "write_agent_action_log",
        ["user_id"],
    )
    op.create_index(
        op.f("ix_write_agent_action_log_outcome"),
        "write_agent_action_log",
        ["outcome"],
    )
    op.create_index(
        op.f("ix_write_agent_action_log_created_at"),
        "write_agent_action_log",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("write_agent_action_log")
