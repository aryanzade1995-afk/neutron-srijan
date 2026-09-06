"""The two store backends must behave identically.

`MemoryStore` is exercised constantly; `RedisStore` only runs when a server is
reachable. That asymmetry is the risk — a divergence between them would show up
as a bug that only appears in the deployment that has Redis. So every test here
runs against both, and the Redis ones skip cleanly when no server answers.

Run a Redis and these stop skipping:
    docker run -d -p 6379:6379 redis:7-alpine
"""
from __future__ import annotations

import time

import pytest

import store


def _redis_or_skip():
    candidate = store.connect()
    if candidate.backend != "redis":
        pytest.skip("no Redis server reachable — set MULETRACE_REDIS_URL to run these")
    return candidate


@pytest.fixture(params=["memory", "redis"])
def backend(request):
    if request.param == "memory":
        yield store.MemoryStore()
        return

    client = _redis_or_skip()
    prefix = f"mt:test:{int(time.time() * 1000)}:"

    class Namespaced:
        """Key-prefix wrapper, so a test run cannot tread on real data."""
        def __init__(self, inner):
            self._inner = inner
            self.backend = inner.backend

        def _k(self, key):
            return prefix + key

        def get(self, k): return self._inner.get(self._k(k))
        def set(self, k, v, ttl=None): return self._inner.set(self._k(k), v, ttl)
        def delete(self, *ks): return self._inner.delete(*[self._k(k) for k in ks])
        def exists(self, k): return self._inner.exists(self._k(k))
        def scan(self, pat): return [x[len(prefix):] for x in self._inner.scan(self._k(pat))]
        def hset(self, k, m): return self._inner.hset(self._k(k), m)
        def hget(self, k, f): return self._inner.hget(self._k(k), f)
        def hgetall(self, k): return self._inner.hgetall(self._k(k))
        def zadd(self, k, m): return self._inner.zadd(self._k(k), m)
        def zrevrange(self, k, a=0, b=-1): return self._inner.zrevrange(self._k(k), a, b)
        def zcard(self, k): return self._inner.zcard(self._k(k))

    wrapper = Namespaced(client)
    yield wrapper
    for key in client.scan(prefix + "*"):
        client.delete(key)


# ---------- strings ----------

def test_set_then_get_round_trips(backend):
    backend.set("a", b"hello")
    assert backend.get("a") == b"hello"


def test_missing_key_is_none(backend):
    assert backend.get("nothing-here") is None


def test_delete_removes_a_key(backend):
    backend.set("a", b"x")
    backend.delete("a")
    assert backend.get("a") is None


def test_exists_reflects_presence(backend):
    assert backend.exists("a") is False
    backend.set("a", b"x")
    assert backend.exists("a") is True


def test_ttl_expires_the_key(backend):
    backend.set("short", b"x", ttl=1)
    assert backend.get("short") == b"x"
    time.sleep(1.4)
    assert backend.get("short") is None


def test_overwrite_replaces_the_value(backend):
    backend.set("a", b"first")
    backend.set("a", b"second")
    assert backend.get("a") == b"second"


def test_binary_values_survive(backend):
    blob = bytes(range(256))
    backend.set("blob", blob)
    assert backend.get("blob") == blob


# ---------- hashes ----------

def test_hash_set_and_read_back(backend):
    backend.hset("h", {"one": b"1", "two": b"2"})
    assert backend.hget("h", "one") == b"1"
    assert backend.hgetall("h") == {"one": b"1", "two": b"2"}


def test_hash_merges_rather_than_replaces(backend):
    backend.hset("h", {"one": b"1"})
    backend.hset("h", {"two": b"2"})
    assert backend.hgetall("h") == {"one": b"1", "two": b"2"}


def test_missing_hash_is_empty(backend):
    assert backend.hgetall("absent") == {}
    assert backend.hget("absent", "x") is None


# ---------- sorted sets: this is what ranks the chains ----------

def test_zrevrange_returns_highest_score_first(backend):
    backend.zadd("z", {"low": 1.0, "high": 9.0, "mid": 5.0})
    assert [m for m, _ in backend.zrevrange("z")] == ["high", "mid", "low"]


def test_zrevrange_honours_the_limit(backend):
    backend.zadd("z", {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0})
    assert [m for m, _ in backend.zrevrange("z", 0, 1)] == ["d", "c"]


def test_zrevrange_returns_scores(backend):
    backend.zadd("z", {"a": 87.5})
    member, score = backend.zrevrange("z")[0]
    assert member == "a"
    assert score == pytest.approx(87.5)


def test_zadd_updates_an_existing_score(backend):
    backend.zadd("z", {"a": 1.0, "b": 2.0})
    backend.zadd("z", {"a": 99.0})
    assert [m for m, _ in backend.zrevrange("z")] == ["a", "b"]


def test_zcard_counts_members(backend):
    assert backend.zcard("z") == 0
    backend.zadd("z", {"a": 1.0, "b": 2.0})
    assert backend.zcard("z") == 2


def test_empty_writes_are_harmless(backend):
    backend.zadd("z", {})
    backend.hset("h", {})
    backend.delete()
    assert backend.zcard("z") == 0


# ---------- scan: used to invalidate cached traces ----------

def test_scan_matches_a_prefix(backend):
    backend.set("trace:1", b"x")
    backend.set("trace:2", b"x")
    backend.set("other:1", b"x")
    assert sorted(backend.scan("trace:*")) == ["trace:1", "trace:2"]


def test_scan_of_nothing_is_empty(backend):
    assert backend.scan("nope:*") == []


def test_delete_accepts_many_keys(backend):
    backend.set("k1", b"x")
    backend.set("k2", b"x")
    backend.delete("k1", "k2")
    assert backend.get("k1") is None and backend.get("k2") is None
