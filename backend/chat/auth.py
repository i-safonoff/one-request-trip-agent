"""Telegram Mini App authentication and API session tokens.

Mini App flow: the client sends Telegram's `initData`; we verify its HMAC with the bot token
(a client-supplied user id is never trusted), then hand out a short-lived signed token.
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""
import base64
import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config import Settings, get_settings
from shared.database import get_session
from shared.models import User


class InvalidInitData(Exception):
    pass


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int, now: float | None = None) -> dict:
    """Return the Telegram user object from valid, fresh initData; raise InvalidInitData otherwise."""
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    received = params.pop("hash", None)
    if not received:
        raise InvalidInitData("hash is missing")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InvalidInitData("signature mismatch")
    try:
        auth_date = int(params["auth_date"])
    except (KeyError, ValueError):
        raise InvalidInitData("auth_date is missing")
    if (now if now is not None else time.time()) - auth_date > max_age_seconds:
        raise InvalidInitData("initData has expired")
    try:
        user = json.loads(params["user"])
        int(user["id"])
    except (KeyError, ValueError, TypeError):
        raise InvalidInitData("user is missing")
    return user


def _signing_key(settings: Settings) -> bytes | None:
    if settings.session_secret:
        return settings.session_secret.encode()
    if settings.telegram_bot_token:
        return hmac.new(b"session", settings.telegram_bot_token.encode(), hashlib.sha256).digest()
    return None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def issue_token(user_id: uuid.UUID, settings: Settings, now: float | None = None) -> str:
    key = _signing_key(settings)
    if key is None:
        raise RuntimeError("no session secret or bot token configured")
    payload = _b64(json.dumps({"sub": str(user_id), "exp": int((now or time.time()) + settings.session_ttl_seconds)}).encode())
    return f"{payload}.{_b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())}"


def verify_token(token: str, settings: Settings, now: float | None = None) -> uuid.UUID | None:
    key = _signing_key(settings)
    try:
        payload, sig = token.split(".")
        if key is None or not hmac.compare_digest(_b64(hmac.new(key, payload.encode(), hashlib.sha256).digest()), sig):
            return None
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if data["exp"] < (now or time.time()):
            return None
        return uuid.UUID(data["sub"])
    except (ValueError, KeyError, TypeError):
        return None


async def current_user(authorization: str | None = Header(default=None),
                       token: str | None = Query(default=None, include_in_schema=False),
                       session: AsyncSession = Depends(get_session)) -> User:
    """FastAPI dependency: the user behind `Authorization: Bearer <token>` (or `?token=` for EventSource, which cannot
    set headers)."""
    scheme, _, bearer = (authorization or "").partition(" ")
    user_id = verify_token(bearer, get_settings()) if scheme.lower() == "bearer" else (
        verify_token(token, get_settings()) if token else None)
    user = await session.scalar(select(User).where(User.id == user_id)) if user_id else None
    if user is None:
        raise HTTPException(401, "Invalid or missing session token")
    return user
