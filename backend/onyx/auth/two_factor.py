"""SMS-based 2FA via Twilio Verify. Pre-auth tokens are structurally distinct
from real session tokens (same JWT machinery fastapi-users already uses
elsewhere in this file, but a different `aud` claim), not a flag on a real
session that every endpoint has to remember to check.

Code generation, storage, expiry, and attempt-limiting are all handled by
Twilio Verify itself (https://www.twilio.com/docs/verify/api) -- not our
own KV-store-based codes (an earlier version of this module did that; see
git history if you need it). Verify sends from its own compliant short-code
pool, which is why it delivers even though TWILIO_FROM_NUMBER (a self-owned
standard 10-digit number, used for the separate general-notification SMS
path in sms_utils.py) is blocked pending A2P 10DLC registration on this
Twilio account -- see spec/twilio-sms-integration.md's setup notes in
unifi-mcp-secure. Twilio correlates a verification by phone number, not by
our internal user_id, so verify_otp needs the phone number too, unlike a
self-hosted OTP store keyed by user_id alone.

See spec/twilio-sms-integration.md (unifi-mcp-secure) for the full design.
"""

import uuid

from fastapi_users.jwt import decode_jwt, generate_jwt
from pydantic import BaseModel
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from onyx.configs.app_configs import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_VERIFY_CONFIGURED,
    TWILIO_VERIFY_SERVICE_SID,
    USER_AUTH_SECRET,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

# Twilio error codes we branch on -- see https://www.twilio.com/docs/api/errors
_ERR_MAX_SEND_ATTEMPTS = 60203  # too many verification sends to this number recently
_ERR_MAX_CHECK_ATTEMPTS = 60202  # too many wrong-code guesses against this verification
_ERR_VERIFICATION_NOT_FOUND = 20404  # no pending verification (expired or never sent)

PRE_AUTH_TOKEN_AUDIENCE = "onyx:2fa-pending"
# Matches Twilio Verify's own default code validity window (10 minutes) --
# no point a pre-auth token outliving the code it's gating entry to.
PRE_AUTH_TOKEN_TTL_SECONDS = 10 * 60

_RATE_LIMIT_RETRY_AFTER_SECONDS = 30  # Twilio doesn't return an exact value; a sane default


class OtpSendResult(BaseModel):
    sent: bool
    retry_after_seconds: int | None = None
    # "rate_limited" | "send_failed" -- lets callers tell "you already have a
    # valid code, slow down" apart from "Twilio/Verify itself is broken"
    # and respond differently (429 + wait vs. a hard failure).
    reason: str | None = None


class OtpVerifyResult(BaseModel):
    verified: bool
    reason: str | None = None  # "expired" | "no_code" | "too_many_attempts" | "mismatch"


def _verify_service():
    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    return client.verify.v2.services(TWILIO_VERIFY_SERVICE_SID)


def generate_and_send_otp(user_id: uuid.UUID, phone_number: str) -> OtpSendResult:
    """Triggers a Twilio Verify send. `user_id` is only used for logging --
    Twilio correlates the verification by phone_number, not our internal id.
    """
    if not TWILIO_VERIFY_CONFIGURED:
        raise ValueError("Twilio Verify is not configured.")

    try:
        verification = _verify_service().verifications.create(
            to=phone_number, channel="sms"
        )
    except TwilioRestException as e:
        if e.code == _ERR_MAX_SEND_ATTEMPTS:
            logger.warning("Twilio Verify rate-limited send for user %s", user_id)
            return OtpSendResult(
                sent=False,
                reason="rate_limited",
                retry_after_seconds=_RATE_LIMIT_RETRY_AFTER_SECONDS,
            )
        logger.exception("Twilio Verify send failed for user %s", user_id)
        return OtpSendResult(sent=False, reason="send_failed")

    logger.info(
        "Sent Twilio Verify OTP to user %s (status=%s)", user_id, verification.status
    )
    return OtpSendResult(sent=True)


def verify_otp(
    user_id: uuid.UUID, phone_number: str, submitted_code: str
) -> OtpVerifyResult:
    try:
        check = _verify_service().verification_checks.create(
            to=phone_number, code=submitted_code
        )
    except TwilioRestException as e:
        if e.code == _ERR_VERIFICATION_NOT_FOUND:
            return OtpVerifyResult(verified=False, reason="no_code")
        if e.code == _ERR_MAX_CHECK_ATTEMPTS:
            return OtpVerifyResult(verified=False, reason="too_many_attempts")
        logger.exception("Twilio Verify check failed for user %s", user_id)
        return OtpVerifyResult(verified=False, reason="no_code")

    if check.status == "approved" and check.valid:
        return OtpVerifyResult(verified=True)

    # "canceled" is Verify's own max-attempts state for a given verification
    # (distinct from the 60202 exception above, which fires on the attempt
    # that trips the limit -- "canceled" is what a later check against the
    # now-dead verification returns).
    if check.status == "canceled":
        return OtpVerifyResult(verified=False, reason="too_many_attempts")

    return OtpVerifyResult(verified=False, reason="mismatch")


def issue_pre_auth_token(user_id: uuid.UUID) -> str:
    return generate_jwt(
        data={"sub": str(user_id), "aud": PRE_AUTH_TOKEN_AUDIENCE},
        secret=USER_AUTH_SECRET,
        lifetime_seconds=PRE_AUTH_TOKEN_TTL_SECONDS,
    )


def verify_pre_auth_token(token: str) -> uuid.UUID | None:
    """Returns the user_id if the token is a valid, unexpired pre-auth token
    for the 2FA-pending audience specifically -- a real session token issued
    by fastapi-users' own strategy will not decode against this audience, so
    the two token types cannot be confused for one another.
    """
    try:
        payload = decode_jwt(
            token, USER_AUTH_SECRET, audience=[PRE_AUTH_TOKEN_AUDIENCE]
        )
    except Exception:
        return None

    sub = payload.get("sub")
    if not sub:
        return None
    try:
        return uuid.UUID(sub)
    except ValueError:
        return None
