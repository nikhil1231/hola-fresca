"""The signed-in account shown in the application chrome and settings."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api import access
from app.api.deps import get_current_user, get_session
from app.api.schemas import AccountOut
from app.db.models import User

router = APIRouter(prefix="/api/account", tags=["account"])


@router.get("", response_model=AccountOut)
def current_account(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
) -> AccountOut:
    """Return a verified production identity or its local presentation mock."""
    # Only ask Cloudflare for the display name when we have not already learned
    # it. The name lives in get-identity rather than in the compact application
    # token, so enriching means an outbound request to the team endpoint — and
    # this endpoint is hit on every page load. Once the name is on the row there
    # is nothing left to learn, so the common case costs nothing.
    identity = (
        access.authenticated_identity(request)
        if user.name
        else access.full_identity(request)
    )
    if identity is None:
        # Localhost/LAN traffic never crosses Cloudflare, so it has no assertion
        # or get-identity profile. Match the production response shape without
        # writing this mock into users or letting it influence authorization.
        local = access.local_identity()
        return AccountOut(email=local.email, name=local.name, is_admin=bool(user.is_admin))

    if identity.name and identity.name != user.name:
        user.name = identity.name
        session.commit()

    return AccountOut(
        email=user.email,
        name=user.name,
        access_authenticated=True,
        logout_url="/cdn-cgi/access/logout",
        is_admin=bool(user.is_admin),
    )
