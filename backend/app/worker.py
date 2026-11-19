"""At-least-once Kafka outbox publisher + immutable object-storage snapshotter."""
import asyncio
import hashlib
import json
import logging
import os

import boto3
from aiokafka import AIOKafkaProducer
import redis.asyncio as redis
from sqlalchemy import select, func
from app.config import settings
from app.database import Session, init_db, engine
from app.models import Document, Outbox, Snapshot

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("syncspace.worker")

s3 = boto3.client("s3", endpoint_url=settings.s3_endpoint,
                  aws_access_key_id=settings.s3_access_key,
                  aws_secret_access_key=settings.s3_secret_key,
                  region_name="us-east-1")

def ensure_bucket():
    try:
        s3.head_bucket(Bucket=settings.s3_bucket)
    except Exception:
        s3.create_bucket(Bucket=settings.s3_bucket)

def put_snapshot(key: str, data: bytes):
    s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType="application/json")

async def publish_outbox(producer):
    async with Session() as db:
        rows = (await db.scalars(select(Outbox).where(Outbox.published.is_(False))
                                 .order_by(Outbox.document_id,Outbox.revision).limit(100))).all()
        for row in rows:
            # Kafka key ensures events of a document stay on one partition.
            # Repeated messages (crash after Kafka commit) dedupe by document_id+revision.
            await producer.send_and_wait("syncspace.document-operations",
                                         key=row.document_id.encode(),
                                         value=json.dumps(row.payload).encode())
            row.published = True
            await db.commit()
        return len(rows)

async def snapshot_documents(force_id: str | None = None):
    async with Session() as db:
        docs = (await db.scalars(select(Document).where(Document.id == force_id) if force_id else select(Document))).all()
        for doc in docs:
            last_revision = await db.scalar(select(func.max(Snapshot.revision)).where(Snapshot.document_id == doc.id)) or 0
            if doc.revision <= last_revision or (force_id != doc.id and doc.revision-last_revision < 20):
                continue
            data = json.dumps({"document_id":doc.id,"revision":doc.revision,"title":doc.title,
                               "content":doc.content},ensure_ascii=False).encode("utf-8")
            key = f"documents/{doc.id}/snapshot-{doc.revision}.json"
            digest = hashlib.sha256(data).hexdigest()
            await asyncio.to_thread(put_snapshot,key,data)
            db.add(Snapshot(document_id=doc.id, revision=doc.revision, object_key=key, sha256=digest))
            try:
                await db.commit()
                log.info("Saved snapshot %s revision %s",doc.id,doc.revision)
            except Exception:
                await db.rollback()  # Duplicate snapshots are harmless.

async def main():
    await init_db()
    r = redis.from_url(settings.redis_url,decode_responses=True)
    await asyncio.to_thread(ensure_bucket)
    producer = AIOKafkaProducer(bootstrap_servers=settings.kafka_bootstrap,
                                acks="all",enable_idempotence=True)
    while True:
        try:
            await producer.start()
            break
        except Exception as exc:
            log.warning("Kafka not ready (%s). Retrying...",exc)
            await asyncio.sleep(5)
    pubsub = r.pubsub()
    await pubsub.subscribe("syncspace:snapshot-requests")
    ticks = 0
    try:
        while True:
            try:
                await publish_outbox(producer)
                message = await pubsub.get_message(ignore_subscribe_messages=True,timeout=0.1)
                if message and message.get("data"):
                    await snapshot_documents(str(message["data"]))
                ticks += 1
                if ticks % 25 == 0:
                    await snapshot_documents()
            except Exception:
                log.exception("Worker cycle failed; will retry")
            await asyncio.sleep(1)
    finally:
        await pubsub.aclose()
        await r.aclose()
        await producer.stop()
        await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())
