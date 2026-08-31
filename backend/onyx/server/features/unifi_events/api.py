"""Inbound ingestion point for UniFi security events (from unifi-sim during
testing, or the real 36acresedge console in prod — same poller, same
endpoint, only the source label differs).

A host-side poller (scripts/unifi_event_poller.py in unifi-mcp-secure) calls
POST /unifi-events/ingest whenever it sees a new alert/IPS event via the
already-existing read-only unifi-network-mcp tools. This turns that into an
Onyx notification for every user.

For warning/critical events (subject to a rate limit), this also runs one
real agent turn server-side before notifying — see `_run_agent_reasoning` —
so the notification carries the agent's actual analysis/proposal instead of
a generic "go look into this" prompt. That agent turn can only ever
*propose* a change (see spec/write-capable-security-agent-admin-gating.md in
unifi-mcp-secure) — it never executes anything itself, and structurally
can't: it runs as a non-admin user, so the same admin gate that already
protects `action_confirm` blocks it regardless of what the model does.
"""

import time
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.auth.email_utils import send_email
from onyx.auth.sms_utils import send_sms
from onyx.auth.users import get_anonymous_user
from onyx.chat.models import AnswerStream, CreateChatSessionID
from onyx.chat.process_message import gather_stream, handle_stream_message_objects
from onyx.configs.app_configs import WEB_DOMAIN
from onyx.configs.constants import NotificationType
from onyx.db.chat import update_chat_session
from onyx.db.engine.sql_engine import get_session
from onyx.db.models import ChatSessionSharedStatus
from onyx.db.notification import create_notification
from onyx.db.persona import get_personas
from onyx.db.users import get_all_users
from onyx.key_value_store.factory import get_kv_store
from onyx.key_value_store.interface import KvKeyNotFoundError
from onyx.server.features.unifi_events.auth import verify_unifi_events_token
from onyx.server.query_and_chat.models import (
    ChatSessionCreationRequest,
    MessageOrigin,
    SendMessageRequest,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

router = APIRouter(prefix="/unifi-events")

# Matches the persona created for this integration (see
# spec/onyx-security-agent-integration.md in unifi-mcp-secure). Looked up by
# name rather than a hardcoded persona_id since ids can differ across
# environments/reseeds.
SECURITY_AGENT_PERSONA_NAME = "UniFi Security Agent"

# Looked up by name, same pattern as SECURITY_AGENT_PERSONA_NAME above. This
# persona does not exist in production yet (see spec/always-monitor.md,
# unifi-mcp-secure) -- until it's created, _run_agent_reasoning() logs
# "persona not found, skipping" and this endpoint falls back to its existing
# human-initiated-only notification behavior, unchanged. Deliberately named
# for its eventual production role, not "sim" -- the sim-only sandbox this
# was built and tested against locally used the name "UniFi Agent (Sim
# Sandbox)"; this constant is what the real, eventually-merged production
# persona should be named once it's created.
MONITOR_AGENT_PERSONA_NAME = "UniFi Agent"

# Only these severities are worth an automatic LLM turn — "info" still gets
# the plain notification below, just without agent reasoning.
MONITOR_AGENT_SEVERITIES = {"warning", "critical"}

# At most one agent-reasoning run per this many seconds. An alert arriving
# inside the cooldown still gets the existing plain notification/SMS/email —
# it's never silently dropped, just without fresh agent analysis.
MONITOR_AGENT_RATE_LIMIT_SECONDS = 300
MONITOR_AGENT_KV_KEY = "unifi_monitor_last_agent_trigger_ts"

# SMS is a much tighter medium than email/in-app — cap the reasoned answer's
# contribution to the text so one verbose agent turn doesn't blow up into a
# multi-part SMS.
SMS_DESCRIPTION_MAX_CHARS = 400

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
    agent_reasoning_ran: bool


def _build_event_prompt(event: UnifiSecurityEventIngest) -> str:
    prompt = (
        f"A UniFi security event just occurred on the {event.source} network: "
        f"{event.title}."
    )
    if event.description:
        prompt += f" {event.description}"
    prompt += " Can you look into this and tell me what's going on and what, if anything, I should do about it?"
    return prompt


def _build_security_agent_link(event: UnifiSecurityEventIngest) -> str:
    params = {"user-prompt": _build_event_prompt(event)}

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


def _build_chat_session_link(chat_session_id: UUID) -> str:
    # Not /app?chatId=... — that route requires the viewer to *own* the chat
    # session, and this one is owned by the anonymous system user that ran
    # the reasoning turn, not whoever ends up clicking the notification.
    # /app/shared/{id} is Onyx's existing public-chat-link mechanism (no
    # ownership check, still supports replying) — see the
    # `sharing_status=PUBLIC` call in `_run_agent_reasoning`.
    return f"/app/shared/{chat_session_id}"


def _run_agent_reasoning(
    event: UnifiSecurityEventIngest, db_session: Session
) -> tuple[str | None, UUID | None]:
    """Runs one real agent turn server-side against the sim-only monitor
    persona and returns its final answer text + the chat session it landed
    in, or (None, None) if reasoning was skipped (severity/rate-limit/no
    persona) or failed. Never raises — a problem here should degrade to the
    existing plain-notification behavior, not break ingestion.

    Uses the same synchronous non-streaming pattern as the Slack bot handler
    (onyx/onyxbot/slack/handlers/handle_regular_answer.py) to run a chat turn
    with no live user session. Runs as the anonymous (non-admin) user
    deliberately: `action_confirm` is admin-gated in MCPTool.run(), so even
    if the model somehow attempted to confirm rather than just propose, the
    existing admin check blocks it structurally — this isn't a new safety
    mechanism, it's the existing one applying one step earlier."""
    if event.severity not in MONITOR_AGENT_SEVERITIES:
        return None, None

    kv_store = get_kv_store()
    try:
        last_run_raw = kv_store.load(MONITOR_AGENT_KV_KEY)
    except KvKeyNotFoundError:
        last_run_raw = None
    if last_run_raw is not None:
        elapsed = time.time() - float(last_run_raw)
        if elapsed < MONITOR_AGENT_RATE_LIMIT_SECONDS:
            logger.info(
                "UniFi monitor: skipping agent reasoning for '%s' — rate-limited "
                "(last run %.0fs ago, limit %ds)",
                event.title,
                elapsed,
                MONITOR_AGENT_RATE_LIMIT_SECONDS,
            )
            return None, None

    monitor_persona_id: int | None = None
    for persona in get_personas(db_session):
        if persona.name == MONITOR_AGENT_PERSONA_NAME:
            monitor_persona_id = persona.id
            break
    if monitor_persona_id is None:
        logger.warning(
            "UniFi monitor: '%s' persona not found, skipping agent reasoning",
            MONITOR_AGENT_PERSONA_NAME,
        )
        return None, None

    new_message_request = SendMessageRequest(
        message=_build_event_prompt(event),
        origin=MessageOrigin.UNIFI_MONITOR,
        chat_session_info=ChatSessionCreationRequest(persona_id=monitor_persona_id),
    )

    captured_chat_session_id: UUID | None = None

    def _tap_chat_session_id(packets: AnswerStream) -> AnswerStream:
        nonlocal captured_chat_session_id
        for packet in packets:
            if isinstance(packet, CreateChatSessionID):
                captured_chat_session_id = packet.chat_session_id
            yield packet

    anonymous_user = get_anonymous_user()
    try:
        packets = handle_stream_message_objects(
            new_msg_req=new_message_request,
            user=anonymous_user,
            bypass_acl=False,
        )
        answer = gather_stream(_tap_chat_session_id(packets))
    except Exception:
        logger.exception(
            "UniFi monitor: agent reasoning turn failed for '%s'", event.title
        )
        return None, None

    if answer.error_msg or not answer.answer:
        logger.warning(
            "UniFi monitor: agent reasoning returned no usable answer for '%s': %s",
            event.title,
            answer.error_msg,
        )
        return None, None

    kv_store.store(MONITOR_AGENT_KV_KEY, time.time())

    # The session is owned by the anonymous system user that just ran this
    # turn, not whoever ends up clicking the notification -- mark it public
    # so /app/shared/{id} works for them without an ownership check. If this
    # fails for any reason, still return the (good) reasoning text but no
    # session id, so the caller falls back to a link that actually works
    # instead of a dead one.
    if captured_chat_session_id is not None:
        try:
            update_chat_session(
                db_session=db_session,
                user_id=anonymous_user.id,
                chat_session_id=captured_chat_session_id,
                sharing_status=ChatSessionSharedStatus.PUBLIC,
            )
        except Exception:
            logger.exception(
                "UniFi monitor: failed to mark chat session %s public",
                captured_chat_session_id,
            )
            captured_chat_session_id = None

    return answer.answer, captured_chat_session_id


def _send_alert_email(title: str, description: str | None, event: UnifiSecurityEventIngest, absolute_link: str) -> None:
    description_html = f"<p>{description}</p>" if description else ""
    html_body = f"""
    <div style="font-family: -apple-system, sans-serif; max-width: 480px;">
      <h2 style="margin-bottom: 4px;">{title}</h2>
      <p style="color: #555;">Source: {event.source} network &middot; type: {event.event_type}</p>
      {description_html}
      <a href="{absolute_link}"
         style="display: inline-block; margin-top: 16px; padding: 12px 24px;
                background: #2563eb; color: #fff; text-decoration: none;
                border-radius: 8px; font-weight: 600;">
        Open in NovoLink AI
      </a>
    </div>
    """
    text_body = (
        f"{title}\n\nSource: {event.source} network, type: {event.event_type}\n"
        f"{description or ''}\n\nOpen in NovoLink AI: {absolute_link}\n"
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


def _send_alert_sms(title: str, description: str | None, event: UnifiSecurityEventIngest, phone_number: str) -> None:
    sms_description = description
    if sms_description and len(sms_description) > SMS_DESCRIPTION_MAX_CHARS:
        sms_description = sms_description[:SMS_DESCRIPTION_MAX_CHARS].rstrip() + "... (open the app for full details)"
    body = (
        f"{title} ({event.source} network, {event.event_type})."
        + (f" {sms_description}" if sms_description else "")
    )
    try:
        send_sms(phone_number, body)
    except Exception:
        logger.exception(
            "Failed to SMS UniFi security alert to %s (is Twilio configured?)",
            phone_number,
        )


@router.post("/ingest")
def ingest_unifi_security_event(
    event: UnifiSecurityEventIngest,
    _: None = Depends(verify_unifi_events_token),
    db_session: Session = Depends(get_session),
) -> UnifiSecurityEventIngestResponse:
    reasoned_answer, reasoned_chat_session_id = _run_agent_reasoning(event, db_session)

    # Reasoning text and the link are independent: a real answer is worth
    # showing even if (for whatever reason) a working link to its own
    # session couldn't be built -- falling back to the generic agent link in
    # that case, never discarding good reasoning just because the link
    # couldn't be made.
    description = reasoned_answer if reasoned_answer is not None else event.description

    if reasoned_chat_session_id is not None:
        link = _build_chat_session_link(reasoned_chat_session_id)
    else:
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
            description=description,
            additional_data={
                "link": link,
                "dedup_key": event.dedup_key,
                "source": event.source,
                "severity": event.severity,
                "event_type": event.event_type,
            },
        )
        notified_count += 1

        if user.phone_number:
            _send_alert_sms(title, description, event, user.phone_number)

    logger.info(
        "UniFi security event '%s' (%s, %s) notified %d user(s), agent reasoning: %s",
        event.title,
        event.source,
        event.dedup_key,
        notified_count,
        "ran" if reasoned_answer is not None else "skipped",
    )

    _send_alert_email(title, description, event, f"{WEB_DOMAIN}{link}")

    return UnifiSecurityEventIngestResponse(
        notified_user_count=notified_count,
        agent_reasoning_ran=reasoned_answer is not None,
    )
