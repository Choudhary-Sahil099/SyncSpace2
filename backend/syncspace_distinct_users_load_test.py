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
from dataclasses import dataclass

import websockets


def post_json(url, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"HTTP {e.code} {url}: {detail}") from e


def get_json(url, token):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {url}: {e.read().decode(errors='replace')[:250]}") from e

@dataclass
class Client:
    username: str
    token: str


def provision_one(base, doc_id, owner_token, prefix, index):
    username = f"{prefix}_{index:03d}"
    password = secrets.token_urlsafe(24)
    _, auth = post_json(f"{base}/auth/register", {"username": username, "password": password})
    token = auth.get("token")
    if not token:
        raise RuntimeError(f"Registration did not return token for {username}: {str(auth)[:200]}")
    post_json(f"{base}/documents/{doc_id}/share", {"username": username, "role": "viewer"}, owner_token)
    return Client(username, token)


async def join_one(client, ws_base, doc_id, timeout):
    uri = f"{ws_base}/ws/{doc_id}?token={urllib.parse.quote(client.token, safe='')}"
    start = time.perf_counter()
    sock = await websockets.connect(uri, open_timeout=timeout, close_timeout=3, max_size=32*1024*1024, ping_interval=20)
    try:
        while True:
            msg = json.loads(await asyncio.wait_for(sock.recv(), timeout=timeout))
            if msg.get("type") == "init":
                if msg.get("role") != "viewer":
                    raise RuntimeError(f"Unexpected role for {client.username}: {msg.get('role')}")
                return sock, (time.perf_counter() - start) * 1000
            if msg.get("type") == "error":
                raise RuntimeError(f"WS error for {client.username}: {msg.get('message')}")
    except Exception:
        await sock.close()
        raise

async def run_stage(clients, n, ws_base, doc_id, hold, timeout, batch):
    sockets = []
    latencies = []
    failures = []
    t0 = time.perf_counter()
    for offset in range(0, n, batch):
        subset = clients[offset:min(offset+batch, n)]
        results = await asyncio.gather(*(join_one(c, ws_base, doc_id, timeout) for c in subset), return_exceptions=True)
        for c, result in zip(subset, results):
            if isinstance(result, BaseException):
                failures.append(f"{c.username}: {str(result)[:150]}")
            else:
                sock, latency = result
                sockets.append(sock)
                latencies.append(latency)
    duration = time.perf_counter() - t0
    print(f"\nTarget DISTINCT users: {n}")
    print(f"Initialized: {len(sockets)}/{n} ({100*len(sockets)/n:.1f}%)")
    print(f"Connection round duration: {duration:.1f}s")
    if latencies:
        ordered = sorted(latencies)
        p95 = ordered[max(0, int((len(ordered)-1)*0.95))]
        print(f"Init latency: median {statistics.median(latencies):.0f} ms; p95 {p95:.0f} ms")
    for error in failures[:5]:
        print("Connection failure:", error)
    print(f"Holding for {hold}s without editing...")
    await asyncio.sleep(hold)
    alive = sum(1 for s in sockets if s.state.name == 'OPEN')
    print(f"Sockets still OPEN: {alive}/{len(sockets)}")
    await asyncio.gather(*(s.close() for s in sockets), return_exceptions=True)
    return len(sockets) == n and alive == n


async def main(args):
    owner_token = os.getenv("SYNCSPACE_TEST_TOKEN", "").strip() or getpass.getpass("Owner JWT (hidden): ").strip()
    if not owner_token:
        raise SystemExit("Missing owner token")
    base = args.base.rstrip("/")
    ws_base = args.ws_base.rstrip("/")
    doc = get_json(f"{base}/documents/{args.document_id}", owner_token)
    if doc.get("role") != "owner":
        raise SystemExit(f"Use OWNER's JWT; document role is {doc.get('role')!r}")
    print("Document verified; revision:", doc.get("revision"), "characters:", len(doc.get("content", "")))
    maximum = max(args.levels)
    if maximum > 100:
        raise SystemExit("This test intentionally caps distinct-account provisioning at 100")
    run_id = secrets.token_hex(4)
    prefix = f"load{run_id}"
    print(f"Will CREATE {maximum} permanent test accounts and share the document with them as viewers.")
    print(f"Username prefix: {prefix}; all clients are read-only; no edits will be sent.")
    if input("Type CREATE to proceed: ").strip() != "CREATE":
        raise SystemExit("Cancelled")
    clients = []
    for i in range(maximum):
        try:
            client = await asyncio.to_thread(provision_one, base, args.document_id, owner_token, prefix, i+1)
        except Exception as exc:
            print(f"Provisioning stopped at account {i+1}: {exc}")
            print(f"Already provisioned: {len(clients)} accounts, prefix {prefix}")
            return
        clients.append(client)
        if len(clients) % 10 == 0:
            print(f"Provisioned + invited {len(clients)}/{maximum}")
    for n in args.levels:
        ok = await run_stage(clients, n, ws_base, args.document_id, args.hold, args.timeout, args.batch)
        if not ok:
            print("Stopping stages after failure; inspect application and Docker logs.")
            break
    print("Done. Test accounts remain in the local database. Verify document content unchanged.")



if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Load test unique authenticated SyncSpace users"
    )
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--ws-base", default="ws://localhost:8000")
    parser.add_argument("--levels", nargs="+", type=int, default=[5, 25, 50, 100])
    parser.add_argument("--hold", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=15)
    parser.add_argument("--batch", type=int, default=10)

    args = parser.parse_args()
    asyncio.run(main(args))
