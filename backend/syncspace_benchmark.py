import argparse
import asyncio
import json
import math
import statistics
import time
import uuid

import httpx
from websockets.asyncio.client import connect


def percentile(values: list[float], p: float) -> float:
    sorted_values = sorted(values)
    index = (len(sorted_values) - 1) * p / 100
    lower, upper = math.floor(index), math.ceil(index)
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (index - lower)


async def receive_matching(ws, event_type: str, op_id: str, timeout: float):
    async with asyncio.timeout(timeout):
        while True:
            message = json.loads(await ws.recv())
            if message.get("type") == "error":
                raise RuntimeError(f"Server rejected edit: {message.get('message')}")
            if message.get("type") == event_type and message.get("operation_id") == op_id:
                return message, time.perf_counter()
async def main(args):
    if args.users < 2:
        raise ValueError("Use at least 2 users to measure cross-client delivery")
    if args.operations < 1:
        raise ValueError("Use at least 1 operation")
    label = uuid.uuid4().hex[:10]
    base_http = args.url.rstrip("/")
    ws_base = base_http.replace("https://", "wss://").replace("http://", "ws://", 1)
    headers = []
    async with httpx.AsyncClient(base_url=base_http, timeout=args.timeout) as http:
        for index in range(args.users):
            username = f"bench_{label}_{index}"
            response = await http.post("/auth/register", json={"username": username, "password": "BenchmarkOnly-Strong-Password-2026"})
            response.raise_for_status()
            token = response.json()["token"]
            headers.append({"Authorization": f"Bearer {token}"})
        response = await http.post("/documents", json={"title": f"Benchmark {label}"}, headers=headers[0])
        response.raise_for_status()
        document = response.json()
        document_id = document["id"]
        for index in range(1, args.users):
            response = await http.post(f"/documents/{document_id}/share", headers=headers[0], json={"username": f"bench_{label}_{index}", "role": "editor"})
            response.raise_for_status()

        sockets = []
        try:
            for index, header in enumerate(headers):
                token = header["Authorization"].split(" ", 1)[1]
                ws = await connect(f"{ws_base}/ws/{document_id}?token={token}", open_timeout=args.timeout, max_size=4*1024*1024)
                init = json.loads(await asyncio.wait_for(ws.recv(), timeout=args.timeout))
                if init.get("type") != "init":
                    raise RuntimeError(f"Unexpected initial WebSocket message: {init}")
                sockets.append(ws)
            content = document["content"]
            revision = document["revision"]
            ack_times = []
            peer_times = []
            iterations = 0
            print(f"Connected {args.users} WebSocket clients; document: {document_id}")
            print("Mode: serialized writes, every socket connected; measures ACK and peer fan-out, NOT simultaneous-write throughput")
            for step in range(args.operations):
                sender = step % args.users
                operation_id = str(uuid.uuid4())
                insert = f"{step}|"
                payload = {"type": "edit", "operation_id": operation_id, "base_revision": revision, "pos": len(content), "delete": 0, "insert": insert}
                started = time.perf_counter()
                await sockets[sender].send(json.dumps(payload))
                arrivals = await asyncio.gather(*[
                    receive_matching(ws, "ack" if index == sender else "operation", operation_id, args.timeout)
                    for index, ws in enumerate(sockets)
                ])
                ack, ack_at = arrivals[sender]
                ack_times.append((ack_at - started)*1000)
                peer_times.append((max(at for index, (_, at) in enumerate(arrivals) if index != sender) - started)*1000)
                content += insert
                revision += 1
                if ack.get("revision") != revision or ack.get("content") != content:
                    raise RuntimeError(f"Incorrect ACK at step {step}: expected revision {revision}")
                if any(event.get("revision") != revision or event.get("content") != content for index, (event, _) in enumerate(arrivals) if index != sender):
                    raise RuntimeError(f"Peer divergence detected at step {step}")
                iterations += 1
            check = await http.get(f"/documents/{document_id}", headers=headers[0])
            check.raise_for_status()
            final = check.json()
            if final["revision"] != revision or final["content"] != content:
                raise RuntimeError("Final persisted document differs from expected content")
            def summarize(name, values):
                print(f"{name}: p50={percentile(values,50):.1f} ms, p95={percentile(values,95):.1f} ms, max={max(values):.1f} ms")
            print(f"PASS: {iterations} edit ACKs, {(args.users-1)*iterations} peer deliveries, final revision {revision}, persisted content verified")
            summarize("Server ACK", ack_times)
            summarize("Slowest peer delivery per edit", peer_times)
            print("Notes: test runs on localhost; no network/geographic latency. Connected clients are not simultaneous writers.")
        finally:
            await asyncio.gather(*(ws.close() for ws in sockets), return_exceptions=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=5)
    parser.add_argument("--operations", type=int, default=100)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--timeout", type=float, default=20)
    args = parser.parse_args()
    asyncio.run(main(args))