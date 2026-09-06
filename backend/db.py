"""PostgreSQL — the system of record for transaction data.

The pipeline is deliberately three-tiered, and each tier earns its place:

    PostgreSQL  ->  Redis  ->  in-memory graph
     durable        working      detection
     truth          copy

* **Postgres** holds every dataset: accounts, transactions, and the labelled
  chains. It survives restarts, can be queried with SQL by anyone auditing a
  case, and is where a real deployment would point at the bank's own warehouse.
* **Redis** holds a serialised copy of whatever dataset is currently in play,
  plus the detection output. It is a cache with a TTL - losing it costs a
  rebuild from Postgres, nothing more.
* The **graph** is built from that working copy and lives only as long as the
  workspace does.

Reading a dataset back out of Postgres is a few hundred milliseconds; rebuilding
the graph and refitting the model is the expensive part, which is why the
serialised copy sits in Redis rather than round-tripping SQL on every request.

Set MULETRACE_POSTGRES_URL to point elsewhere. If no server answers, the app
generates datasets in memory exactly as before and says so on /api/health -
Postgres is the system of record when present, not a hard dependency that stops
a demo when it is not.
"""
from __future__ import annotations

import os
from datetime import timezone
import sys
from typing import Iterable

import pandas as pd

POSTGRES_URL = os.environ.get(
    "MULETRACE_POSTGRES_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/muletrace")

# the database the app owns; created on first use if it is missing
ADMIN_URL = os.environ.get(
    "MULETRACE_POSTGRES_ADMIN_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/postgres")

