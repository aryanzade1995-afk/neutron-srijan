"""Synthetic UPI-style transaction generator with injected mule chains.

Real UPI data is not accessible outside NPCI/banks, so the demo runs on a
synthetic network with labelled ground truth. The generator deliberately mixes
in legitimate fast-forwarding accounts so the detector has to earn its precision
instead of separating classes on a single obvious threshold.
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

BANKS = ["HDFC", "SBIN", "ICIC", "AXIS", "KOTK", "PYTM", "YESB", "UTIB", "IDFB", "BARB"]
FIRST = ["arjun", "priya", "rahul", "sneha", "vikram", "anita", "rohit", "kavya", "aman",
         "divya", "sanjay", "meera", "nikhil", "pooja", "farhan", "isha", "manav", "tara"]
MERCHANTS = ["swiggy", "zomato", "bigbasket", "amazon", "flipkart", "irctc", "bookmyshow",
             "jiomart", "dmart", "uber", "ola", "phonepestore", "blinkit"]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _vpa(rng: random.Random, tag: str = "") -> str:
    name = rng.choice(FIRST)
    return f"{name}{tag}{rng.randint(100, 9999)}@{rng.choice(BANKS).lower()}"


class Generator:
    def __init__(self, seed: int = 26) -> None:
        self.rng = random.Random(seed)
        self.accounts: dict[str, dict] = {}
        self.txns: list[dict] = []
        self._txn_seq = 0
        # outgoing count per account, so "dormant" recruits can be picked cheaply
        self._out_index: dict[str, list[int]] = {}
        self.window_end = datetime(2026, 9, 5, 18, 0, tzinfo=timezone.utc)
        self.window_start = self.window_end - timedelta(days=30)

    # ---------- primitives ----------

    def _next_txn_id(self) -> str:
        self._txn_seq += 1
        return f"TXN{self._txn_seq:07d}"

    def add_account(self, kind: str, **overrides) -> str:
        rng = self.rng
        if kind == "mule":
            acc = {
                "account_id": _vpa(rng, "x"),
                "account_age_days": rng.randint(4, 95),
                "kyc_tier": rng.choice([1, 1, 1, 2]),
                "account_type": "mule",
                "mule_archetype": "fresh",
                "is_mule": 1,
            }
        elif kind in ("merchant", "cashout"):
            acc = {
                "account_id": f"{rng.choice(MERCHANTS)}{rng.randint(1, 99)}@{rng.choice(BANKS).lower()}",
                "account_age_days": rng.randint(400, 3000),
                "kyc_tier": 3,
                "account_type": kind,
                "is_mule": 0,
            }
        else:
            acc = {
                "account_id": _vpa(rng),
                "account_age_days": rng.randint(120, 3600),
                "kyc_tier": rng.choice([2, 3, 3, 3]),
                "account_type": kind,
                "is_mule": 0,
            }
        acc.update(overrides)
        # VPAs are the primary key of the graph, so collisions must not merge accounts
        while acc["account_id"] in self.accounts:
            acc["account_id"] = _vpa(rng, "z")
        acc["bank"] = acc["account_id"].split("@")[-1].upper()
        self.accounts[acc["account_id"]] = acc
        return acc["account_id"]

    def add_txn(self, sender: str, receiver: str, amount: float, ts: datetime,
                mode: str = "P2P", chain_id: str | None = None,
                hop_index: int | None = None, is_fraud: int = 0) -> dict:
        txn = {
            "txn_id": self._next_txn_id(),
            "sender": sender,
            "receiver": receiver,
            "amount": round(amount, 2),
            "timestamp": ts.isoformat(),
            "mode": mode,
            "chain_id": chain_id or "",
            "hop_index": -1 if hop_index is None else hop_index,
            "is_fraud": is_fraud,
        }
        self.txns.append(txn)
        self._out_index.setdefault(sender, []).append(self._txn_seq)
        return txn

    def _random_time(self) -> datetime:
        span = int((self.window_end - self.window_start).total_seconds())
        ts = self.window_start + timedelta(seconds=self.rng.randint(0, span))
        # people transact mostly in waking hours; keeps temporal features realistic
        if self.rng.random() < 0.65:
            ts = ts.replace(hour=self.rng.choice([9, 10, 11, 13, 14, 18, 19, 20, 21]))
        return ts

    # ---------- population ----------

    def build_population(self, n_normal: int, n_merchants: int) -> None:
        for _ in range(n_normal):
            self.add_account("personal")
        for _ in range(n_merchants):
            self.add_account("merchant")

    def _people(self) -> list[str]:
        return [a for a, v in self.accounts.items() if v["account_type"] == "personal"]

    def _merchants(self) -> list[str]:
        return [a for a, v in self.accounts.items() if v["account_type"] == "merchant"]

    def recruit_mule(self) -> str:
        """Pick the account a ring would actually use for the next hop.

        Only a minority of real mule accounts are freshly opened for the purpose.
        Most are existing accounts that get rented, bought or socially recruited,
        and they arrive with a genuine history and an ordinary account age. If every
        mule here were newly created, account age alone would separate the classes
        and the classifier would learn nothing transferable.
        """
        rng = self.rng
        roll = rng.random()

        if roll < 0.35:
            # opened for the purpose - young, thin KYC
            return self.add_account("mule")

        # turned: an existing personal account keeps its age, KYC tier and history
        candidates = [a for a, v in self.accounts.items()
                      if v["account_type"] == "personal" and not v.get("is_mule")]
        if not candidates:
            return self.add_account("mule")

        if roll < 0.85:
            account = rng.choice(candidates)          # actively used account, turned
            archetype = "recruited"
        else:
            # dormant: little prior activity, reactivated to move money
            quiet = sorted(candidates, key=lambda a: len(self._out_index.get(a, [])))[:150]
            account = rng.choice(quiet or candidates)
            archetype = "dormant"

        meta = self.accounts[account]
        meta["is_mule"] = 1
        meta["mule_archetype"] = archetype
        return account

    def background_traffic(self, n_txns: int) -> None:
        """Ordinary P2P and P2M activity - the noise a real chain hides inside."""
        rng = self.rng
        people, merchants = self._people(), self._merchants()
        for _ in range(n_txns):
            ts = self._random_time()
            if rng.random() < 0.55:
                sender, receiver = rng.choice(people), rng.choice(merchants)
                amount = rng.choice([rng.uniform(40, 600), rng.uniform(600, 4000)])
                mode = "P2M"
            else:
                sender = rng.choice(people)
                receiver = rng.choice(people)
                if sender == receiver:
                    continue
                roll = rng.random()
                if roll < 0.05:
                    # legitimate high-value P2P - rent deposits, vehicle and property
                    # advances, family transfers. Without this tail, transaction size
                    # alone would separate fraud from background.
                    amount = rng.uniform(45000, 420000)
                elif roll < 0.55:
                    amount = rng.uniform(100, 2500)
                else:
                    amount = rng.uniform(2500, 30000)
                mode = "P2P"
            self.add_txn(sender, receiver, amount, ts, mode=mode)

    def legit_fast_forwarders(self, n_accounts: int) -> None:
        """False-positive pressure: honest accounts that receive and forward fast.

        Shared rent, a friend collecting for a group gift, a small trader settling
        with a supplier. Same temporal shape as a mule hop, no fraud behind it.
        """
        rng = self.rng
        people = self._people()
        for _ in range(n_accounts):
            hub = rng.choice(people)
            for _ in range(rng.randint(2, 5)):
                payer = rng.choice(people)
                onward = rng.choice(people)
                if len({hub, payer, onward}) < 3:
                    continue
                t_in = self._random_time()
                # some of these settle genuinely large sums, matching chain amounts
                amount = rng.uniform(3000, 45000) if rng.random() < 0.75 else rng.uniform(45000, 320000)
                self.add_txn(payer, hub, amount, t_in)
                gap = timedelta(minutes=rng.randint(5, 240))
                self.add_txn(hub, onward, amount * rng.uniform(0.80, 0.99), t_in + gap)

    # ---------- fraud ----------

    def inject_chain(self, index: int) -> dict:
        """One victim -> 3-6 mule hops -> holds the money or cashes out."""
        rng = self.rng
        chain_id = f"CHAIN{index:03d}"
        victim = rng.choice([a for a, v in self.accounts.items()
                             if v["account_type"] == "personal" and not v.get("is_mule")])
        n_hops = rng.randint(3, 6)

        mules: list[str] = []
        while len(mules) < n_hops:
            candidate = self.recruit_mule()
            if candidate != victim and candidate not in mules:
                mules.append(candidate)

        amount = rng.choice([rng.uniform(18000, 90000), rng.uniform(90000, 480000)])
        ts = self._random_time().replace(minute=rng.randint(0, 59))

        # most rings move within minutes; a patient minority sits on funds for hours
        # to duck velocity rules, which is what keeps the detector honest
        mean_gap = 22.0 if rng.random() < 0.78 else 260.0

        first = self.add_txn(victim, mules[0], amount, ts, chain_id=chain_id,
                             hop_index=0, is_fraud=1)
        current, current_amount, current_ts = mules[0], amount, ts

        for hop in range(1, n_hops):
            current_ts = current_ts + timedelta(minutes=int(rng.expovariate(1 / mean_gap)) + 2)
            # each mule skims a small cut; the rest moves on
            current_amount = current_amount * rng.uniform(0.75, 0.985)
            self.add_txn(current, mules[hop], current_amount, current_ts,
                         chain_id=chain_id, hop_index=hop, is_fraud=1)
            current = mules[hop]

        cashed_out = rng.random() < 0.4
        if cashed_out:
            current_ts = current_ts + timedelta(minutes=int(rng.expovariate(1 / 30.0)) + 3)
            sink = self.add_account("cashout")
            self.add_txn(current, sink, current_amount * rng.uniform(0.85, 1.0), current_ts,
                         mode=rng.choice(["ATM", "P2M"]), chain_id=chain_id,
                         hop_index=n_hops, is_fraud=1)
            end_node = sink
        else:
            end_node = current

        # mules also make small ordinary-looking payments so they are not pure conduits
        for mule in mules:
            for _ in range(rng.randint(0, 2)):
                self.add_txn(mule, rng.choice(self._merchants()),
                             rng.uniform(50, 900), self._random_time(), mode="P2M")

        return {
            "chain_id": chain_id,
            "victim": victim,
            "entry_txn_id": first["txn_id"],
            "mules": mules,
            "end_node": end_node,
            "hops": n_hops,
            "amount": round(amount, 2),
            "cashed_out": int(cashed_out),
        }

    # ---------- output ----------

    def run(self, n_normal: int, n_merchants: int, n_txns: int, n_chains: int,
            n_forwarders: int) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        self.build_population(n_normal, n_merchants)
        self.background_traffic(n_txns)
        self.legit_fast_forwarders(n_forwarders)
        chains = [self.inject_chain(i + 1) for i in range(n_chains)]

        txns = pd.DataFrame(self.txns).sort_values("timestamp").reset_index(drop=True)
        accounts = pd.DataFrame(self.accounts.values())
        truth = pd.DataFrame([{**c, "mules": "|".join(c["mules"])} for c in chains])
        return txns, accounts, truth


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the MuleTrace synthetic dataset.")
    ap.add_argument("--accounts", type=int, default=900)
    ap.add_argument("--merchants", type=int, default=120)
    ap.add_argument("--txns", type=int, default=14000)
    ap.add_argument("--chains", type=int, default=45)
    ap.add_argument("--forwarders", type=int, default=35)
    ap.add_argument("--seed", type=int, default=26)
    ap.add_argument("--out", type=Path, default=DATA_DIR)
    args = ap.parse_args()

    gen = Generator(seed=args.seed)
    txns, accounts, truth = gen.run(args.accounts, args.merchants, args.txns,
                                    args.chains, args.forwarders)
    args.out.mkdir(parents=True, exist_ok=True)
    txns.to_csv(args.out / "transactions.csv", index=False)
    accounts.to_csv(args.out / "accounts.csv", index=False)
    truth.to_csv(args.out / "ground_truth.csv", index=False)

    print(f"transactions : {len(txns):,}  ({int(txns.is_fraud.sum()):,} fraud edges)")
    print(f"accounts     : {len(accounts):,}  ({int(accounts.is_mule.sum()):,} mules)")
    print(f"chains       : {len(truth):,}  ({int(truth.cashed_out.sum())} cashed out)")
    print(f"written to   : {args.out}")


if __name__ == "__main__":
    main()
