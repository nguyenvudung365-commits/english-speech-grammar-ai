import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, Response
from sqlalchemy import delete, select

from src.api.models import AuthSession, User, now
from src.api.storage import digest

hasher = PasswordHasher()
DUMMY_HASH = hasher.hash("not-a-user-password")


def token_hash(value: str) -> str:
    return digest(value.encode())


def current_user(request: Request) -> str:
    raw = request.cookies.get("session", "")
    with request.app.state.db.sessions() as db:
        auth = db.get(AuthSession, token_hash(raw))
        user = db.get(User, auth.user_id) if auth and auth.expires_at > now() else None
        if not user or user.disabled_at or user.deleted_at:
            raise HTTPException(401, "Authentication required")
        return user.id


def check_csrf(request: Request) -> None:
    token = request.headers.get("X-CSRF-Token", "")
    if not token or len(token) > 200:
        raise HTTPException(403, "CSRF token required")
    session = request.cookies.get("session")
    if session:
        with request.app.state.db.sessions() as db:
            auth = db.get(AuthSession, token_hash(session))
            valid = (
                auth
                and auth.expires_at > now()
                and secrets.compare_digest(auth.csrf_hash, token_hash(token))
            )
    else:
        valid = secrets.compare_digest(request.cookies.get("csrf", ""), token)
    if not valid:
        raise HTTPException(403, "Invalid CSRF token")


def set_cookie(
    response: Response, request: Request, key: str, value: str, age: int, http_only=True
):
    response.set_cookie(
        key,
        value,
        max_age=age,
        httponly=http_only,
        secure=request.app.state.settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def login_session(request: Request, response: Response, email: str, password: str) -> dict:
    database = request.app.state.db
    with database.sessions.begin() as db:
        user = db.scalar(select(User).where(User.email == email.lower()))
        try:
            correct = hasher.verify(user.credential if user else DUMMY_HASH, password)
        except (VerificationError, InvalidHashError):
            correct = False
        if not correct or not user or user.disabled_at or user.deleted_at:
            raise HTTPException(401, "Invalid credentials")
        previous = request.cookies.get("session")
        if previous:
            db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(previous)))
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        hours = request.app.state.settings.session_hours
        db.add(
            AuthSession(
                token_hash=token_hash(raw),
                user_id=user.id,
                csrf_hash=token_hash(csrf),
                expires_at=now() + timedelta(hours=hours),
            )
        )
    set_cookie(response, request, "session", raw, hours * 3600)
    set_cookie(response, request, "csrf", csrf, hours * 3600, False)
    return {"csrf_token": csrf}
