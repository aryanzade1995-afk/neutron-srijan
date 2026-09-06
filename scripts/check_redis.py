"""Verify a Redis server and that MuleTrace can use it.

Run this straight after installing or pointing at a Redis:

    .venv/Scripts/python.exe scripts/check_redis.py

It connects, exercises exactly the operations the app relies on, cleans up after
itself, and says plainly whether the app will use Redis or fall back.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import store  # noqa: E402

OK = "  [ok]  "
BAD = "  [FAIL]"


def main() -> int:
    print(f"Connecting to {store.REDIS_URL}\n")

    client = store.connect()
    if client.backend != "redis":
        print(f"{BAD} No Redis answered — the app will use the in-process store.")
        print("\n  The app still works; it just loses durability across restarts")
        print("  and cannot share state between workers.")
        print("\n  Start one, then re-run this:")
        print("    Memurai : winget install Memurai.MemuraiDeveloper   (elevated)")
        print("    Docker  : docker run -d -p 6379:6379 redis:7-alpine")
        print("    Remote  : set MULETRACE_REDIS_URL=redis://user:pass@host:port")
        return 1

    print(f"{OK} Connected. {client.info()}\n")

    prefix = f"mt:check:{int(time.time() * 1000)}:"
    failures = 0

    def check(label: str, got, want) -> None:
        nonlocal failures
        if got == want:
            print(f"{OK} {label}")
        else:
            failures += 1
            print(f"{BAD} {label} — expected {want!r}, got {got!r}")

    try:
        # the operations the app actually issues
        client.set(prefix + "s", b"value")
        check("string set/get", client.get(prefix + "s"), b"value")

        client.set(prefix + "ttl", b"x", ttl=1)
        got = client.get(prefix + "ttl")
        time.sleep(1.4)
        check("ttl expiry (sessions, cached traces)",
              (got, client.get(prefix + "ttl")), (b"x", None))

        blob = bytes(range(256))
        client.set(prefix + "blob", blob)
        check("binary values (serialised datasets)", client.get(prefix + "blob"), blob)

        client.hset(prefix + "h", {"a": b"1", "b": b"2"})
        check("hash write/read (per-account mule scores)",
              client.hgetall(prefix + "h"), {"a": b"1", "b": b"2"})

        client.zadd(prefix + "z", {"low": 1.0, "high": 9.0, "mid": 5.0})
        check("sorted set ordering (ranked chains)",
              [m for m, _ in client.zrevrange(prefix + "z")], ["high", "mid", "low"])
        check("sorted set limit", len(client.zrevrange(prefix + "z", 0, 1)), 2)
        check("zcard", client.zcard(prefix + "z"), 3)

        client.set(prefix + "trace:1", b"x")
        client.set(prefix + "trace:2", b"x")
        check("prefix scan (trace invalidation)",
              sorted(client.scan(prefix + "trace:*")),
              [prefix + "trace:1", prefix + "trace:2"])

        client.delete(prefix + "s", prefix + "blob")
        check("multi-key delete", client.get(prefix + "s"), None)

    finally:
        for key in client.scan(prefix + "*"):
            client.delete(key)

    print()
    if failures:
        print(f"{BAD} {failures} check(s) failed — the app would misbehave on this server.")
        return 1

    print(f"{OK} All checks passed. Start the app and /api/health will report")
    print('        "store": {"backend": "redis", "durable": true}')
    print("\n  The 19 skipped Redis contract tests will now run:")
    print("    .venv/Scripts/python.exe -m pytest tests/test_store.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
