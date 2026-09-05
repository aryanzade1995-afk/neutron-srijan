"""Redis-backed store for datasets, detection patterns and auth state.

Redis holds three kinds of thing here:

  * **Datasets** - a generated network is serialised once and reused, so a seed
    that has been built before is served without regenerating it.
  * **Detection patterns** - ranked chains live in a sorted set keyed by risk
    score, and per-account mule scores in a hash. Retrieving the worst chains or
    scoring one account is then a single Redis command rather than a rescan.
    The detection itself stays in the graph walk and the classifier; Redis
    stores and serves the result, it does not do the recognising.
  * **Auth state** - sessions and pending MFA challenges, with TTLs so they
    expire on their own.

If no Redis server answers, an in-process backend with the same interface takes
over so the console still runs. Which one is live is reported on /api/health -
this must never be ambiguous, because the fallback is not shared between
processes and is not durable.
"""
from __future__ import annotations

import fnmatch
import os
import sys
import threading
import time
from typing import Iterable

REDIS_URL = os.environ.get("MULETRACE_REDIS_URL", "redis://127.0.0.1:6379/0")
CONNECT_TIMEOUT = float(os.environ.get("MULETRACE_REDIS_TIMEOUT", "0.4"))


class MemoryStore:
    """In-process stand-in implementing the subset of Redis this app uses."""

    backend = "in-process"
    durable = False

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, tuple[bytes, float | None]] = {}
        self._hashes: dict[str, dict[str, bytes]] = {}
        self._zsets: dict[str, dict[str, float]] = {}

    # ---- expiry ----

    def _alive(self, key: str) -> bool:
        item = self._values.get(key)
        if item is None:
            return False
        _, expires = item
        if expires is not None and expires < time.time():
            self._values.pop(key, None)
            return False
        return True

    # ---- strings ----

    def get(self, key: str) -> bytes | None:
        with self._lock:
            return self._values[key][0] if self._alive(key) else None

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        with self._lock:
            self._values[key] = (value, time.time() + ttl if ttl else None)

    def delete(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._values.pop(key, None)
                self._hashes.pop(key, None)
                self._zsets.pop(key, None)

    def exists(self, key: str) -> bool:
        with self._lock:
            return self._alive(key) or key in self._hashes or key in self._zsets

    def scan(self, pattern: str) -> list[str]:
        with self._lock:
            keys = set(self._hashes) | set(self._zsets) | {k for k in self._values if self._alive(k)}
            return sorted(k for k in keys if fnmatch.fnmatch(k, pattern))

    # ---- hashes ----

    def hset(self, key: str, mapping: dict[str, bytes]) -> None:
        with self._lock:
            self._hashes.setdefault(key, {}).update(mapping)

    def hget(self, key: str, field: str) -> bytes | None:
        with self._lock:
            return self._hashes.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, bytes]:
        with self._lock:
            return dict(self._hashes.get(key, {}))

    # ---- sorted sets ----

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        with self._lock:
            self._zsets.setdefault(key, {}).update(mapping)

    def zrevrange(self, key: str, start: int = 0, stop: int = -1) -> list[tuple[str, float]]:
        with self._lock:
            items = sorted(self._zsets.get(key, {}).items(), key=lambda kv: -kv[1])
        stop = len(items) if stop == -1 else stop + 1
        return items[start:stop]

    def zcard(self, key: str) -> int:
        with self._lock:
            return len(self._zsets.get(key, {}))

    def ping(self) -> bool:
        return True

    def info(self) -> dict:
        with self._lock:
            return {"backend": self.backend, "durable": self.durable,
                    "keys": len(self._values) + len(self._hashes) + len(self._zsets)}


class RedisStore:
    """Thin wrapper so the app never depends on redis-py's exact signatures."""

    backend = "redis"
    durable = True

    def __init__(self, client) -> None:
        self._r = client

    def get(self, key: str) -> bytes | None:
        return self._r.get(key)

    def set(self, key: str, value: bytes, ttl: int | None = None) -> None:
        self._r.set(key, value, ex=ttl)

    def delete(self, *keys: str) -> None:
        if keys:
            self._r.delete(*keys)

    def exists(self, key: str) -> bool:
        return bool(self._r.exists(key))

    def scan(self, pattern: str) -> list[str]:
        return sorted(k.decode() for k in self._r.scan_iter(match=pattern, count=500))

    def hset(self, key: str, mapping: dict[str, bytes]) -> None:
        if mapping:
            self._r.hset(key, mapping=mapping)

    def hget(self, key: str, field: str) -> bytes | None:
        return self._r.hget(key, field)

    def hgetall(self, key: str) -> dict[str, bytes]:
        return {k.decode(): v for k, v in self._r.hgetall(key).items()}

    def zadd(self, key: str, mapping: dict[str, float]) -> None:
        if mapping:
            self._r.zadd(key, mapping)

    def zrevrange(self, key: str, start: int = 0, stop: int = -1) -> list[tuple[str, float]]:
        return [(m.decode(), s) for m, s in
                self._r.zrevrange(key, start, stop, withscores=True)]

    def zcard(self, key: str) -> int:
        return int(self._r.zcard(key))

    def ping(self) -> bool:
        return bool(self._r.ping())

    def info(self) -> dict:
        try:
            server = self._r.info("server")
            memory = self._r.info("memory")
            return {"backend": self.backend, "durable": self.durable,
                    "redis_version": server.get("redis_version"),
                    "used_memory_human": memory.get("used_memory_human"),
                    "keys": self._r.dbsize()}
        except Exception:
            return {"backend": self.backend, "durable": self.durable}


def connect(url: str = REDIS_URL) -> MemoryStore | RedisStore:
    """Use Redis when one answers, otherwise fall back and say so."""
    try:
        import redis

        client = redis.Redis.from_url(
            url, socket_connect_timeout=CONNECT_TIMEOUT, socket_timeout=CONNECT_TIMEOUT)
        client.ping()
        return RedisStore(client)
    except Exception as exc:                      # server down, wrong URL, no package
        # stderr, not stdout: importing this module must not corrupt the output
        # of any script that prints a result
        print(f"[store] Redis unavailable at {url} ({type(exc).__name__}); "
              f"using the in-process store. Sessions and cached datasets will not "
              f"survive a restart and are not shared across workers.",
              file=sys.stderr)
        return MemoryStore()


STORE: MemoryStore | RedisStore = connect()


def keys_for(seed: int) -> dict[str, str]:
    """Key layout for one dataset. Grouped under a seed so a whole workspace can
    be dropped with one scan."""
    base = f"mt:ws:{seed}"
    return {
        "dataset": f"{base}:dataset",     # serialised frames
        "chains": f"{base}:chains",       # ZSET  entry_txn_id -> risk score
        "chain": f"{base}:chain:",        # STRING per chain, JSON
        "mule": f"{base}:mule",           # HASH  account -> mule score
        "accounts": f"{base}:accounts",   # ZSET  account -> mule score
        "trace": f"{base}:trace:",        # STRING cached walk, JSON
        "meta": f"{base}:meta",           # HASH  counts and params
    }
