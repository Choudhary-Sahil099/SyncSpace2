import argparse
import asyncio
import getpass
import json
import os
import secrets
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

import websockets


def http_json(method, url, token=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:350]
        raise RuntimeError(f"HTTP {exc.code} for {method} {url}: {detail}") from exc

@dataclass
class Client:
    username: str
    token: str


def provision(base, document_id, owner_token, prefix, index):
    username = f"{prefix}_{index:03d}"
    password = secrets.token_urlsafe(24)
    result = http_json("POST", f"{base}/auth/register", payload={"username": username, "password": password})
    token = result.get("token")
    if not token:
        raise RuntimeError(f"Registration did not return token for {username}: {str(result)[:200]}")
    http_json("POST", f"{base}/documents/{document_id}/share", token=owner_token,
              payload={"username": username, "role": "editor"})
    return Client(username, token)

async def open_client(client, ws_base, doc_id, timeout):
    address = f"{ws_base}/ws/{doc_id}?token={urllib.parse.quote(client.token, safe='')}"
    ws = await websockets.connect(address, open_timeout=timeout, close_timeout=3,
                                  max_size=32 * 1024 * 1024, ping_interval=20)
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(.1, deadline - asyncio.get_running_loop().time())))
            if event.get("type") == "init":
                if event.get("role") != "editor":
                    raise RuntimeError(f"Unexpected role {event.get('role')} for {client.username}")
                return ws, event
            if event.get("type") == "error":
                raise RuntimeError(f"WS error for {client.username}: {event.get('message')}")
    except BaseException:
        await ws.close()
        raise

async def edit_and_ack(ws, op_id, revision, pos, marker, start_event, timeout):
    await start_event.wait()
    t0 = time.perf_counter()
    await ws.send(json.dumps({"type": "edit", "operation_id": op_id,
                              "base_revision": revision, "pos": pos,
                              "delete": 0, "insert": marker}))
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"ACK timeout for operation {op_id}")
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if msg.get("type") == "ack" and msg.get("operation_id") == op_id:
            return (time.perf_counter() - t0) * 1000, msg
        if msg.get("type") == "error" and msg.get("operation_id") == op_id:
            raise RuntimeError(f"Edit rejected: {msg.get('message')}")
        # Other clients' operation broadcasts, cursors and presence are expected.

async def test_stage(clients, n, base, ws_base, doc_id, owner_token, prefix, timeout):
    before = await asyncio.to_thread(http_json, "GET", f"{base}/documents/{doc_id}", owner_token)
    initial_content = before["content"]
    revision = int(before["revision"])
    if not isinstance(initial_content, str):
        raise RuntimeError("REST GET did not return string content")
    sockets = []
    tasks = []
    stage_name = f"{prefix}_n{n}"
    markers = [f"\n<{stage_name}_{i+1:03d}>" for i in range(n)]
    start_event = asyncio.Event()
    try:
        # Start all users from the same base revision and same end position to
        # intentionally create a concurrent insertion conflict.
        open_results = await asyncio.gather(
            *(open_client(c, ws_base, doc_id, timeout) for c in clients[:n]),
            return_exceptions=True,
        )
        failures = []
        for client, result in zip(clients[:n], open_results):
            if isinstance(result, BaseException):
                failures.append(f"{client.username}: {result}")
            else:
                sockets.append(result[0])
                if result[1].get("revision") != revision or result[1].get("content") != initial_content:
                    failures.append(f"{client.username}: initialization document mismatch")
        if failures:
            print("Initialization issues:", *failures[:5], sep="\n  ")
            return False
        print(f"\nStage: {n} distinct editors | same base revision={revision}, same insertion position={len(initial_content)}")
        # Each socket has one reader: edit_and_ack receives and skips broadcasts.
        for idx, ws in enumerate(sockets):
            tasks.append(asyncio.create_task(edit_and_ack(ws, str(uuid.uuid4()), revision,
                                                        len(initial_content), markers[idx], start_event, timeout)))
        start_event.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        ok_acks = []
        failures = []
        for idx, result in enumerate(results):
            if isinstance(result, BaseException):
                failures.append(f"{clients[idx].username}: {result}")
            else:
                delay, ack = result
                ok_acks.append((delay, ack))
        print(f"ACKs: {len(ok_acks)}/{n}")
        if ok_acks:
            ordered = sorted(x[0] for x in ok_acks)
            p95 = ordered[int((len(ordered)-1)*.95)]
            print(f"ACK latency: median {statistics.median(ordered):.0f}ms, p95 {p95:.0f}ms")
        for error in failures[:5]:
            print("Failure:", error)
        # REST query after all acknowledgments verifies durable state.
        after = await asyncio.to_thread(http_json, "GET", f"{base}/documents/{doc_id}", owner_token)
        final_content = after["content"]
        final_revision = int(after["revision"])
        counts = [final_content.count(marker) for marker in markers]
        content_ok = all(count == 1 for count in counts)
        expected_revision = revision + n
        print(f"Revision: {revision} -> {final_revision} (expected {expected_revision})")
        print(f"Unique markers present exactly once: {sum(c == 1 for c in counts)}/{n}")
        print(f"Document retained original text: {initial_content in final_content}")
        passed = (len(ok_acks) == n and final_revision == expected_revision
                  and content_ok and initial_content in final_content)
        print("STAGE RESULT:", "PASS" if passed else "FAIL — stop and inspect")
        return passed
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)

async def main(args):
    owner = os.getenv("SYNCSPACE_TEST_TOKEN", "").strip() or getpass.getpass("Owner JWT (hidden): ").strip()
    if not owner:
        raise SystemExit("Missing owner token")
    if not args.levels or any(x < 1 or x > 100 for x in args.levels) or sorted(set(args.levels)) != args.levels:
        raise SystemExit("Levels must be unique ascending integers between 1 and 100")
    doc = http_json("GET", f"{args.base}/documents/{args.document_id}", owner)
    if doc.get("role") != "owner":
        raise SystemExit(f"Expected owner JWT; actual role={doc.get('role')!r}")
    print("Verified test document; revision:", doc["revision"], "characters:", len(doc["content"]))
    maximum = max(args.levels)
    prefix = "edit" + secrets.token_hex(4)
    print(f"WARNING: This WILL CREATE {maximum} persistent editor accounts and WILL MODIFY the document.")
    print(f"Account prefix: {prefix}; test markers will be appended to the document.")
    if input("Type EDIT to proceed: ").strip() != "EDIT":
        raise SystemExit("Cancelled")
    clients = []
    for i in range(1, maximum+1):
        try:
            client = await asyncio.to_thread(provision, args.base, args.document_id, owner, prefix, i)
        except Exception as exc:
            print(f"Stopped during registration {i}: {exc}; {len(clients)} accounts already provisioned")
            return
        clients.append(client)
        if i % 10 == 0 or i == maximum:
            print(f"Provisioned editor accounts: {i}/{maximum}")
    for n in args.levels:
        success = await test_stage(clients, n, args.base, args.ws_base, args.document_id, owner, prefix, args.timeout)
        if not success:
            print("Stopping after failure. Preserve document and inspect logs.")
            break
    print("Done. Editor permissions and appended markers remain in the local database.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SyncSpace distinct-editor concurrent edit test")
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--ws-base", default="ws://localhost:8000")
    parser.add_argument("--levels", nargs="+", type=int, default=[5, 10, 25, 50, 100])
    parser.add_argument("--timeout", type=float, default=30)
    asyncio.run(main(parser.parse_args()))
