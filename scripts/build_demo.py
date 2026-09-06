"""Write frontend/demo.json — a read-only snapshot of a full investigation.

The console normally traces against a live FastAPI service. On a static host
there is none, and an empty console demonstrates nothing. This bakes one real
workspace — the chains, their hops and nodes, the rule-engine alerts and the
validation figures — so the deployed console can render an actual case.

It is a snapshot, not a simulation: everything in it came out of the real
detection pipeline. What it cannot do is trace an arbitrary transaction, rescan,
or record a verdict, because those need the service. The console says so.

Regenerate after changing detection:

    .venv/Scripts/python.exe scripts/build_demo.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import evaluate                        # noqa: E402
from workspace import build_workspace  # noqa: E402

REFERENCE_SEED = 26
MAX_CHAINS = 30          # keeps the payload small enough to serve from a CDN


def main() -> int:
    ws = build_workspace(REFERENCE_SEED)

    # decorate exactly as the API does, so the payload matches /api/chains
    import app as api
    ws.rescan(api._decorate)

    chains = ws.visible_chains()[:MAX_CHAINS]
    live = [c for c in chains if c["recoverable"]]
    gaps = sorted(c["elapsed_minutes"] for c in chains) or [0]
    flagged = int((ws.model.scores >= 0.5).sum()) if ws.model.scores is not None else 0

    overview = {
        "dataset": ws.label,
        "seed": ws.seed,
        "transactions": int(len(ws.graph.txns)),
        "accounts": int(len(ws.graph.accounts)),
        "active_chains": len(chains),
        "freezable_chains": len(live),
        "cashed_out_chains": len(chains) - len(live),
        "dismissed_chains": 0,
        "funds_recoverable": round(sum(c["amount_at_end"] for c in live), 2),
        "funds_lost": round(sum(c["amount_at_end"] for c in chains
                                if not c["recoverable"]), 2),
        "median_chain_minutes": gaps[len(gaps) // 2],
        "accounts_flagged": flagged,
        "mule_accounts_known": int(ws.features["is_mule"].sum()),
        "injected_chains": int(len(ws.truth)),
        "review": {"chains_reviewed": 0, "entries_logged": 0, "counts": {}},
        "rules": ws.rule_summary,
        "accounts_monitored": ws.rule_summary.get("accounts_monitored", 0),
        "model": ws.report.as_dict(),
        "baseline": ws.model.baseline_report,
    }

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "note": "Snapshot of a real detection run on the reference dataset. Served "
                "when no backend is reachable. Tracing, rescanning and recording "
                "verdicts need the live service.",
        "overview": overview,
        "chains": chains,
        "rules": {"summary": ws.rule_summary,
                  "alerts": [a.as_dict() for a in ws.alerts[:60]]},
        "evaluation": evaluate.compute(ws.graph, ws.truth, ws.model, ws.report),
    }

    out = ROOT / "frontend" / "demo.json"
    out.write_text(json.dumps(snapshot, separators=(",", ":")), encoding="utf-8")
    size_kb = out.stat().st_size / 1024

    print(f"wrote {out.relative_to(ROOT)}  ({size_kb:.0f} KB)")
    print(f"  dataset {overview['dataset']}  {overview['transactions']:,} txns  "
          f"{overview['accounts']:,} accounts")
    print(f"  {len(chains)} chains  ·  {overview['accounts_monitored']} accounts monitored  "
          f"·  {len(snapshot['rules']['alerts'])} alerts")
    if size_kb > 4000:
        print("  WARNING: large for a CDN payload — lower MAX_CHAINS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
