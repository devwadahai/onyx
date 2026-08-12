import re
import uuid
from enum import Enum
from typing import Any

from fastapi_users import schemas
from pydantic import field_validator
from typing_extensions import override

from onyx.db.enums import AccountType

# E.164: a leading '+', a first digit 1-9, then up to 14 more digits.
# Mirrors backend/onyx/server/manage/users.py's _PHONE_NUMBER_RE.
_PHONE_NUMBER_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class UserRole(str, Enum):
    """
    User roles
    - Basic can't perform any admin actions
    - Admin can perform all admin actions
    - Curator can perform admin actions for
        groups they are curators of
    - Global Curator can perform admin actions
        for all groups they are a member of
    - Limited can access a limited set of basic api endpoints
    - Slack are users that have used onyx via slack but dont have a web login
    - External permissioned users that have been picked up during the external permissions sync process but don't have a web login
    """

    LIMITED = "limited"
    BASIC = "basic"
    ADMIN = "admin"
    CURATOR = "curator"
    GLOBAL_CURATOR = "global_curator"
    SLACK_USER = "slack_user"
    EXT_PERM_USER = "ext_perm_user"

    def is_web_login(self) -> bool:
        return self not in [
            UserRole.SLACK_USER,
            UserRole.EXT_PERM_USER,
        ]


class UserRead(schemas.BaseUser[uuid.UUID]):
    role: UserRole


class UserCreate(schemas.BaseUserCreate):
    role: UserRole = UserRole.BASIC
    account_type: AccountType = AccountType.STANDARD
    tenant_id: str | None = None
    # Captcha token for cloud signup protection (optional, only used when captcha is enabled)
    # Excluded from create_update_dict so it never reaches the DB layer
    captcha_token: str | None = None
    # E.164 format. Optional at this shared schema level -- SSO/SAML account
    # auto-provisioning (onyx.auth.users, onyx.server.saml) constructs
    # UserCreate directly and has no phone number to supply -- but the
    # self-service email/password signup form requires it client-side. See
    # spec/twilio-sms-integration.md (unifi-mcp-secure).
    phone_number: str | None = None

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, value: str | None) -> str | None:
        if value is not None and not _PHONE_NUMBER_RE.match(value):
            raise ValueError(
                "Phone number must be in E.164 format, e.g. +15551234567."
            )
        return value

    @override
    def create_update_dict(self) -> dict[str, Any]:
        d = super().create_update_dict()
        d.pop("captcha_token", None)
        # Force STANDARD for self-registration; only trusted paths
        # (SCIM, API key creation) supply a different account_type directly.
        d["account_type"] = AccountType.STANDARD
        return d

    @override
    def create_update_dict_superuser(self) -> dict[str, Any]:
        d = super().create_update_dict_superuser()
        d.pop("captcha_token", None)
        d.setdefault("account_type", self.account_type)
        return d


class UserUpdate(schemas.BaseUserUpdate):
    """
    Role updates are not allowed through the user update endpoint for security reasons
    Role changes should be handled through a separate, admin-only process
    """


class AuthBackend(str, Enum):
    REDIS = "redis"
    POSTGRES = "postgres"
    JWT = "jwt"
