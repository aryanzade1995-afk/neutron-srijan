"""Authentication: the second factor has to actually be a gate, not decoration."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import auth
import store


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import app as app_module
    import workspace as workspace_module

    workspace_module.FEEDBACK_DIR = tmp_path_factory.mktemp("feedback")

    # the gate ships off so a demo is not blocked; this suite is what proves it
    # works, so turn it on for the duration
    previous, auth.AUTH_ENABLED = auth.AUTH_ENABLED, True
    with TestClient(app_module.app) as test_client:
        yield test_client
    auth.AUTH_ENABLED = previous


@pytest.fixture
def user(tmp_path, monkeypatch):
    # accounts persist to disk, so point the suite at a throwaway file rather
    # than writing test users into the real one
    monkeypatch.setattr(auth, "USERS_FILE", tmp_path / "users.json")
    name = f"tester-{int(time.time() * 1000) % 1_000_000}"
    record = auth.create_user(name, "correct horse battery staple")
    yield name, "correct horse battery staple", record["totp_secret"]
    auth.delete_user(name)
    store.STORE.delete(auth.THROTTLE_KEY + name)


def code_for(secret: str, drift: int = 0) -> str:
    return auth.totp_at(secret, int(time.time() // 30) + drift)


# ---------- TOTP itself ----------

def test_totp_matches_the_current_step(user):
    _, _, secret = user
    assert auth.verify_totp(secret, code_for(secret))


def test_totp_tolerates_a_small_clock_skew(user):
    _, _, secret = user
    assert auth.verify_totp(secret, code_for(secret, -1))
    assert auth.verify_totp(secret, code_for(secret, +1))


def test_totp_rejects_a_distant_step(user):
    _, _, secret = user
    assert not auth.verify_totp(secret, code_for(secret, 10))


def test_totp_rejects_rubbish(user):
    _, _, secret = user
    for bad in ("", "abcdef", "12345", "1234567", None):
        assert not auth.verify_totp(secret, bad)


def test_secrets_differ_between_users():
    assert auth.new_totp_secret() != auth.new_totp_secret()


# ---------- passwords ----------

def test_password_hash_is_salted_and_verifiable():
    h1, s1 = auth.hash_password("hunter2")
    h2, s2 = auth.hash_password("hunter2")
    assert h1 != h2 and s1 != s2          # same password, different salt
    assert auth.check_password("hunter2", h1, s1)
    assert not auth.check_password("hunter3", h1, s1)


# ---------- the gate ----------

def test_case_data_is_closed_without_a_session(client):
    for path in ("/api/overview", "/api/chains", "/api/watchlist", "/api/evaluation"):
        assert client.get(path).status_code == 401, path


def test_console_redirects_to_login(client):
    res = client.get("/console", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"].startswith("/login")


def test_login_and_health_stay_open(client):
    assert client.get("/login").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


# ---------- the two factors ----------

def test_password_alone_does_not_authenticate(client, user):
    name, password, _ = user
    res = client.post("/api/auth/login", json={"username": name, "password": password})
    assert res.status_code == 200
    assert res.json()["mfa_required"] is True
    # the first factor must not have issued a session
    assert client.get("/api/overview").status_code == 401


def test_wrong_password_is_rejected(client, user):
    name, _, _ = user
    assert client.post("/api/auth/login",
                       json={"username": name, "password": "wrong"}).status_code == 401


def test_unknown_user_is_rejected(client):
    assert client.post("/api/auth/login",
                       json={"username": "nobody", "password": "x"}).status_code == 401


def test_wrong_code_does_not_authenticate(client, user):
    name, password, _ = user
    challenge = client.post("/api/auth/login",
                            json={"username": name, "password": password}).json()["challenge"]
    res = client.post("/api/auth/verify", json={"challenge": challenge, "code": "000000"})
    assert res.status_code == 401
    assert client.get("/api/overview").status_code == 401


def test_both_factors_grant_access_then_logout_revokes_it(client, user):
    name, password, secret = user
    challenge = client.post("/api/auth/login",
                            json={"username": name, "password": password}).json()["challenge"]
    res = client.post("/api/auth/verify",
                      json={"challenge": challenge, "code": code_for(secret)})
    assert res.status_code == 200 and res.json()["authenticated"] is True

    assert client.get("/api/overview").status_code == 200

    client.post("/api/auth/logout")
    assert client.get("/api/overview").status_code == 401


def test_a_challenge_cannot_be_replayed(client, user):
    name, password, secret = user
    challenge = client.post("/api/auth/login",
                            json={"username": name, "password": password}).json()["challenge"]
    assert client.post("/api/auth/verify",
                       json={"challenge": challenge, "code": code_for(secret)}).status_code == 200
    client.post("/api/auth/logout")
    # the same challenge must not mint a second session
    assert client.post("/api/auth/verify",
                       json={"challenge": challenge, "code": code_for(secret)}).status_code == 401


def test_repeated_failures_lock_the_account(client, user):
    name, _, _ = user
    for _ in range(auth.MAX_ATTEMPTS):
        client.post("/api/auth/login", json={"username": name, "password": "wrong"})
    res = client.post("/api/auth/login", json={"username": name, "password": "wrong"})
    assert res.status_code == 429


def test_enrolment_secret_is_shown_once_then_withheld(client, user):
    name, password, secret = user
    first = client.post("/api/auth/login", json={"username": name, "password": password}).json()
    assert first["enrolled"] is False
    assert first["totp_secret"] == secret          # offered for enrolment

    client.post("/api/auth/verify", json={"challenge": first["challenge"],
                                          "code": code_for(secret)})
    client.post("/api/auth/logout")

    second = client.post("/api/auth/login", json={"username": name, "password": password}).json()
    assert second["enrolled"] is True
    assert "totp_secret" not in second             # never handed out again
