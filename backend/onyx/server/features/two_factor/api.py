"""Replaces the stock fastapi_users login mount (fastapi_users.get_auth_router
(auth_backend), previously mounted at main.py's /auth prefix) with a version
that branches on SMS 2FA. Reuses UserManager.authenticate() and
auth_backend.login(strategy, user) exactly as onyx.auth.users' existing
OAuth callback route (complete_login_flow) already does for issuing a real
session -- no new session-issuance mechanism, just a gate in front of the
existing one.

A user with no phone_number set logs in exactly as before (password only).
A user with one set gets an SMS code and only a short-lived pre-auth token
in the immediate response -- the real session is not issued until that code
is verified. See spec/twilio-sms-integration.md (unifi-mcp-secure) for why
this shape (a structurally distinct token, not a flag on a real session).

The stock router (fastapi_users.get_auth_router) bundles /login AND
/logout together; replacing the whole mount means /logout has to be
re-added here too (verbatim from fastapi_users.router.auth.get_auth_router)
-- otherwise it silently disappears, which is exactly what happened the
first time this file was written (caught via a real "Failed to logout" in
the browser, not by any of the automated checks -- none of them exercise
the full login-then-logout cycle).
"""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_users.authentication import Strategy
from pydantic import BaseModel

from onyx.auth.two_factor import (
    OtpSendResult,
    generate_and_send_otp,
    issue_pre_auth_token,
    verify_otp,
    verify_pre_auth_token,
)
from onyx.auth.users import UserManager, auth_backend, fastapi_users, get_user_manager
from onyx.db.models import User
from onyx.utils.logger import setup_logger

logger = setup_logger()

router = APIRouter()


class LoginTwoFactorRequired(BaseModel):
    requires_2fa: bool = True
    pre_auth_token: str


class TwoFactorVerifyRequest(BaseModel):
    pre_auth_token: str
    code: str


class TwoFactorResendRequest(BaseModel):
    pre_auth_token: str


class TwoFactorResendResponse(BaseModel):
    sent: bool
    retry_after_seconds: int | None = None
    reason: str | None = None


def _bad_credentials() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST, detail="LOGIN_BAD_CREDENTIALS"
    )


def _invalid_pre_auth_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired verification session -- please log in again",
    )


def _otp_send_failure(result: OtpSendResult) -> HTTPException:
    if result.reason == "rate_limited":
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Code already sent recently, retry in {result.retry_after_seconds}s",
        )
    # "send_failed" (Twilio/carrier error) or unknown -- don't silently fall
    # back to a password-only login, since that would quietly weaken the 2FA
    # guarantee exactly when SMS is broken. See spec/twilio-sms-integration.md
    # (unifi-mcp-secure) open question #4.
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Unable to send verification code right now. Please try again shortly.",
    )


@router.post("/login", response_model=None)
async def login(
    request: Request,
    credentials: OAuth2PasswordRequestForm = Depends(),
    user_manager: UserManager = Depends(get_user_manager),
    strategy: Strategy = Depends(auth_backend.get_strategy),
) -> LoginTwoFactorRequired | Response:
    user = await user_manager.authenticate(credentials)
    if user is None or not user.is_active:
        raise _bad_credentials()

    if not user.phone_number:
        # No 2FA enrolled for this user -- same session-issuance call the
        # existing OAuth callback route already uses, unchanged behavior.
        response = await auth_backend.login(strategy, user)
        await user_manager.on_after_login(user, request, response)
        return response

    result = generate_and_send_otp(user.id, user.phone_number)
    if not result.sent:
        raise _otp_send_failure(result)

    return LoginTwoFactorRequired(pre_auth_token=issue_pre_auth_token(user.id))


@router.post("/logout")
async def logout(
    user_token: tuple[User, str] = Depends(
        fastapi_users.authenticator.current_user_token(active=True, verified=False)
    ),
    strategy: Strategy = Depends(auth_backend.get_strategy),
) -> Response:
    user, token = user_token
    return await auth_backend.logout(strategy, user, token)


@router.post("/2fa/verify", response_model=None)
async def verify_2fa(
    request: Request,
    body: TwoFactorVerifyRequest,
    user_manager: UserManager = Depends(get_user_manager),
    strategy: Strategy = Depends(auth_backend.get_strategy),
) -> Response:
    user_id = verify_pre_auth_token(body.pre_auth_token)
    if user_id is None:
        raise _invalid_pre_auth_token()

    user = await user_manager.get(user_id)
    if not user.phone_number:
        # Shouldn't happen (a pre-auth token is only ever issued for a user
        # who had a phone_number at login time), but don't assume -- Twilio
        # Verify's check API needs a phone number, not just our user_id.
        raise _invalid_pre_auth_token()

    result = verify_otp(user_id, user.phone_number, body.code)
    if not result.verified:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=result.reason)

    response = await auth_backend.login(strategy, user)
    await user_manager.on_after_login(user, request, response)
    return response


@router.post("/2fa/resend")
async def resend_2fa(
    body: TwoFactorResendRequest,
    user_manager: UserManager = Depends(get_user_manager),
) -> TwoFactorResendResponse:
    user_id = verify_pre_auth_token(body.pre_auth_token)
    if user_id is None:
        raise _invalid_pre_auth_token()

    user = await user_manager.get(user_id)
    if not user.phone_number:
        # Shouldn't happen (a pre-auth token is only ever issued for a user
        # who had a phone_number at login time), but don't assume.
        raise _invalid_pre_auth_token()

    result = generate_and_send_otp(user.id, user.phone_number)
    return TwoFactorResendResponse(
        sent=result.sent,
        retry_after_seconds=result.retry_after_seconds,
        reason=result.reason,
    )