CONNECT_TIMEOUT = float(os.environ.get("MULETRACE_POSTGRES_TIMEOUT", "3"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS datasets (
    seed          BIGINT PRIMARY KEY,
    label         TEXT        NOT NULL,
    transactions  INTEGER     NOT NULL,
    accounts      INTEGER     NOT NULL,
    chains        INTEGER     NOT NULL,
    params        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accounts (
    seed             BIGINT  NOT NULL REFERENCES datasets(seed) ON DELETE CASCADE,
    account_id       TEXT    NOT NULL,
    bank             TEXT,
    account_type     TEXT,
    account_age_days INTEGER,
    kyc_tier         INTEGER,
    mule_archetype   TEXT,
    is_mule          SMALLINT NOT NULL DEFAULT 0,
    PRIMARY KEY (seed, account_id)
);

CREATE TABLE IF NOT EXISTS transactions (
    seed        BIGINT      NOT NULL REFERENCES datasets(seed) ON DELETE CASCADE,
    txn_id      TEXT        NOT NULL,
    sender      TEXT        NOT NULL,
    receiver    TEXT        NOT NULL,
    amount      NUMERIC(16,2) NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    mode        TEXT        NOT NULL,
    chain_id    TEXT,
    hop_index   INTEGER,
    is_fraud    SMALLINT    NOT NULL DEFAULT 0,
    PRIMARY KEY (seed, txn_id)
);

-- the rule engine works a recent window per account, and the chain walk follows
-- a specific account forward in time; both want these
CREATE INDEX IF NOT EXISTS txn_seed_ts       ON transactions (seed, ts);
CREATE INDEX IF NOT EXISTS txn_seed_sender   ON transactions (seed, sender, ts);
CREATE INDEX IF NOT EXISTS txn_seed_receiver ON transactions (seed, receiver, ts);

CREATE TABLE IF NOT EXISTS ground_truth (
    seed         BIGINT NOT NULL REFERENCES datasets(seed) ON DELETE CASCADE,
    chain_id     TEXT   NOT NULL,
    victim       TEXT   NOT NULL,
    entry_txn_id TEXT   NOT NULL,
    mules        TEXT   NOT NULL,
    end_node     TEXT   NOT NULL,
    hops         INTEGER,
    amount       NUMERIC(16,2),
    cashed_out   SMALLINT NOT NULL DEFAULT 0,
    PRIMARY KEY (seed, chain_id)
);
"""


class Postgres:
    """Thin wrapper. Absent server is a supported state, not an error."""

    def __init__(self, url: str = POSTGRES_URL) -> None:
        self.url = url
        self.available = False
        self.version: str | None = None
        self._connect_error: str | None = None
        self._prepare()

    # ---- connection ----

    def _prepare(self) -> None:
        try:
            import psycopg
        except ImportError:
            self._connect_error = "psycopg not installed"
            return

        try:
            self._ensure_database(psycopg)
            with psycopg.connect(self.url, connect_timeout=CONNECT_TIMEOUT) as conn:
                self.version = conn.execute("SELECT version()").fetchone()[0].split(",")[0]
                conn.execute(SCHEMA)
                conn.commit()
            self.available = True
        except Exception as exc:
            self._connect_error = f"{type(exc).__name__}: {exc}"
            print(f"[db] PostgreSQL unavailable at {self.url} ({self._connect_error}); "
                  f"datasets will be generated in memory instead.", file=sys.stderr)

    def _ensure_database(self, psycopg) -> None:
        """Create the application database if the server has not got it yet."""
        name = self.url.rsplit("/", 1)[-1]
        with psycopg.connect(ADMIN_URL, connect_timeout=CONNECT_TIMEOUT,
                             autocommit=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{name}"')

    def _conn(self):
        import psycopg
        return psycopg.connect(self.url, connect_timeout=CONNECT_TIMEOUT)

    # ---- reads ----

    def has_dataset(self, seed: int) -> bool:
        if not self.available:
            return False
        with self._conn() as conn:
            row = conn.execute("SELECT 1 FROM datasets WHERE seed = %s", (seed,)).fetchone()
        return row is not None

    def load(self, seed: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame] | None:
        """Read one dataset back in the shape the graph expects."""
        if not self.available:
            return None
        with self._conn() as conn:
            if not conn.execute("SELECT 1 FROM datasets WHERE seed = %s",
                                (seed,)).fetchone():
                return None

            txns = conn.execute(
                "SELECT txn_id, sender, receiver, amount, ts, mode, chain_id, "
                "hop_index, is_fraud FROM transactions WHERE seed = %s ORDER BY ts",
                (seed,)).fetchall()
            accounts = conn.execute(
                "SELECT account_id, bank, account_type, account_age_days, kyc_tier, "
                "mule_archetype, is_mule FROM accounts WHERE seed = %s", (seed,)).fetchall()
            truth = conn.execute(
                "SELECT chain_id, victim, entry_txn_id, mules, end_node, hops, amount, "
                "cashed_out FROM ground_truth WHERE seed = %s", (seed,)).fetchall()

        txn_df = pd.DataFrame(txns, columns=[
            "txn_id", "sender", "receiver", "amount", "timestamp", "mode",
            "chain_id", "hop_index", "is_fraud"])
        # The graph parses timestamps itself and expects ISO strings, and NUMERIC
        # comes back as Decimal which the arithmetic downstream will not take.
        #
        # Normalising to UTC matters: TIMESTAMPTZ is returned in the server's own
        # zone, so the same instant renders as +05:30 here and +00:00 from the
        # generator. The comparisons would still be correct, but a chain loaded
        # from Postgres would display different timestamps to the identical chain
        # generated in memory.
        txn_df["timestamp"] = txn_df["timestamp"].apply(
            lambda t: t.astimezone(timezone.utc).isoformat())
        txn_df["amount"] = txn_df["amount"].astype(float)
        txn_df["chain_id"] = txn_df["chain_id"].fillna("")

        acct_df = pd.DataFrame(accounts, columns=[
            "account_id", "bank", "account_type", "account_age_days", "kyc_tier",
            "mule_archetype", "is_mule"])

        truth_df = pd.DataFrame(truth, columns=[
            "chain_id", "victim", "entry_txn_id", "mules", "end_node", "hops",
            "amount", "cashed_out"])
        if not truth_df.empty:
            truth_df["amount"] = truth_df["amount"].astype(float)

        return txn_df, acct_df, truth_df

    # ---- writes ----

    def save(self, seed: int, label: str, txns: pd.DataFrame, accounts: pd.DataFrame,
             truth: pd.DataFrame, params: dict | None = None) -> bool:
        """Persist a dataset. Replaces any existing rows for the seed."""
        if not self.available:
            return False

        import json as _json
        with self._conn() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM datasets WHERE seed = %s", (seed,))
                conn.execute(
                    "INSERT INTO datasets (seed, label, transactions, accounts, "
                    "chains, params) VALUES (%s, %s, %s, %s, %s, %s)",
                    (seed, label, len(txns), len(accounts), len(truth),
                     _json.dumps(params or {})))

                with conn.cursor().copy(
                    "COPY accounts (seed, account_id, bank, account_type, "
                    "account_age_days, kyc_tier, mule_archetype, is_mule) "
                    "FROM STDIN"
                ) as copy:
                    for row in accounts.itertuples(index=False):
                        copy.write_row((
                            seed, row.account_id, getattr(row, "bank", None),
                            getattr(row, "account_type", None),
                            int(getattr(row, "account_age_days", 0) or 0),
                            int(getattr(row, "kyc_tier", 0) or 0),
                            getattr(row, "mule_archetype", None),
                            int(getattr(row, "is_mule", 0) or 0)))

                with conn.cursor().copy(
                    "COPY transactions (seed, txn_id, sender, receiver, amount, ts, "
                    "mode, chain_id, hop_index, is_fraud) FROM STDIN"
                ) as copy:
                    for row in txns.itertuples(index=False):
                        copy.write_row((
                            seed, row.txn_id, row.sender, row.receiver,
                            float(row.amount), row.timestamp, row.mode,
                            row.chain_id or None, int(row.hop_index),
                            int(row.is_fraud)))

                if len(truth):
                    with conn.cursor().copy(
                        "COPY ground_truth (seed, chain_id, victim, entry_txn_id, "
                        "mules, end_node, hops, amount, cashed_out) FROM STDIN"
                    ) as copy:
                        for row in truth.itertuples(index=False):
                            copy.write_row((
                                seed, row.chain_id, row.victim, row.entry_txn_id,
                                row.mules, row.end_node, int(row.hops),
                                float(row.amount), int(row.cashed_out)))
        return True

    def drop(self, seed: int) -> None:
        if self.available:
            with self._conn() as conn:
                conn.execute("DELETE FROM datasets WHERE seed = %s", (seed,))
                conn.commit()

    # ---- reporting ----

    def catalogue(self, limit: int = 50) -> list[dict]:
        if not self.available:
            return []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT seed, label, transactions, accounts, chains, created_at "
                "FROM datasets ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
        return [{"seed": r[0], "label": r[1], "transactions": r[2],
                 "accounts": r[3], "chains": r[4], "created_at": r[5].isoformat()}
                for r in rows]

    def info(self) -> dict:
        if not self.available:
            return {"available": False, "reason": self._connect_error, "url": self.url}
        with self._conn() as conn:
            datasets = conn.execute("SELECT count(*) FROM datasets").fetchone()[0]
            txns = conn.execute("SELECT count(*) FROM transactions").fetchone()[0]
            size = conn.execute(
                "SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
        return {"available": True, "version": self.version, "datasets": datasets,
                "transactions": txns, "size": size, "url": self.url.rsplit("@", 1)[-1]}


DB = Postgres()
