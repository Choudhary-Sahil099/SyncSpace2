import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from uuid import uuid4

import redis.asyncio as redis
from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.database import Session, engine, get_db, init_db
from app.models import User, Document, Permission, Operation, Outbox, Snapshot
from app.security import passwords, make_token, token_user_id, current_user, doc_role, require_role
from app.ot import Edit, apply_actions, transform_edit

logger = logging.getLogger("syncspace")

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()   # Developer MVP only. Production: Alembic migrations.
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True)
    await app.state.redis.ping()
    yield
    await app.state.redis.aclose()
    await engine.dispose()


app = FastAPI(
    title="SyncSpace API",
    version="0.1.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://10.250.219.92:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class AuthRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=128)

class CreateDoc(BaseModel):
    title: str = Field(default="Untitled document", min_length=1, max_length=160)

class RenameDoc(BaseModel):
    title: str = Field(min_length=1, max_length=160)

class ShareDoc(BaseModel):
    username: str
    role: str = "editor"

def doc_response(doc: Document, role: str):
    return {"id":doc.id, "title":doc.title, "content":doc.content,
            "revision":doc.revision, "owner_id":doc.owner_id,
            "role":role, "updated_at":doc.updated_at.isoformat()}

@app.get("/health")
async def health():
    return {"ok":True, "service":"SyncSpace"}

@app.post("/auth/register", status_code=201)
async def register(body: AuthRequest, db: AsyncSession = Depends(get_db)):
    name = body.username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{3,40}", name):
        raise HTTPException(400, "Username must contain 3-40 lowercase letters, numbers or underscores")
    if await db.scalar(select(User.id).where(User.username == name)):
        raise HTTPException(409, "Username already taken")
    user = User(username=name, password_hash=passwords.hash(body.password))
    db.add(user)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(409, "Username already taken")
    return {"token":make_token(user), "user":{"id":user.id,"username":user.username}}

@app.post("/auth/login")
async def login(body: AuthRequest, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if user is None or not passwords.verify(body.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password")
    return {"token":make_token(user), "user":{"id":user.id,"username":user.username}}

@app.get("/me")
async def me(user: User = Depends(current_user)):
    return {"id":user.id,"username":user.username}

@app.get("/documents")
async def documents(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    shared = select(Permission.document_id).where(Permission.user_id == user.id)
    result = await db.scalars(select(Document).where(or_(Document.owner_id == user.id, Document.id.in_(shared))).order_by(Document.updated_at.desc()))
    docs = result.all()
    return [{"id":d.id,"title":d.title,"revision":d.revision,"updated_at":d.updated_at.isoformat(),"owner":d.owner_id==user.id} for d in docs]

@app.post("/documents", status_code=201)
async def create_document(body: CreateDoc, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    doc = Document(title=body.title.strip(), owner_id=user.id, content="", revision=0)
    db.add(doc)
    await db.commit()
    return doc_response(doc,"owner")

@app.get("/documents/{document_id}")
async def get_document(document_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    role = await doc_role(db, document_id, user.id)
    require_role(role)
    return doc_response(await db.get(Document, document_id), role)

@app.patch("/documents/{document_id}")
async def rename_document(document_id: str, body: RenameDoc, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    role = await doc_role(db,document_id,user.id)
    require_role(role,edit=True)
    doc = await db.get(Document,document_id)
    doc.title = body.title.strip()
    doc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return doc_response(doc,role)

@app.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    doc = await db.get(Document, document_id)
    if not doc or doc.owner_id != user.id:
        raise HTTPException(403, "Only the owner can delete")
    # Explicitly delete child rows to avoid relying on DB-side ORM cascades.
    from sqlalchemy import delete
    for table in (Permission, Operation, Snapshot, Outbox):
        await db.execute(delete(table).where(table.document_id == document_id))
    await db.delete(doc)
    await db.commit()

@app.post("/documents/{document_id}/share")
async def share(document_id: str, body: ShareDoc, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    doc = await db.get(Document,document_id)
    if not doc or doc.owner_id != user.id:
        raise HTTPException(403, "Only owner can share")
    if body.role not in ("editor","viewer"):
        raise HTTPException(400,"Role must be editor or viewer")
    target = await db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if not target or target.id == user.id:
        raise HTTPException(404, "Target user not found")
    permission = await db.scalar(select(Permission).where(Permission.document_id == document_id, Permission.user_id == target.id))
    if permission:
        permission.role = body.role
    else:
        db.add(Permission(document_id=document_id,user_id=target.id,role=body.role))
    await db.commit()
    return {"username":target.username,"role":body.role}

@app.get("/documents/{document_id}/history")
async def history(document_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    require_role(await doc_role(db,document_id,user.id))
    items = (await db.scalars(select(Snapshot).where(Snapshot.document_id == document_id).order_by(Snapshot.revision.desc()).limit(50))).all()
    return [{"revision":s.revision,"created_at":s.created_at.isoformat()} for s in items]

@app.post("/documents/{document_id}/snapshot")
async def snapshot_request(document_id: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    require_role(await doc_role(db,document_id,user.id),edit=True)
    await app.state.redis.publish("syncspace:snapshot-requests", document_id)
    return {"queued":True}

async def handle_edit(document_id: str, user_id: str, data: dict) -> dict:
    """Postgres row lock is the single per-document revision sequencer.

    Commit the edit + outbox entry atomically, then broadcast over Redis.
    A separate worker durably sends outbox events to Kafka (at least once).
    """
    operation_id = str(data.get("operation_id", ""))
    if not 1 <= len(operation_id) <= 90:
        raise ValueError("operation_id required")
    base = int(data["base_revision"])
    pos = int(data["pos"])
    delete = int(data["delete"])
    insert = data.get("insert", "")
    if not isinstance(insert,str) or len(insert) > 100_000 or pos < 0 or delete < 0:
        raise ValueError("Invalid edit")
    async with Session() as db:
        async with db.begin():
            doc = await db.scalar(select(Document).where(Document.id == document_id).with_for_update())
            if not doc:
                raise ValueError("Document missing")
            role = await doc_role(db,document_id,user_id)
            if role not in ("owner","editor"):
                raise PermissionError("Edit permission denied")
            duplicate = await db.scalar(select(Operation).where(Operation.document_id == document_id, Operation.operation_id == operation_id))
            if duplicate:
                return {"type":"ack", "operation_id":operation_id, "revision":duplicate.revision,
                        "content":doc.content, "current_revision":doc.revision}
            if base > doc.revision or base < 0:
                raise ValueError("Invalid base revision")
            if doc.revision - base > 10000:
                raise ValueError("Client too far behind; resynchronize")
            rows = (await db.scalars(select(Operation).where(Operation.document_id == document_id, Operation.revision > base).order_by(Operation.revision))).all()
            if len(rows) != doc.revision - base:
                raise ValueError("Missing operation history; resynchronize")
            # Rebuild the base document from logged committed operations in reverse
            # is unnecessary for a simple insert if its position is later clamped,
            # but we validate against historical length to prevent bad delete ranges.
            # Compute base length from current length and subsequent edit size changes.
            base_length = len(doc.content)
            for row in rows:
                for a in row.actions:
                    base_length += a["length"] if a["type"] == "delete" else -len(a["text"])
            if pos > base_length or pos + delete > base_length:
                raise ValueError("Edit range is outside the base document")
            edits = transform_edit(Edit(pos,delete,insert),[row.actions for row in rows])
            updated = apply_actions(doc.content,edits)
            if len(updated) > 1_000_000:
                raise ValueError("Document exceeds 1 million characters")
            doc.content = updated
            doc.revision += 1
            doc.updated_at = datetime.now(timezone.utc)
            operation = Operation(document_id=document_id, actor_id=user_id, operation_id=operation_id,
                                  revision=doc.revision, base_revision=base, actions=edits)
            db.add(operation)
            event = {"type":"operation", "document_id":document_id, "actor_id":user_id,
                     "operation_id":operation_id, "revision":doc.revision, "actions":edits,
                     "content":updated}
            db.add(Outbox(document_id=document_id,revision=doc.revision,payload=event))
            ack = {"type":"ack", "operation_id":operation_id, "revision":doc.revision,
                   "current_revision":doc.revision, "content":updated}
    await app.state.redis.publish(f"syncspace:doc:{document_id}", json.dumps(event))
    return ack

@app.websocket("/ws/{document_id}")
async def websocket_document(socket: WebSocket, document_id: str):
    token = socket.query_params.get("token", "")
    try:
        user_id = token_user_id(token)
        async with Session() as db:
            user = await db.get(User,user_id)
            role = await doc_role(db,document_id,user_id)
            if not user or not role:
                await socket.close(code=1008)
                return
            username = user.username
    except HTTPException:
        await socket.close(code=1008)
        return
    await socket.accept()
    r = app.state.redis
    connection_id = str(uuid4())
    presence_key = f"syncspace:presence:{document_id}:{connection_id}"
    channel = f"syncspace:doc:{document_id}"
    pubsub = r.pubsub()
    await pubsub.subscribe(channel)
    await r.set(presence_key, username, ex=35)
    cursor_key = f"syncspace:cursor:{document_id}:{connection_id}"
    async def redis_listener():
        async for message in pubsub.listen():
            if message.get("type") == "message":
                await socket.send_text(message["data"])
    task = asyncio.create_task(redis_listener())
    try:
        async with Session() as db:
            doc = await db.get(Document,document_id)
            await socket.send_json({"type":"init", "content":doc.content, "revision":doc.revision,
                                    "role":role,"username":username, "connection_id": connection_id})
            # New collaborators immediately see previously published cursors.
            existing = await r.keys(f"syncspace:cursor:{document_id}:*")
            stored = await r.mget(existing) if existing else []
            cursors = []
            for value in stored:
                if value:
                    try:
                        cursors.append(json.loads(value))
                    except (TypeError, ValueError):
                        pass
            await socket.send_json({"type":"cursors", "cursors":cursors})
        while True:
            data = await socket.receive_json()
            kind = data.get("type")
            if kind == "edit":
                try:
                    ack = await handle_edit(document_id,user_id,data)
                    await socket.send_json(ack)
                except (ValueError,KeyError,TypeError,PermissionError) as exc:
                    await socket.send_json({"type":"error","message":str(exc),"operation_id":data.get("operation_id")})
            elif kind == "cursor":
                # Positions are Unicode code-point offsets, not UTF-16 offsets.
                # Cap client-provided indexes to keep messages small and valid.
                try:
                    position = max(0, min(1_000_000, int(data.get("position", 0))))
                    anchor = max(0, min(1_000_000, int(data.get("anchor", position))))
                    cursor_revision = int(data.get("revision", 0))
                except (ValueError, TypeError):
                    continue
                state = {"type":"cursor", "username":username,
                         "connection_id":connection_id, "position":position,
                         "anchor":anchor, "revision":cursor_revision}
                await r.set(cursor_key, json.dumps(state), ex=35)
                await r.publish(channel, json.dumps(state))
            elif kind == "ping":
                await r.expire(presence_key,35)
                await r.expire(cursor_key,35)
                await socket.send_json({"type":"pong"})
            elif kind == "presence":
                users = await r.keys(f"syncspace:presence:{document_id}:*")
                names = await r.mget(users) if users else []
                await socket.send_json({"type":"presence","users":sorted(set(filter(None,names)))})
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await r.delete(presence_key, cursor_key)
        await r.publish(channel, json.dumps({"type":"cursor_leave", "connection_id":connection_id}))
