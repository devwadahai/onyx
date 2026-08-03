from typing import Literal

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from onyx.auth.permissions import require_permission
from onyx.db.enums import Permission
from onyx.db.models import User
from onyx.utils.logger import setup_logger

logger = setup_logger()

admin_router = APIRouter(prefix="/admin/unifi-target")

# The unifi-network-mcp control panel is a small standalone process on the
# Docker host (scripts/mcp_target_control_panel.py in unifi-mcp-secure),
# reachable via the same host.docker.internal route already used to reach
# the MCP server itself. This router is just a thin authenticated proxy so
# the switch is a click in Onyx's own admin UI instead of a host terminal
# command.
CONTROL_PANEL_BASE_URL = "http://host.docker.internal:9000"
_STATUS_TIMEOUT_SECONDS = 5
_SWITCH_TIMEOUT_SECONDS = 35

_UNREACHABLE_DETAIL = (
    "Can't reach the unifi-network-mcp control panel on the host "
    "(is scripts/mcp_target_control_panel.py running on port 9000?)."
)


class UnifiTargetStatus(BaseModel):
    target: str | None
    output: str
    ok: bool | None


class UnifiTargetSwitchRequest(BaseModel):
    target: Literal["sim", "real"]


@admin_router.get("/status")
def get_unifi_target_status(
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> UnifiTargetStatus:
    try:
        resp = requests.get(
            f"{CONTROL_PANEL_BASE_URL}/status", timeout=_STATUS_TIMEOUT_SECONDS
        )
        resp.raise_for_status()
        return UnifiTargetStatus(**resp.json())
    except requests.RequestException as e:
        logger.warning("unifi-mcp target control panel unreachable: %s", e)
        raise HTTPException(status_code=503, detail=_UNREACHABLE_DETAIL)


@admin_router.post("/switch")
def switch_unifi_target(
    switch_request: UnifiTargetSwitchRequest,
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> UnifiTargetStatus:
    try:
        resp = requests.post(
            f"{CONTROL_PANEL_BASE_URL}/switch/{switch_request.target}",
            timeout=_SWITCH_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        return UnifiTargetStatus(**resp.json())
    except requests.RequestException as e:
        logger.warning("unifi-mcp target switch failed: %s", e)
        raise HTTPException(status_code=503, detail=_UNREACHABLE_DETAIL)
