"""Bearer-token auth for the UniFi security-event ingestion endpoint.

Same fail-secure pattern as onyx.server.metrics.metrics_auth: if
UNIFI_EVENTS_INGEST_TOKEN is unset, the endpoint is locked (every request
gets 401) rather than exposed by accident. Callers present it as
``Authorization: Bearer <token>``.
"""

import secrets

from fastapi import Request

from onyx.auth.constants import BEARER_PREFIX
from onyx.configs.app_configs import UNIFI_EVENTS_INGEST_TOKEN
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError

_WWW_AUTHENTICATE = {"WWW-Authenticate": "Bearer"}


def verify_unifi_events_token(request: Request) -> None:
    expected = UNIFI_EVENTS_INGEST_TOKEN
    if not expected:
        raise OnyxError(
            OnyxErrorCode.UNAUTHENTICATED,
            "/unifi-events/ingest auth not configured; set "
            "UNIFI_EVENTS_INGEST_TOKEN",
            headers=_WWW_AUTHENTICATE,
        )

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith(BEARER_PREFIX):
        raise OnyxError(
            OnyxErrorCode.UNAUTHENTICATED,
            "Missing or invalid unifi-events bearer token",
            headers=_WWW_AUTHENTICATE,
        )

    provided = auth_header[len(BEARER_PREFIX) :].strip()
    if not secrets.compare_digest(provided, expected):
        raise OnyxError(
            OnyxErrorCode.UNAUTHENTICATED,
            "Invalid unifi-events bearer token",
            headers=_WWW_AUTHENTICATE,
        )
