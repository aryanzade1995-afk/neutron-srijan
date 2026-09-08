"""PostgreSQL round-trip fidelity.

A dataset written to Postgres and read back must be the same dataset. If it is
not, detection silently disagrees with itself depending on which tier served the
data — the sort of defect that only shows up once the cache is cold.

Skips when no server answers, so the suite still runs without Postgres.
"""
from __future__ import annotations

import pandas as pd
import pytest

import db
import workspace
from generate_data import Generator

SEED = 987_654


@pytest.fixture(scope="module")
def database():
    handle = db.Postgres()
    if not handle.available:
        pytest.skip("no PostgreSQL reachable — set MULETRACE_POSTGRES_URL to run these")
    yield handle
    handle.drop(SEED)


@pytest.fixture(scope="module")
def generated():
    params = workspace.dataset_params(SEED)
    txns, accounts, truth = Generator(seed=SEED).run(
        params["n_normal"], params["n_merchants"], 2_500,
        params["n_chains"], params["n_forwarders"])
    return txns, accounts, truth, params


@pytest.fixture(scope="module")
def saved(database, generated):
    txns, accounts, truth, params = generated
    database.drop(SEED)
    assert database.save(SEED, f"DS-{SEED % 100000:05d}", txns, accounts, truth, params)
    loaded = database.load(SEED)
    assert loaded is not None
    return generated, loaded


def test_row_counts_survive(saved):
    (txns, accounts, truth, _), (rt, ra, rtr) = saved
    assert len(rt) == len(txns)
    assert len(ra) == len(accounts)
    assert len(rtr) == len(truth)


def test_every_transaction_is_identical(saved):
    """Not a sample — every row, because a partial match hides the bug."""
    (txns, _, _, _), (rt, _, _) = saved
    merged = txns[["txn_id", "sender", "receiver", "amount", "timestamp", "mode"]].merge(
        rt[["txn_id", "sender", "receiver", "amount", "timestamp", "mode"]],
        on="txn_id", suffixes=("_src", "_db"))
    assert len(merged) == len(txns), "some transactions did not come back"
    assert (merged.sender_src == merged.sender_db).all()
    assert (merged.receiver_src == merged.receiver_db).all()
    assert (merged.mode_src == merged.mode_db).all()
    assert (abs(merged.amount_src - merged.amount_db) < 0.005).all()


def test_timestamps_come_back_in_utc(saved):
    """TIMESTAMPTZ returns in the server's zone; the same instant must not render
    differently depending on which tier served it."""
    (txns, _, _, _), (rt, _, _) = saved
    merged = txns[["txn_id", "timestamp"]].merge(
        rt[["txn_id", "timestamp"]], on="txn_id", suffixes=("_src", "_db"))
    assert (merged.timestamp_src == merged.timestamp_db).all()
    assert rt["timestamp"].str.endswith("+00:00").all()


def test_account_attributes_survive(saved):
    (_, accounts, _, _), (_, ra, _) = saved
    merged = accounts[["account_id", "account_age_days", "kyc_tier", "is_mule"]].merge(
        ra[["account_id", "account_age_days", "kyc_tier", "is_mule"]],
        on="account_id", suffixes=("_src", "_db"))
    assert len(merged) == len(accounts)
    assert (merged.account_age_days_src == merged.account_age_days_db).all()
    assert (merged.kyc_tier_src == merged.kyc_tier_db).all()
    assert (merged.is_mule_src == merged.is_mule_db).all()


def test_ground_truth_survives(saved):
    (_, _, truth, _), (_, _, rtr) = saved
    merged = truth[["chain_id", "end_node", "entry_txn_id", "cashed_out"]].merge(
        rtr[["chain_id", "end_node", "entry_txn_id", "cashed_out"]],
        on="chain_id", suffixes=("_src", "_db"))
    assert len(merged) == len(truth)
    assert (merged.end_node_src == merged.end_node_db).all()
    assert (merged.entry_txn_id_src == merged.entry_txn_id_db).all()


def test_amount_precision_holds_to_the_paisa(saved):
    """NUMERIC(16,2) must not quietly round a rupee amount."""
    (txns, _, _, _), (rt, _, _) = saved
    merged = txns[["txn_id", "amount"]].merge(
        rt[["txn_id", "amount"]], on="txn_id", suffixes=("_src", "_db"))
    assert (abs(merged.amount_src - merged.amount_db) < 0.005).all()
    assert merged.amount_db.dtype.kind == "f", "amounts must be usable as floats"


def test_saving_twice_replaces_rather_than_duplicates(database, generated):
    txns, accounts, truth, params = generated
    database.save(SEED, "DS-DUP", txns, accounts, truth, params)
    database.save(SEED, "DS-DUP", txns, accounts, truth, params)
    reloaded = database.load(SEED)
    assert len(reloaded[0]) == len(txns)


def test_unknown_seed_reads_as_none(database):
    assert database.load(-1) is None
    assert database.has_dataset(-1) is False


def test_catalogue_lists_the_saved_dataset(database, saved):
    seeds = {row["seed"] for row in database.catalogue(200)}
    assert SEED in seeds


def test_absent_server_is_a_supported_state():
    """No Postgres must degrade, not raise."""
    offline = db.Postgres("postgresql://nobody:nobody@127.0.0.1:1/none")
    assert offline.available is False
    assert offline.load(1) is None
    assert offline.has_dataset(1) is False
    assert offline.save(1, "x", pd.DataFrame(), pd.DataFrame(), pd.DataFrame()) is False
    assert offline.catalogue() == []
    assert offline.info()["available"] is False


def test_default_info_does_not_scan_the_transactions_table(database, saved):
    """The health endpoint calls info(), so it must not count rows.

    count(*) is a sequential scan. It is free on an empty database and costs
    seconds once the table holds a million rows, which turns a liveness probe
    into something a platform will eventually time out and kill. Guard the
    shape - an estimate, never an exact count - rather than timing it, because
    a timing assertion would be flaky on a loaded machine.
    """
    cheap = database.info()
    assert "transactions_estimate" in cheap
    assert "transactions" not in cheap

    exact = database.info(exact=True)
    assert isinstance(exact["transactions"], int)
    assert "transactions_estimate" not in exact
