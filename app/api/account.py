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
    identity = access.full_identity(request)
    if identity is None:
        # Localhost/LAN traffic never crosses Cloudflare, so it has no assertion
        # or get-identity profile. Match the production response shape without
        # writing this mock into users or letting it influence authorization.
        local = access.local_identity()
        return AccountOut(email=local.email, name=local.name)

    if identity.name and identity.name != user.name:
        user.name = identity.name
        session.commit()

    return AccountOut(
        email=user.email,
        name=user.name,
        access_authenticated=True,
        logout_url="/cdn-cgi/access/logout",
    )
