"""Inbound ingestion point for UniFi security events (from unifi-sim during
testing, or the real 36acresedge console in prod — same poller, same
endpoint, only the source label differs).

A host-side poller (scripts/unifi_event_poller.py in unifi-mcp-secure) calls
POST /unifi-events/ingest whenever it sees a new alert/IPS event via the
already-existing read-only unifi-network-mcp tools. This turns that into an
Onyx notification for every user, deep-linking into a new chat with the
UniFi Security Agent, pre-filled with the event so the user can ask it to
look into it. Nothing here executes any tool call itself — it only raises a
notification for a human to act on.
"""

from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.auth.email_utils import send_email
from onyx.configs.app_configs import WEB_DOMAIN
from onyx.configs.constants import NotificationType
from onyx.db.engine.sql_engine import get_session
from onyx.db.notification import create_notification
from onyx.db.persona import get_personas
from onyx.db.users import get_all_users
from onyx.server.features.unifi_events.auth import verify_unifi_events_token
from onyx.utils.logger import setup_logger

logger = setup_logger()

router = APIRouter(prefix="/unifi-events")

# Matches the persona created for this integration (see
# spec/onyx-security-agent-integration.md in unifi-mcp-secure). Looked up by
# name rather than a hardcoded persona_id since ids can differ across
# environments/reseeds.
SECURITY_AGENT_PERSONA_NAME = "UniFi Security Agent"

# In addition to the in-app notification (raised for every Onyx user), also
# email this address directly — a separate, explicit ask from the in-app
# push. Not read from an env var since only one fixed recipient was
# requested; revisit if that changes.
SECURITY_ALERT_EMAIL_RECIPIENT = "henry@arc.market"

_SEVERITY_PREFIX = {
    "critical": "\U0001f6a8",  # rotating light
    "warning": "⚠️",  # warning sign
    "info": "ℹ️",  # information
}


class UnifiSecurityEventIngest(BaseModel):
    source: Literal["sim", "real"]
    event_type: str
    title: str
    description: str | None = None
    severity: Literal["info", "warning", "critical"] = "warning"
    # Stable identifier for this exact event occurrence (e.g. the UniFi
    # alert's own _id/key + timestamp). Reused verbatim in the notification's
    # additional_data so create_notification's existing dedup (same user +
    # notif_type + additional_data = same row) naturally absorbs repeat
    # ingestion of the same event instead of spamming a new notification.
    dedup_key: str


class UnifiSecurityEventIngestResponse(BaseModel):
    notified_user_count: int


def _build_security_agent_link(event: UnifiSecurityEventIngest) -> str:
    prompt = (
        f"A UniFi security event just occurred on the {event.source} network: "
        f"{event.title}."
    )
    if event.description:
        prompt += f" {event.description}"
    prompt += " Can you look into this and tell me what's going on and what, if anything, I should do about it?"

    params = {"user-prompt": prompt}

    # Best-effort: if the Security Agent persona isn't found (e.g. renamed,
    # not yet created in this environment), still deep-link into a fresh
    # chat with the prompt filled in — the user just picks the agent
    # themselves instead of it being preselected.
    return "/app?" + urlencode(params)


def _prepend_persona_id(link: str, persona_id: int | None) -> str:
    if persona_id is None:
        return link
    separator = "&" if "?" in link else "?"
    return f"{link}{separator}{urlencode({'agentId': persona_id})}"


def _send_alert_email(title: str, event: UnifiSecurityEventIngest, absolute_link: str) -> None:
    description_html = f"<p>{event.description}</p>" if event.description else ""
    html_body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px;">
      <h2 style="margin-bottom: 4px;">{title}</h2>
      <p style="color: #555;">Source: {event.source} network &middot; type: {event.event_type}</p>
      {description_html}
      <a href="{absolute_link}"
         style="display: inline-block; margin-top: 16px; padding: 12px 24px;
                background: #2563eb; color: #fff; text-decoration: none;
                border-radius: 8px; font-weight: 600;">
        Open in Onyx
      </a>
    </div>
    """
    text_body = (
        f"{title}\n\nSource: {event.source} network, type: {event.event_type}\n"
        f"{event.description or ''}\n\nOpen in Onyx: {absolute_link}\n"
    )
    try:
        send_email(
            user_email=SECURITY_ALERT_EMAIL_RECIPIENT,
            subject=f"UniFi Security Agent: {title}",
            html_body=html_body,
            text_body=text_body,
        )
    except Exception:
        logger.exception(
            "Failed to email UniFi security alert to %s (is SMTP/SendGrid configured?)",
            SECURITY_ALERT_EMAIL_RECIPIENT,
        )


@router.post("/ingest")
def ingest_unifi_security_event(
    event: UnifiSecurityEventIngest,
    _: None = Depends(verify_unifi_events_token),
    db_session: Session = Depends(get_session),
) -> UnifiSecurityEventIngestResponse:
    security_agent_persona_id: int | None = None
    for persona in get_personas(db_session):
        if persona.name == SECURITY_AGENT_PERSONA_NAME:
            security_agent_persona_id = persona.id
            break
    if security_agent_persona_id is None:
        logger.warning(
            "UniFi security event ingested but no '%s' persona found; "
            "notification link will not preselect an agent",
            SECURITY_AGENT_PERSONA_NAME,
        )

    link = _prepend_persona_id(
        _build_security_agent_link(event), security_agent_persona_id
    )

    title_prefix = _SEVERITY_PREFIX.get(event.severity, "")
    title = f"{title_prefix} {event.title}".strip()

    notified_count = 0
    for user in get_all_users(db_session, include_api_key_users=False):
        create_notification(
            user_id=user.id,
            notif_type=NotificationType.UNIFI_SECURITY_ALERT,
            db_session=db_session,
            title=title,
            description=event.description,
            additional_data={
                "link": link,
                "dedup_key": event.dedup_key,
                "source": event.source,
                "severity": event.severity,
                "event_type": event.event_type,
            },
        )
        notified_count += 1

    logger.info(
        "UniFi security event '%s' (%s, %s) notified %d user(s)",
        event.title,
        event.source,
        event.dedup_key,
        notified_count,
    )

    _send_alert_email(title, event, f"{WEB_DOMAIN}{link}")

    return UnifiSecurityEventIngestResponse(notified_user_count=notified_count)
