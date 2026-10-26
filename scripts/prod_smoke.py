#!/usr/bin/env python3
"""Production smoke: the deployed profile, walked end to end (R16).

Boots the real API with ``DATABASE_URL`` pointing at a fresh
Postgres database (migrations run at startup, exactly as in
production) and walks one credit file through the whole
lifecycle over HTTP:

    open file → documents → extraction → spread → computation →
    policy → memo draft + verify → officer gate → committee
    approve → closing → covenant set + schedule → FY2026
    statements → a covenant test run (with a real failure and a
    real breach case)

Every step asserts the state it should have produced; a mismatch
fails the step, and any failed step exits non-zero. The smoke
database is dropped afterwards.

    DATABASE_URL=<redacted> python scripts/prod_smoke.py

Exit codes: 0 = every step passed; 1 = a step failed;
2 = DATABASE_URL is not set (nothing was smoked).
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import psycopg  # noqa: E402

# ------------------------------------------------------------------
# The Calloway fixture — the same committee-ready borrower the
# suite uses (tests/test_committee_gate.py): three audited years
# with full cash-flow pages, FY2025 DSCR 1.3125, leverage 5.0000.
# FY2026 is the hand-built failing year from the covenant-testing
# round: DSCR 0.9143 against the 1.25 floor.
# ------------------------------------------------------------------

_BS = (
    "cash_and_equivalents", "accounts_receivable", "inventory",
    "total_current_assets", "net_fixed_assets", "total_assets",
    "accounts_payable", "current_portion_ltd",
    "total_current_liabilities", "long_term_debt",
    "total_liabilities", "total_equity",
)
_IS = (
    "revenue", "cost_of_goods_sold", "gross_profit",
    "operating_expenses", "ebit", "interest_expense", "net_income",
)
_CF = (
    "depreciation_and_amortization", "change_in_receivables",
    "change_in_inventory", "change_in_payables",
    "net_cash_from_operations", "capital_expenditures",
    "net_cash_from_investing", "debt_proceeds", "debt_repayments",
    "net_cash_from_financing", "net_change_in_cash",
)

FIGURES: dict[int, dict[str, int]] = {
    2023: dict(zip(_BS, (
        200000, 800000, 1000000, 2000000, 3000000, 5000000,
        650000, 200000, 1100000, 2900000, 4000000, 1000000,
    )), **dict(zip(_IS, (
        7500000, 5250000, 2250000, 1970000, 280000, 90000, 145000,
    ))), **dict(zip(_CF, (
        100000, -40000, -50000, 30000, 300000, 50000, -50000,
        50000, -180000, -130000, 120000,
    )))),
    2024: dict(zip(_BS, (
        250000, 840000, 1110000, 2200000, 3300000, 5500000,
        700000, 220000, 1200000, 3300000, 4500000, 1000000,
    )), **dict(zip(_IS, (
        8200000, 5740000, 2460000, 2140000, 320000, 95000, 170000,
    ))), **dict(zip(_CF, (
        110000, -40000, -110000, 50000, 350000, 55000, -55000,
        80000, -200000, -120000, 175000,
    )))),
    2025: dict(zip(_BS, (
        300000, 900000, 1200000, 2400000, 3600000, 6000000,
        800000, 240000, 1400000, 3600000, 5000000, 1000000,
    )), **dict(zip(_IS, (
        9000000, 6300000, 2700000, 2400000, 300000, 100000, 150000,
    ))), **dict(zip(_CF, (
        120000, -60000, -90000, 100000, 380000, 60000, -60000,
        100000, -220000, -120000, 200000,
    )))),
    2026: dict(zip(_BS, (
        260000, 950000, 1300000, 2510000, 3740000, 6250000,
        850000, 260000, 1450000, 3650000, 5100000, 1150000,
    )), **dict(zip(_IS, (
        8600000, 6020000, 2580000, 2380000, 200000, 110000, 60000,
    ))), **dict(zip(_CF, (
        120000, -50000, -100000, 50000, 80000, 260000, -260000,
        270000, -200000, 70000, -110000,
    )))),
}

BORROWER = {
    "legal_name": "Calloway Precision Manufacturing, Inc.",
    "ein_last4": "4417",
    "segment": "manufacturing",
    "state_of_formation": "NC",
    "years_in_business": 14,
    "primary_contact_name": "Dana Whitfield",
    "primary_contact_email": "dana.whitfield@example.com",
}
FACILITY = {
    "facility_type": "term_loan",
    "amount": "1500000",
    "purpose": "CNC machining line expansion",
    "term_months": 60,
    "collateral_description": "Machining equipment and receivables",
    "guarantors": [],
}

RM = "rm-priya-nair"
RM_ROLE = "relationship_manager"
OFFICER = "officer-amara-osei"
OFFICER_ROLE = "credit_officer"
CHAIR = "chair-eleanor-voss"
CHAIR_ROLE = "committee_chair"
MEMBER = "member-tomas-lindqvist"
MEMBER_ROLE = "committee_member"


def statement_document(year: int) -> bytes:
    from commercial_credit.spreading import TEMPLATE_BY_FIELD

    figures = FIGURES[year]

    def page(number, title, fields):
        return {
            "page": number, "title": title,
            "lines": [
                {
                    "line": i,
                    "label": TEMPLATE_BY_FIELD[f].label,
                    "value": figures[f],
                }
                for i, f in enumerate(fields, start=1)
            ],
        }

    return json.dumps({
        "document_type": "financial_statement",
        "borrower": BORROWER["legal_name"],
        "fiscal_year": year, "period_label": f"FY{year}",
        "basis": "audited",
        "pages": [
            page(1, "Balance Sheet", _BS),
            page(2, "Income Statement", _IS),
            page(3, "Cash Flow Statement", _CF),
        ],
    }).encode()


def tax_document() -> bytes:
    from commercial_credit.spreading import TEMPLATE_BY_FIELD

    figures = FIGURES[2025]
    return json.dumps({
        "document_type": "tax_return",
        "borrower": BORROWER["legal_name"],
        "fiscal_year": 2025, "period_label": "FY2025",
        "pages": [{
            "page": 1, "title": "Income Statement (as filed)",
            "lines": [
                {
                    "line": i,
                    "label": TEMPLATE_BY_FIELD[f].label,
                    "value": figures[f],
                }
                for i, f in enumerate(_IS, start=1)
            ],
        }],
    }).encode()


def documents() -> list[tuple[str, str, bytes, str | None]]:
    docs: list[tuple[str, str, bytes, str | None]] = [
        (
            "financial_statement", f"calloway-fy{year}-statements.json",
            statement_document(year), f"FY{year}",
        )
        for year in (2023, 2024, 2025)
    ]
    docs.append((
        "tax_return", "calloway-business-tax-fy2025.json",
        tax_document(), "FY2025",
    ))
    docs.append((
        "tax_return", "guarantor-personal-tax.json",
        json.dumps(
            {"document_type": "tax_return",
             "pages": [{"page": 1, "lines": []}]}
        ).encode(),
        "FY2025",
    ))
    for kind, filename in (
        ("personal_financial_statement", "guarantor-pfs.json"),
        ("org_document", "articles-of-incorporation.json"),
        ("debt_schedule", "debt-schedule.json"),
        ("insurance_certificate", "insurance-certificate.json"),
        ("projections", "projections-fy2026.json"),
    ):
        docs.append((kind, filename, json.dumps({}).encode(), None))
    return docs


# ------------------------------------------------------------------
# HTTP plumbing + step accounting
# ------------------------------------------------------------------

_steps = 0
_passed = 0


def call(method: str, port: int, path: str, body: dict | None = None):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def step(label: str, ok: bool, detail: str = "") -> bool:
    global _steps, _passed
    _steps += 1
    if ok:
        _passed += 1
    print(f"  [{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail and not ok else ""))
    return ok


def expect(label: str, cond: bool, detail: str = "") -> None:
    if not step(label, cond, detail):
        raise AssertionError(label)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 90.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            status, _ = call("GET", port, "/health")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("prod_smoke: DATABASE_URL is not set — nothing smoked.")
        return 2

    smoke_db = f"credit_smoke_{uuid.uuid4().hex[:12]}"
    base = database_url.rsplit("/", 1)[0]
    smoke_url = f"{base}/{smoke_db}"
    with psycopg.connect(database_url, autocommit=True) as admin:
        admin.execute(
            f'CREATE DATABASE "{smoke_db}" TEMPLATE template0 '
            "ENCODING 'UTF8'"
        )
    print(f"prod_smoke: database {smoke_db} created")

    port = free_port()
    env = {
        k: v for k, v in os.environ.items()
        if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")
    }
    env["DATABASE_URL"] = smoke_url
    env["NO_PROXY"] = "127.0.0.1,localhost"
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "commercial_credit.api:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    failed = False
    try:
        if not wait_ready(port, proc):
            print("prod_smoke: the API did not come up on Postgres")
            return 1

        # -- boot profile -----------------------------------------
        status, readiness = call("GET", port, "/readiness")
        step(
            "the app booted on the Postgres profile",
            status == 200 and readiness.get("store_profile") == "postgres",
            f"readiness={readiness}",
        )

        # -- open file → documents → extraction → spread ----------
        status, opened = call(
            "POST", port, "/applications",
            {"borrower": BORROWER, "facility": FACILITY},
        )
        expect("credit file opened", status == 201, f"{status} {opened}")
        cf = opened["credit_file_id"]

        docs_ok = True
        for kind, filename, data, period in documents():
            status, _ = call(
                "POST", port, f"/applications/{cf}/documents",
                {
                    "kind": kind, "filename": filename,
                    "content_b64": base64.b64encode(data).decode("ascii"),
                    "period_label": period,
                },
            )
            docs_ok = docs_ok and status == 201
        step("all ten documents registered", docs_ok)

        status, extracted = call("POST", port, f"/applications/{cf}/extract")
        step(
            "extraction ran",
            status == 200 and extracted.get("extraction_count", 0) > 0,
            f"{status} {extracted}",
        )

        status, spread = call("GET", port, f"/applications/{cf}/spread")
        periods = set(spread.get("periods", [])) \
            if isinstance(spread, dict) else set()
        step(
            "spread built for FY2023–FY2025",
            status == 200 and {2023, 2024, 2025} <= periods,
            f"{status} periods={sorted(periods)}",
        )

        # -- computation → policy → memo --------------------------
        status, pack = call(
            "POST", port, f"/applications/{cf}/computations", {}
        )
        step("computation pack produced", status == 201, f"{status}")
        pack_id = pack.get("computation_pack_id")

        status, report = call(
            "POST", port, f"/applications/{cf}/policy-evaluations",
            {"pack_version": "v1", "as_of": "2026-06-30"},
        )
        step("policy evaluation produced", status == 201, f"{status}")
        report_id = report.get("report_id")

        status, memo = call(
            "POST", port, f"/applications/{cf}/memos",
            {"report_id": report_id},
        )
        step("memo composed", status == 201, f"{status} {memo}")
        draft_id = memo["memo_id"]

        status, verification = call(
            "GET", port,
            f"/applications/{cf}/memos/{draft_id}/verification",
        )
        step(
            "memo verification passes",
            status == 200 and verification.get("verified") is True,
            f"{status} {verification}",
        )

        # -- officer gate ------------------------------------------
        base = f"/applications/{cf}/memos/{draft_id}"
        status, _ = call(
            "POST", port, f"{base}/submit",
            {"actor": RM, "actor_role": RM_ROLE},
        )
        step("memo submitted by the RM", status in (200, 201), f"{status}")
        status, final = call(
            "POST", port, f"{base}/approve",
            {"officer_id": OFFICER, "officer_role": OFFICER_ROLE},
        )
        step("officer approved the memo", status == 201, f"{status} {final}")
        final_id = final["memo_id"]

        # -- committee ----------------------------------------------
        cbase = f"/applications/{cf}/memos/{final_id}/committee"
        status, convened = call(
            "POST", port, f"{cbase}/convene",
            {"actor": OFFICER, "actor_role": OFFICER_ROLE},
        )
        step(
            "committee convened",
            status == 200 and convened.get("status") == "in_committee",
            f"{status} {convened}",
        )
        status, closing = call(
            "POST", port, f"/applications/{cf}/closing",
            {
                "closing_date": "2026-07-01",
                "actor": OFFICER, "actor_role": OFFICER_ROLE,
            },
        )
        step("closing date recorded", status == 201, f"{status} {closing}")
        status, decision = call(
            "POST", port, f"{cbase}/decide",
            {
                "recorder_id": CHAIR, "recorder_role": CHAIR_ROLE,
                "members": [
                    {"member_id": CHAIR, "member_role": CHAIR_ROLE},
                    {"member_id": MEMBER, "member_role": MEMBER_ROLE},
                ],
                "outcome": "approve_with_conditions",
                "conditions": [
                    {
                        "condition_id": "", "type": "financial_covenant",
                        "metric_key": "dscr", "operator": ">=",
                        "threshold": "1.25", "due_cadence": "annual",
                    },
                    {
                        "condition_id": "",
                        "type": "reporting_requirement",
                        "report_what": "Annual audited financial statements",
                        "due_cadence": "annual",
                    },
                ],
            },
        )
        step(
            "committee approved with conditions",
            status == 201 and decision.get("outcome")
            == "approve_with_conditions",
            f"{status} {decision}",
        )

        # -- covenant set + schedule --------------------------------
        status, sets = call("GET", port, f"/applications/{cf}/covenant-sets")
        step(
            "exactly one active covenant set exists",
            status == 200 and len(sets) == 1
            and sets[0].get("state") == "active",
            f"{status} {sets}",
        )
        status, schedule = call(
            "GET", port,
            f"/applications/{cf}/covenant-schedule?as_of=2026-10-05",
        )
        instances = [
            i for s in schedule.get("sets", []) for i in s.get("instances", [])
        ] if isinstance(schedule, dict) else []
        step(
            "covenant schedule projects test instances",
            status == 200 and len(instances) > 0,
            f"{status} instances={len(instances)}",
        )

        # -- FY2026 statements → a covenant test run -----------------
        status, _ = call(
            "POST", port, f"/applications/{cf}/documents",
            {
                "kind": "financial_statement",
                "filename": "calloway-fy2026-statements.json",
                "content_b64": base64.b64encode(
                    statement_document(2026)
                ).decode("ascii"),
                "period_label": "FY2026",
            },
        )
        step("FY2026 statements delivered", status == 201, f"{status}")
        status, _ = call("POST", port, f"/applications/{cf}/extract")
        step("FY2026 extraction ran", status == 200, f"{status}")

        status, ran = call(
            "POST", port, f"/applications/{cf}/covenant-tests/run",
            {"as_of": "2027-03-01"},
        )
        results = ran.get("results", []) if isinstance(ran, dict) else []
        outcomes = {r.get("outcome") for r in results}
        step(
            "covenant test run recorded results, incl. an honest failure",
            status == 200 and len(results) > 0 and "fail" in outcomes,
            f"{status} outcomes={sorted(o for o in outcomes if o)}",
        )
        step(
            "the failing test opened a breach case",
            len(ran.get("breaches_opened", [])) >= 1,
            f"breaches={ran.get('breaches_opened')}",
        )
        status, recorded = call(
            "GET", port, f"/applications/{cf}/covenant-test-results"
        )
        step(
            "test results read back from the store",
            status == 200 and len(recorded) > 0,
            f"{status} count={len(recorded) if isinstance(recorded, list) else recorded}",
        )
    except AssertionError as exc:
        failed = True
        print(f"prod_smoke: aborted at step: {exc}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        with psycopg.connect(database_url, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (smoke_db,),
            )
            admin.execute(f'DROP DATABASE IF EXISTS "{smoke_db}"')
        print(f"prod_smoke: database {smoke_db} dropped")

    print(f"SMOKE: {_passed}/{_steps} steps passed")
    return 0 if (_passed == _steps and not failed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
