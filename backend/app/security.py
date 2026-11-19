from datetime import datetime, timedelta, timezone
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import get_db
from app.models import User

passwords = PasswordHash.recommended()
bearer = HTTPBearer()

def make_token(user: User):
    return jwt.encode({"sub": user.id, "exp": datetime.now(timezone.utc) + timedelta(days=3)}, settings.jwt_secret, algorithm="HS256")

def token_user_id(token: str) -> str:
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return str(data["sub"])
    except (jwt.InvalidTokenError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

async def current_user(creds: HTTPAuthorizationCredentials = Depends(bearer), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, token_user_id(creds.credentials))
    if user is None:
        raise HTTPException(401, "User not found")
    return user

async def doc_role(db: AsyncSession, document_id: str, user_id: str) -> str | None:
    from app.models import Document, Permission
    doc = await db.get(Document, document_id)
    if not doc:
        return None
    if doc.owner_id == user_id:
        return "owner"
    result = await db.scalar(select(Permission.role).where(Permission.document_id == document_id, Permission.user_id == user_id))
    return result

def require_role(role, edit=False):
    if role is None or (edit and role not in ("owner", "editor")):
        raise HTTPException(403, "Document access denied")
