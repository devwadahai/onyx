"""Admin-facing read view over write_agent_action_log (spec/write-capable-
security-agent-requirements.md #4 in unifi-mcp-secure: "his actions will be
logged"). The log itself is written from MCPTool.run() in
onyx/tools/tool_implementations/mcp/mcp_tool.py -- this router only reads
it back for the admin UI.
"""

import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission, WriteAgentActionOutcome
from onyx.db.models import User, WriteAgentActionLog

admin_router = APIRouter(prefix="/admin/write-agent-action-log")

# Simple recency cap, not pagination -- this is an audit-trail read view for
# an admin to eyeball, not a high-volume log browser. Revisit if this
# feature's action volume ever makes 200 rows too few to be useful.
_MAX_ROWS = 200


class WriteAgentActionLogEntry(BaseModel):
    id: int
    user_email: str | None
    tool_name: str
    mcp_server_name: str | None
    outcome: WriteAgentActionOutcome
    detail: str | None
    created_at: datetime.datetime


class WriteAgentActionLogResponse(BaseModel):
    entries: list[WriteAgentActionLogEntry]


@admin_router.get("")
def get_write_agent_action_log(
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
    db_session: Session = Depends(get_session),
) -> WriteAgentActionLogResponse:
    rows = (
        db_session.query(WriteAgentActionLog)
        .order_by(desc(WriteAgentActionLog.created_at))
        .limit(_MAX_ROWS)
        .all()
    )
    return WriteAgentActionLogResponse(
        entries=[
            WriteAgentActionLogEntry(
                id=row.id,
                user_email=row.user_email,
                tool_name=row.tool_name,
                mcp_server_name=row.mcp_server_name,
                outcome=row.outcome,
                detail=row.detail,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )
