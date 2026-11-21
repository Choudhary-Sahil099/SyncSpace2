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
from dataclasses import dataclass, field

import websockets

def http_json(method, url, token=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code} {method} {url}: {detail}") from exc


@dataclass
class User:
    name: str
    token: str

def provision(base, doc_id, owner, prefix, number):
    name = f"{prefix}_{number:03d}"
    response = http_json("POST", f"{base}/auth/register", payload={
        "username": name, "password": secrets.token_urlsafe(24)
    })
    token = response.get("token")
    if not token:
        raise RuntimeError(f"No token returned for {name}")
    http_json("POST", f"{base}/documents/{doc_id}/share", token=owner,
              payload={"username": name, "role": "editor"})
    return User(name, token)


def percentile(samples, fraction):
    values = sorted(samples)
    if not values:
        return None
    return values[min(len(values)-1, int((len(values)-1)*fraction))]


def format_latency(samples):
    if not samples:
        return "n/a"
    return f"median {statistics.median(samples):.0f}ms, p95 {percentile(samples, .95):.0f}ms, max {max(samples):.0f}ms"

@dataclass
class Peer:
    name: str
    ws: object
    content: str
    revision: int
    reader_task: object = None
    pending: dict = field(default_factory=dict)
    error: str = ""


async def connect(user, ws_base, doc_id, expected_role, timeout):
    uri = f"{ws_base}/ws/{doc_id}?token={urllib.parse.quote(user.token, safe='')}"
    ws = await websockets.connect(uri, open_timeout=timeout, close_timeout=3,
                                  max_size=32*1024*1024, ping_interval=20)
    try:
        deadline = time.perf_counter() + timeout
        while True:
            item = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(.1, deadline-time.perf_counter())))
            if item.get("type") == "init":
                if item.get("role") != expected_role:
                    raise RuntimeError(f"{user.name}: expected role {expected_role}, got {item.get('role')}")
                return Peer(user.name, ws, item["content"], int(item["revision"]))
            if item.get("type") == "error":
                raise RuntimeError(f"{user.name}: {item.get('message')}")
    except BaseException:
        await ws.close()
        raise

async def read_peer(peer, observer, send_times, observer_times, anomalies, sender_ack_ms):
    """Single reader per WebSocket: handles unsolicited operations and ACKs."""
    try:
        async for raw in peer.ws:
            received = time.perf_counter()
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "operation":
                rev = int(msg.get("revision", -1))
                if rev > peer.revision:
                    peer.revision = rev
                    peer.content = msg.get("content", peer.content)
                if observer:
                    opid = msg.get("operation_id")
                    sent_at = send_times.get(opid)
                    if sent_at is not None and opid not in observer_times:
                        observer_times[opid] = (received - sent_at)*1000
            elif kind == "ack":
                opid = msg.get("operation_id")
                f = peer.pending.pop(opid, None)
                if f is not None and not f.done():
                    sent_at = send_times.get(opid)
                    if sent_at is not None:
                        sender_ack_ms[opid] = (received-sent_at)*1000
                    rev = int(msg.get("current_revision", msg.get("revision", -1)))
                    if rev >= peer.revision:
                        peer.revision = rev
                        peer.content = msg.get("content", peer.content)
                    f.set_result(msg)
            elif kind == "error":
                opid = msg.get("operation_id")
                f = peer.pending.pop(opid, None)
                if f is not None and not f.done():
                    f.set_exception(RuntimeError(f"{peer.name}: {msg.get('message')}"))
                else:
                    anomalies.append(f"{peer.name}: unsolicited error {str(msg)[:160]}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        peer.error = f"reader failed: {exc}"
    finally:
        for future in peer.pending.values():
            if not future.done():
                future.set_exception(ConnectionError(f"{peer.name}: socket reader ended"))
        peer.pending.clear()

async def edit_loop(peer, index, run_tag, end_at, interval, timeout, send_times,
                    expected_markers, errors):
    count = 0
    loop = asyncio.get_running_loop()
    next_start = time.perf_counter()
    while time.perf_counter() < end_at:
        if peer.error or peer.ws.state.name != "OPEN":
            errors.append(f"{peer.name}: unexpected disconnect ({peer.error})")
            return
        # Use the latest known authoritative server snapshot; no overlapping
        # outstanding edits from the SAME editor (frontend-style discipline).
        count += 1
        marker = f"\n<S9D_{run_tag}_{index:03d}_{count:05d}>"
        opid = str(uuid.uuid4())
        payload = {"type": "edit", "operation_id": opid,
                   "base_revision": peer.revision, "pos": len(peer.content),
                   "delete": 0, "insert": marker}
        future = loop.create_future()
        peer.pending[opid] = future
        try:
            # Timestamp just before enqueuing the send (local mono clock).
            send_times[opid] = time.perf_counter()
            await peer.ws.send(json.dumps(payload))
            expected_markers[opid] = marker
            await asyncio.wait_for(future, timeout=timeout)
        except Exception as exc:
            peer.pending.pop(opid, None)
            errors.append(f"{peer.name} operation {count}: {type(exc).__name__}: {exc}")
            return
        next_start += interval
        remaining = next_start - time.perf_counter()
        if remaining > 0:
            await asyncio.sleep(remaining)
        else:
            next_start = time.perf_counter()

async def one_stage(args, users, owner, tag, n):
    doc_url = f"{args.base}/documents/{args.document_id}"
    before = await asyncio.to_thread(http_json, "GET", doc_url, owner)
    base_rev = int(before["revision"])
    base_content = before["content"]
    peers = []
    send_times, observer_times, sender_ack_ms, expected_markers = {}, {}, {}, {}
    anomalies = []
    try:
        # Owner's separate socket acts as a passive independent receiving client.
        peers.append(await connect(User("independent_owner_observer", owner), args.ws_base,
                                   args.document_id, "owner", args.timeout))
        for batch_start in range(0, n, args.batch):
            batch = await asyncio.gather(*[
                connect(u, args.ws_base, args.document_id, "editor", args.timeout)
                for u in users[batch_start:batch_start+args.batch]
            ])
            peers.extend(batch)
        if any(p.revision != base_rev or p.content != base_content for p in peers):
            raise RuntimeError("Init content/revision differs across peers; use idle test document")
        for i, peer in enumerate(peers):
            peer.reader_task = asyncio.create_task(read_peer(
                peer, i == 0, send_times, observer_times, anomalies, sender_ack_ms))

        print(f"\nStage: {n} editors, duration={args.duration}s, interval={args.interval}s per editor")
        print(f"Document starting revision={base_rev}; observer and editors initialized consistently")
        start = time.perf_counter()
        end_at = start + args.duration
        tasks = [asyncio.create_task(edit_loop(
            peer, index, tag, end_at, args.interval, args.timeout,
            send_times, expected_markers, anomalies))
            for index, peer in enumerate(peers[1:], start=1)]
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - start
        # Allow queued broadcasts to reach the observer and all peers.
        grace_until = time.perf_counter() + args.settle
        while time.perf_counter() < grace_until:
            if len(observer_times) >= len(sender_ack_ms) == len(expected_markers):
                break
            await asyncio.sleep(.1)
        after = await asyncio.to_thread(http_json, "GET", doc_url, owner)
        final_content = after["content"]
        final_rev = int(after["revision"])
        markers = list(expected_markers.values())
        duplicates = sum(final_content.count(marker) != 1 for marker in markers)
        missing_observer = set(expected_markers) - set(observer_times)
        acknowledged = len(sender_ack_ms)
        observed = len(observer_times)
        throughput = acknowledged / elapsed if elapsed else 0
        sync_samples = [observer_times[op] for op in expected_markers if op in observer_times]
        ack_samples = [sender_ack_ms[op] for op in expected_markers if op in sender_ack_ms]
        revision_ok = final_rev == base_rev + len(expected_markers)
        original_ok = final_content.startswith(base_content)
        # Independent observer content and revision must match the REST source.
        converged = (peers[0].revision == final_rev and peers[0].content == final_content)
        # Editors can lag behind at finish (they may receive their ACK after
        # another remote commit); do not count that as permanent divergence.
        convergence_until = time.perf_counter() + args.settle
        while time.perf_counter() < convergence_until and any(
                p.revision != final_rev or p.content != final_content for p in peers):
            await asyncio.sleep(.1)
        all_converged = all(p.revision == final_rev and p.content == final_content for p in peers)

        print(f"Attempted operations: {len(expected_markers)}; ACKs: {acknowledged}; observer received: {observed}")
        print(f"Accepted edit throughput: {throughput:.1f} ops/sec (wall-time {elapsed:.1f}s)")
        print(f"ACK latency: {format_latency(ack_samples)}")
        print(f"EDIT PROPAGATION (send -> separate observer): {format_latency(sync_samples)}")
        print(f"Document revision: {base_rev} -> {final_rev} (expected {base_rev+len(expected_markers)})")
        print(f"Unique markers each exactly once: {len(markers)-duplicates}/{len(markers)}")
        print(f"All {len(peers)} connected clients converged to REST content/revision: {all_converged}")
        for err in anomalies[:6]:
            print("ERROR:", err)
        passed = (not anomalies and acknowledged == len(expected_markers)
                  and observed == len(expected_markers) and not missing_observer
                  and revision_ok and duplicates == 0 and original_ok and converged
                  and all_converged)
        print("STAGE RESULT:", "PASS" if passed else "FAIL — do not use latency for resume")
        return passed
    finally:
        await asyncio.gather(*(p.ws.close() for p in peers), return_exceptions=True)
        tasks = [p.reader_task for p in peers if p.reader_task is not None]
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

async def main(args):
    if not args.levels or any(x < 1 or x > 100 for x in args.levels) or sorted(set(args.levels)) != args.levels:
        raise SystemExit("--levels must be unique ascending integers from 1 through 100")
    if args.interval < 0.2 or args.duration < 5 or args.batch < 1 or args.timeout < 1 or args.settle < 1:
        raise SystemExit("Use interval >=0.2, duration >=5, batch >=1, timeout >=1, settle >=1")
    owner = os.getenv("SYNCSPACE_TEST_TOKEN", "").strip() or getpass.getpass("Owner JWT (hidden): ").strip()
    if not owner:
        raise SystemExit("Owner JWT missing")
    args.base = args.base.rstrip("/")
    args.ws_base = args.ws_base.rstrip("/")
    info = await asyncio.to_thread(http_json, "GET", f"{args.base}/documents/{args.document_id}", owner)
    if info.get("role") != "owner":
        raise SystemExit(f"Must use document OWNER token, got role {info.get('role')!r}")
    print(f"Verified document: revision {info['revision']}, characters {len(info['content'])}")
    prefix = "s9d"+secrets.token_hex(4)
    print(f"WARNING: creates {max(args.levels)} PERMANENT accounts with editor permission and MODIFIES the document.")
    print(f"Run ID: {prefix}. Use only an isolated/disposable test document. No production data.")
    print("Note: propagation is measured by an independent WebSocket observer, not browser rendering.")
    if input("Type SUSTAIN to proceed: ").strip() != "SUSTAIN":
        print("Cancelled")
        return
    users = []
    for i in range(1, max(args.levels)+1):
        try:
            users.append(await asyncio.to_thread(provision, args.base, args.document_id, owner, prefix, i))
        except Exception as exc:
            print(f"Provisioning stopped at {i}: {exc}")
            print(f"Provisioned {len(users)} accounts with prefix {prefix}")
            return
        if i % 10 == 0 or i == max(args.levels):
            print(f"Provisioned and shared {i}/{max(args.levels)}")
    for n in args.levels:
        if not await one_stage(args, users, owner, prefix+f"_n{n}", n):
            print("Stopping after failed stage")
            break
    print("Done. Test accounts, permissions, and markers remain persisted in your local database.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SyncSpace Test 9D sustained editor-to-observer sync benchmark")
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--ws-base", default="ws://localhost:8000")
    parser.add_argument("--levels", nargs="+", type=int, default=[10])
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--interval", type=float, default=2.0, help="per-editor send interval in seconds")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--settle", type=float, default=10)
    parser.add_argument("--batch", type=int, default=10)
    asyncio.run(main(parser.parse_args()))
