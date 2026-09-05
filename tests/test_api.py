"""API contract tests, run against the real generated dataset.

These boot the app the way uvicorn does, so the lifespan hook, the model fit and
the proactive scan are all exercised.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

SEED = 4242          # pin one dataset so the suite is deterministic


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    import app as app_module
    import workspace as workspace_module

    # keep the suite from writing into the real per-session audit logs
    workspace_module.FEEDBACK_DIR = tmp_path_factory.mktemp("feedback")

    # Every cookie-based session resolves to one workspace, so the suite is
    # deterministic. Requests that pass ?seed= explicitly still bypass this,
    # which is what the seed tests need.
    app_module._seed_from_token = lambda token: SEED

    with TestClient(app_module.app) as test_client:
        yield test_client


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["transactions"] > 0
    assert body["seed"] == SEED


def test_overview_reports_totals_and_model(client):
    body = client.get("/api/overview").json()
    assert body["transactions"] > 0
    assert body["active_chains"] > 0
    assert body["freezable_chains"] + body["cashed_out_chains"] == body["active_chains"]
    assert 0 <= body["model"]["roc_auc"] <= 1
    assert body["baseline"]["algorithm"] == "LogisticRegression"


def test_chains_are_ranked_by_priority(client):
    chains = client.get("/api/chains?limit=25").json()["chains"]
    assert chains
    scores = [c["risk"]["score"] for c in chains]
    assert scores == sorted(scores, reverse=True)


def test_chain_payload_is_self_consistent(client):
    chain = client.get("/api/chains?limit=1").json()["chains"][0]
    assert chain["hop_count"] == len(chain["hops"])
    assert len(chain["path"]) == chain["hop_count"] + 1
    assert chain["nodes"][0]["role"] == "victim"
    assert chain["nodes"][-1]["account_id"] == chain["end_node"]
    assert chain["path"][-1] == chain["end_node"]


def test_trace_matches_the_chain_it_came_from(client):
    chain = client.get("/api/chains?limit=1").json()["chains"][0]
    traced = client.get(f"/api/trace/{chain['entry_txn_id']}").json()
    assert traced["end_node"] == chain["end_node"]
    assert traced["hop_count"] == chain["hop_count"]


def test_trace_honours_walk_parameters(client):
    entry = client.get("/api/chains?limit=1").json()["chains"][0]["entry_txn_id"]
    strict = client.get(f"/api/trace/{entry}?min_forward_pct=0.99&max_gap_hours=0.05").json()
    default = client.get(f"/api/trace/{entry}").json()
    assert strict["hop_count"] <= default["hop_count"]


def test_trace_rejects_unknown_transaction(client):
    assert client.get("/api/trace/TXN9999999").status_code == 404


def test_trace_validates_parameter_bounds(client):
    entry = client.get("/api/chains?limit=1").json()["chains"][0]["entry_txn_id"]
    assert client.get(f"/api/trace/{entry}?min_forward_pct=5").status_code == 422
    assert client.get(f"/api/trace/{entry}?max_hops=0").status_code == 422


def test_risk_score_explains_itself(client):
    account = client.get("/api/watchlist?limit=1").json()["accounts"][0]["account_id"]
    body = client.get(f"/api/risk-score/{account}").json()
    assert 0 <= body["score"] <= 1
    assert body["signals"]
    assert all(0 <= s["percentile"] <= 100 for s in body["signals"])


def test_risk_score_unknown_account(client):
    assert client.get("/api/risk-score/nobody@nowhere").status_code == 404


def test_watchlist_is_sorted_descending(client):
    accounts = client.get("/api/watchlist?limit=20&min_score=0.3").json()["accounts"]
    scores = [a["score"] for a in accounts]
    assert scores == sorted(scores, reverse=True)


def test_search_finds_a_transaction(client):
    entry = client.get("/api/chains?limit=1").json()["chains"][0]["entry_txn_id"]
    body = client.get(f"/api/search?q={entry}").json()
    assert entry in [t["txn_id"] for t in body["transactions"]]


# ---------- feedback loop ----------

def test_recording_a_verdict_persists_and_attaches_to_the_chain(client):
    entry = client.get("/api/chains?limit=1").json()["chains"][0]["entry_txn_id"]

    posted = client.post("/api/feedback", json={
        "entry_txn_id": entry, "verdict": "confirmed_fraud",
        "note": "funds frozen at end node", "reviewer": "desk-01",
    })
    assert posted.status_code == 201
    assert posted.json()["recorded"]["verdict"] == "confirmed_fraud"

    traced = client.get(f"/api/trace/{entry}").json()
    assert traced["review"]["verdict"] == "confirmed_fraud"
    assert traced["review"]["reviewer"] == "desk-01"


def test_confirmed_chains_become_training_labels(client):
    entry = client.get("/api/chains?limit=3").json()["chains"][2]["entry_txn_id"]
    client.post("/api/feedback", json={"entry_txn_id": entry, "verdict": "confirmed_fraud"})

    labels = client.get("/api/feedback/labels").json()
    assert labels["positives"] >= 1
    assert all(v in (0, 1) for v in labels["labels"].values())


def test_dismissed_chains_drop_out_of_the_queue(client):
    chains = client.get("/api/chains?limit=25").json()["chains"]
    target = chains[-1]["entry_txn_id"]

    client.post("/api/feedback", json={"entry_txn_id": target, "verdict": "false_positive"})

    remaining = [c["entry_txn_id"] for c in client.get("/api/chains?limit=60").json()["chains"]]
    assert target not in remaining

    with_dismissed = [c["entry_txn_id"] for c in
                      client.get("/api/chains?limit=60&include_dismissed=true").json()["chains"]]
    assert target in with_dismissed


def test_dashboard_counts_match_the_table(client):
    """The stat cards and the queue must never disagree - they are the same set."""
    overview = client.get("/api/overview").json()
    listed = client.get("/api/chains?limit=500").json()
    assert overview["active_chains"] == listed["count"]
    assert overview["freezable_chains"] + overview["cashed_out_chains"] == overview["active_chains"]

    with_dismissed = client.get("/api/chains?limit=500&include_dismissed=true").json()
    assert overview["dismissed_chains"] == with_dismissed["count"] - listed["count"]


def test_dismissing_a_chain_moves_the_dashboard_count(client):
    before = client.get("/api/overview").json()["active_chains"]
    target = client.get("/api/chains?limit=500").json()["chains"][-1]["entry_txn_id"]

    client.post("/api/feedback", json={"entry_txn_id": target, "verdict": "false_positive"})

    after = client.get("/api/overview").json()
    assert after["active_chains"] == before - 1
    assert after["active_chains"] == client.get("/api/chains?limit=500").json()["count"]


def test_evaluation_describes_the_loaded_dataset(client):
    overview = client.get("/api/overview").json()
    ev = client.get("/api/evaluation").json()
    # the evaluation must be computed against the data actually in memory
    assert ev["dataset"]["transactions"] == overview["transactions"]
    assert ev["dataset"]["accounts"] == overview["accounts"]
    assert ev["dataset"]["injected_chains"] == overview["injected_chains"]
    assert 0 <= ev["chain_walk"]["end_node_accuracy"] <= 1
    assert ev["proactive_scan"]["clean_prefix"] >= 0
    assert len(ev["sensitivity"]) == 5


def test_pinning_a_seed_reproduces_the_same_dataset(client):
    """A pinned seed must be reproducible - a demo has to be able to return to
    the exact numbers on a slide."""
    a = client.get(f"/api/overview?seed={SEED}").json()
    b = client.get(f"/api/overview?seed={SEED}").json()
    assert (a["transactions"], a["accounts"], a["active_chains"]) ==            (b["transactions"], b["accounts"], b["active_chains"])


def test_different_seeds_give_different_datasets(client):
    """Two sessions must not land on the same network."""
    seen = set()
    for seed in (11, 22, 33, 44):
        o = client.get(f"/api/overview?seed={seed}").json()
        assert o["seed"] == seed
        seen.add((o["transactions"], o["accounts"], o["injected_chains"]))
    assert len(seen) == 4


def test_a_rejected_seed_is_reported(client):
    assert client.get("/api/overview?seed=notanumber").status_code == 422


def test_rejects_an_unknown_verdict(client):
    entry = client.get("/api/chains?limit=1").json()["chains"][0]["entry_txn_id"]
    response = client.post("/api/feedback", json={"entry_txn_id": entry, "verdict": "maybe"})
    assert response.status_code == 422


def test_rejects_feedback_on_an_unknown_transaction(client):
    response = client.post("/api/feedback",
                           json={"entry_txn_id": "TXN9999999", "verdict": "confirmed_fraud"})
    assert response.status_code == 404
