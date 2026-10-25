"""Credit file stores behind the CreditFileStore port.

PostgresStore is the deployment store: payloads are the Pydantic
models serialized to JSONB, with migrations from db.py applied at
startup. SqliteCreditStore and InMemoryCreditStore are test doubles —
same contract, no deployment claims.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .errors import StoreUnavailableError
from .models import (
    AuditEvent,
    ComputationPack,
    CreditFile,
    CreditMemo,
    ExtractedValue,
    MemoDiff,
    MemoReviewEvent,
    PolicyReport,
    Spread,
    SpreadAdjustment,
)


class InMemoryCreditStore:
    def __init__(self) -> None:
        self._files: dict[str, CreditFile] = {}
        self._extractions: dict[str, list[ExtractedValue]] = {}
        self._audit: dict[str, list[AuditEvent]] = {}
        self._adjustments: dict[str, list[SpreadAdjustment]] = {}
        self._spreads: dict[str, dict[str, Spread]] = {}
        self._spread_order: dict[str, list[str]] = {}
        self._packs: dict[str, ComputationPack] = {}
        self._pack_order: dict[str, list[str]] = {}
        self._reports: dict[str, PolicyReport] = {}
        self._report_order: dict[str, list[str]] = {}
        self._memos: dict[str, CreditMemo] = {}
        self._memo_order: dict[str, list[str]] = {}
        self._review_events: dict[str, list[MemoReviewEvent]] = {}
        self._diffs: dict[str, MemoDiff] = {}
        self._diff_order: dict[str, list[str]] = {}

    def save_credit_file(self, credit_file: CreditFile) -> None:
        self._files[credit_file.credit_file_id] = credit_file.model_copy(deep=True)

    def get_credit_file(self, credit_file_id: str) -> CreditFile | None:
        cf = self._files.get(credit_file_id)
        return cf.model_copy(deep=True) if cf else None

    def list_credit_files(self) -> list[CreditFile]:
        return [cf.model_copy(deep=True) for cf in self._files.values()]

    def save_extractions(self, credit_file_id: str, values: list[ExtractedValue]) -> None:
        self._extractions.setdefault(credit_file_id, []).extend(values)

    def get_extractions(self, credit_file_id: str) -> list[ExtractedValue]:
        return list(self._extractions.get(credit_file_id, []))

    def append_audit(self, event: AuditEvent) -> None:
        self._audit.setdefault(event.credit_file_id, []).append(event)

    def list_audit(self, credit_file_id: str) -> list[AuditEvent]:
        return list(self._audit.get(credit_file_id, []))

    def save_adjustment(self, adjustment: SpreadAdjustment) -> None:
        self._adjustments.setdefault(adjustment.credit_file_id, []).append(
            adjustment.model_copy(deep=True)
        )

    def list_adjustments(self, credit_file_id: str) -> list[SpreadAdjustment]:
        return [
            a.model_copy(deep=True) for a in self._adjustments.get(credit_file_id, [])
        ]

    def save_spread(self, spread: Spread) -> None:
        by_id = self._spreads.setdefault(spread.credit_file_id, {})
        if spread.spread_id not in by_id:
            self._spread_order.setdefault(spread.credit_file_id, []).append(
                spread.spread_id
            )
        by_id[spread.spread_id] = spread.model_copy(deep=True)

    def get_latest_spread(self, credit_file_id: str) -> Spread | None:
        order = self._spread_order.get(credit_file_id, [])
        if not order:
            return None
        spread = self._spreads[credit_file_id][order[-1]]
        return spread.model_copy(deep=True)

    def list_spreads(self, credit_file_id: str) -> list[Spread]:
        by_id = self._spreads.get(credit_file_id, {})
        return [
            by_id[spread_id].model_copy(deep=True)
            for spread_id in self._spread_order.get(credit_file_id, [])
        ]

    def save_computation_pack(self, pack: ComputationPack) -> None:
        # Content-addressed: the same pack id is the same artefact.
        # Storing it twice records it once.
        if pack.pack_id in self._packs:
            return
        self._packs[pack.pack_id] = pack.model_copy(deep=True)
        self._pack_order.setdefault(pack.credit_file_id, []).append(pack.pack_id)

    def get_computation_pack(self, pack_id: str) -> ComputationPack | None:
        pack = self._packs.get(pack_id)
        return pack.model_copy(deep=True) if pack else None

    def list_computation_packs(self, credit_file_id: str) -> list[ComputationPack]:
        return [
            self._packs[pack_id].model_copy(deep=True)
            for pack_id in self._pack_order.get(credit_file_id, [])
        ]

    def save_policy_report(self, report: PolicyReport) -> None:
        # Content-addressed: the same report id is the same artefact.
        # Storing it twice records it once — a re-evaluation under a
        # newer pack version is a NEW report, never an overwrite.
        if report.report_id in self._reports:
            return
        self._reports[report.report_id] = report.model_copy(deep=True)
        self._report_order.setdefault(report.credit_file_id, []).append(
            report.report_id
        )

    def get_policy_report(self, report_id: str) -> PolicyReport | None:
        report = self._reports.get(report_id)
        return report.model_copy(deep=True) if report else None

    def list_policy_reports(self, credit_file_id: str) -> list[PolicyReport]:
        return [
            self._reports[report_id].model_copy(deep=True)
            for report_id in self._report_order.get(credit_file_id, [])
        ]

    def save_memo(self, memo: CreditMemo) -> None:
        # Content-addressed: the same memo id is the same artefact.
        # A memo only reaches the store after the figure verifier
        # has cleared it; storing it twice records it once.
        if memo.memo_id in self._memos:
            return
        self._memos[memo.memo_id] = memo.model_copy(deep=True)
        self._memo_order.setdefault(memo.credit_file_id, []).append(memo.memo_id)

    def get_memo(self, memo_id: str) -> CreditMemo | None:
        memo = self._memos.get(memo_id)
        return memo.model_copy(deep=True) if memo else None

    def list_memos(self, credit_file_id: str) -> list[CreditMemo]:
        return [
            self._memos[memo_id].model_copy(deep=True)
            for memo_id in self._memo_order.get(credit_file_id, [])
        ]

    def save_memo_review_event(self, event: MemoReviewEvent) -> None:
        # Append-only: a recorded gate action is never rewritten.
        self._review_events.setdefault(event.credit_file_id, []).append(
            event.model_copy(deep=True)
        )

    def list_memo_review_events(
        self, credit_file_id: str, memo_id: str | None = None
    ) -> list[MemoReviewEvent]:
        events = self._review_events.get(credit_file_id, [])
        return [
            e.model_copy(deep=True)
            for e in events
            if memo_id is None or e.memo_id == memo_id
        ]

    def save_memo_diff(self, diff: MemoDiff) -> None:
        # Content-addressed: the same diff id is the same record.
        if diff.diff_id in self._diffs:
            return
        self._diffs[diff.diff_id] = diff.model_copy(deep=True)
        self._diff_order.setdefault(diff.credit_file_id, []).append(diff.diff_id)

    def get_memo_diff(self, diff_id: str) -> MemoDiff | None:
        diff = self._diffs.get(diff_id)
        return diff.model_copy(deep=True) if diff else None

    def get_memo_diff_by_final(self, memo_id: str) -> MemoDiff | None:
        for diff in self._diffs.values():
            if diff.to_memo_id == memo_id:
                return diff.model_copy(deep=True)
        return None

    def list_memo_diffs(self, credit_file_id: str) -> list[MemoDiff]:
        return [
            self._diffs[diff_id].model_copy(deep=True)
            for diff_id in self._diff_order.get(credit_file_id, [])
        ]

    def ping(self) -> bool:
        return True


class SqliteCreditStore:
    """SQLite double: same contract, local file, no deployment role."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS credit_files (
                credit_file_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS extractions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                credit_file_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS adjustments (
                adjustment_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS spreads (
                spread_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS computation_packs (
                pack_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS policy_reports (
                report_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memos (
                memo_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memo_review_events (
                event_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, memo_id TEXT NOT NULL,
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memo_diffs (
                diff_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, from_memo_id TEXT NOT NULL,
                to_memo_id TEXT NOT NULL, payload TEXT NOT NULL);
            """
        )
        self._conn.commit()

    def save_credit_file(self, credit_file: CreditFile) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO credit_files (credit_file_id, payload) VALUES (?, ?) "
                "ON CONFLICT(credit_file_id) DO UPDATE SET payload = excluded.payload",
                (credit_file.credit_file_id, credit_file.model_dump_json()),
            )

    def get_credit_file(self, credit_file_id: str) -> CreditFile | None:
        row = self._conn.execute(
            "SELECT payload FROM credit_files WHERE credit_file_id = ?",
            (credit_file_id,),
        ).fetchone()
        return CreditFile.model_validate_json(row[0]) if row else None

    def list_credit_files(self) -> list[CreditFile]:
        rows = self._conn.execute("SELECT payload FROM credit_files").fetchall()
        return [CreditFile.model_validate_json(r[0]) for r in rows]

    def save_extractions(self, credit_file_id: str, values: list[ExtractedValue]) -> None:
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO extractions (credit_file_id, payload) VALUES (?, ?)",
                [(credit_file_id, v.model_dump_json()) for v in values],
            )

    def get_extractions(self, credit_file_id: str) -> list[ExtractedValue]:
        rows = self._conn.execute(
            "SELECT payload FROM extractions WHERE credit_file_id = ? ORDER BY id",
            (credit_file_id,),
        ).fetchall()
        return [ExtractedValue.model_validate_json(r[0]) for r in rows]

    def append_audit(self, event: AuditEvent) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit_events (event_id, credit_file_id, payload) VALUES (?, ?, ?)",
                (event.event_id, event.credit_file_id, event.model_dump_json()),
            )

    def list_audit(self, credit_file_id: str) -> list[AuditEvent]:
        rows = self._conn.execute(
            "SELECT payload FROM audit_events WHERE credit_file_id = ?",
            (credit_file_id,),
        ).fetchall()
        return [AuditEvent.model_validate_json(r[0]) for r in rows]

    def save_adjustment(self, adjustment: SpreadAdjustment) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO adjustments (adjustment_id, credit_file_id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(adjustment_id) DO NOTHING",
                (
                    adjustment.adjustment_id,
                    adjustment.credit_file_id,
                    adjustment.model_dump_json(),
                ),
            )

    def list_adjustments(self, credit_file_id: str) -> list[SpreadAdjustment]:
        rows = self._conn.execute(
            "SELECT payload FROM adjustments WHERE credit_file_id = ? ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [SpreadAdjustment.model_validate_json(r[0]) for r in rows]

    def save_spread(self, spread: Spread) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO spreads (spread_id, credit_file_id, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(spread_id) DO UPDATE SET payload = excluded.payload",
                (spread.spread_id, spread.credit_file_id, spread.model_dump_json()),
            )

    def get_latest_spread(self, credit_file_id: str) -> Spread | None:
        row = self._conn.execute(
            "SELECT payload FROM spreads WHERE credit_file_id = ? ORDER BY rowid DESC LIMIT 1",
            (credit_file_id,),
        ).fetchone()
        return Spread.model_validate_json(row[0]) if row else None

    def list_spreads(self, credit_file_id: str) -> list[Spread]:
        rows = self._conn.execute(
            "SELECT payload FROM spreads WHERE credit_file_id = ? ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [Spread.model_validate_json(r[0]) for r in rows]

    def save_computation_pack(self, pack: ComputationPack) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO computation_packs (pack_id, credit_file_id, payload) "
                "VALUES (?, ?, ?) ON CONFLICT(pack_id) DO NOTHING",
                (pack.pack_id, pack.credit_file_id, pack.model_dump_json()),
            )

    def get_computation_pack(self, pack_id: str) -> ComputationPack | None:
        row = self._conn.execute(
            "SELECT payload FROM computation_packs WHERE pack_id = ?",
            (pack_id,),
        ).fetchone()
        return ComputationPack.model_validate_json(row[0]) if row else None

    def list_computation_packs(self, credit_file_id: str) -> list[ComputationPack]:
        rows = self._conn.execute(
            "SELECT payload FROM computation_packs WHERE credit_file_id = ? "
            "ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [ComputationPack.model_validate_json(r[0]) for r in rows]

    def save_policy_report(self, report: PolicyReport) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO policy_reports (report_id, credit_file_id, payload) "
                "VALUES (?, ?, ?) ON CONFLICT(report_id) DO NOTHING",
                (report.report_id, report.credit_file_id, report.model_dump_json()),
            )

    def get_policy_report(self, report_id: str) -> PolicyReport | None:
        row = self._conn.execute(
            "SELECT payload FROM policy_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        return PolicyReport.model_validate_json(row[0]) if row else None

    def list_policy_reports(self, credit_file_id: str) -> list[PolicyReport]:
        rows = self._conn.execute(
            "SELECT payload FROM policy_reports WHERE credit_file_id = ? "
            "ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [PolicyReport.model_validate_json(r[0]) for r in rows]

    def save_memo(self, memo: CreditMemo) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memos (memo_id, credit_file_id, payload) "
                "VALUES (?, ?, ?) ON CONFLICT(memo_id) DO NOTHING",
                (memo.memo_id, memo.credit_file_id, memo.model_dump_json()),
            )

    def get_memo(self, memo_id: str) -> CreditMemo | None:
        row = self._conn.execute(
            "SELECT payload FROM memos WHERE memo_id = ?",
            (memo_id,),
        ).fetchone()
        return CreditMemo.model_validate_json(row[0]) if row else None

    def list_memos(self, credit_file_id: str) -> list[CreditMemo]:
        rows = self._conn.execute(
            "SELECT payload FROM memos WHERE credit_file_id = ? ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [CreditMemo.model_validate_json(r[0]) for r in rows]

    def save_memo_review_event(self, event: MemoReviewEvent) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memo_review_events (event_id, credit_file_id, memo_id, payload) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(event_id) DO NOTHING",
                (
                    event.event_id,
                    event.credit_file_id,
                    event.memo_id,
                    event.model_dump_json(),
                ),
            )

    def list_memo_review_events(
        self, credit_file_id: str, memo_id: str | None = None
    ) -> list[MemoReviewEvent]:
        if memo_id is None:
            rows = self._conn.execute(
                "SELECT payload FROM memo_review_events "
                "WHERE credit_file_id = ? ORDER BY rowid",
                (credit_file_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT payload FROM memo_review_events "
                "WHERE credit_file_id = ? AND memo_id = ? ORDER BY rowid",
                (credit_file_id, memo_id),
            ).fetchall()
        return [MemoReviewEvent.model_validate_json(r[0]) for r in rows]

    def save_memo_diff(self, diff: MemoDiff) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO memo_diffs (diff_id, credit_file_id, from_memo_id, to_memo_id, payload) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(diff_id) DO NOTHING",
                (
                    diff.diff_id,
                    diff.credit_file_id,
                    diff.from_memo_id,
                    diff.to_memo_id,
                    diff.model_dump_json(),
                ),
            )

    def get_memo_diff(self, diff_id: str) -> MemoDiff | None:
        row = self._conn.execute(
            "SELECT payload FROM memo_diffs WHERE diff_id = ?",
            (diff_id,),
        ).fetchone()
        return MemoDiff.model_validate_json(row[0]) if row else None

    def get_memo_diff_by_final(self, memo_id: str) -> MemoDiff | None:
        row = self._conn.execute(
            "SELECT payload FROM memo_diffs WHERE to_memo_id = ?",
            (memo_id,),
        ).fetchone()
        return MemoDiff.model_validate_json(row[0]) if row else None

    def list_memo_diffs(self, credit_file_id: str) -> list[MemoDiff]:
        rows = self._conn.execute(
            "SELECT payload FROM memo_diffs WHERE credit_file_id = ? "
            "ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [MemoDiff.model_validate_json(r[0]) for r in rows]

    def ping(self) -> bool:
        try:
            self._conn.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False


class PostgresCreditStore:
    """Deployment store over psycopg 3 + the numbered migrations."""

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise StoreUnavailableError("psycopg is not installed") from exc
        self._psycopg = psycopg
        self._dict_row = dict_row
        self._url = database_url

    def _connect(self):  # noqa: ANN202 - psycopg connection, untyped alias
        return self._psycopg.connect(self._url, row_factory=self._dict_row)

    def save_credit_file(self, credit_file: CreditFile) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO credit_files (credit_file_id, borrower_name, facility_type, status, payload, updated_at) "
                "VALUES (%s, %s, %s, %s, %s::jsonb, now()) "
                "ON CONFLICT (credit_file_id) DO UPDATE SET borrower_name = EXCLUDED.borrower_name, "
                "facility_type = EXCLUDED.facility_type, status = EXCLUDED.status, "
                "payload = EXCLUDED.payload, updated_at = now()",
                (
                    credit_file.credit_file_id,
                    credit_file.borrower.legal_name,
                    credit_file.facility.facility_type.value,
                    credit_file.status.value,
                    credit_file.model_dump_json(),
                ),
            )

    def get_credit_file(self, credit_file_id: str) -> CreditFile | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM credit_files WHERE credit_file_id = %s",
                (credit_file_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return CreditFile.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_credit_files(self) -> list[CreditFile]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM credit_files").fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                CreditFile.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_extractions(self, credit_file_id: str, values: list[ExtractedValue]) -> None:
        with self._connect() as conn:
            for v in values:
                conn.execute(
                    "INSERT INTO extractions (credit_file_id, field, period_label, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    (credit_file_id, v.field, v.period_label, v.model_dump_json()),
                )

    def get_extractions(self, credit_file_id: str) -> list[ExtractedValue]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM extractions WHERE credit_file_id = %s ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                ExtractedValue.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def append_audit(self, event: AuditEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO audit_events (event_id, credit_file_id, actor, action, payload) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (event_id) DO NOTHING",
                (
                    event.event_id,
                    event.credit_file_id,
                    event.actor,
                    event.action,
                    event.model_dump_json(),
                ),
            )

    def list_audit(self, credit_file_id: str) -> list[AuditEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM audit_events WHERE credit_file_id = %s ORDER BY at",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                AuditEvent.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_adjustment(self, adjustment: SpreadAdjustment) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO adjustments (adjustment_id, credit_file_id, line_key, fiscal_year, payload) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (adjustment_id) DO NOTHING",
                (
                    adjustment.adjustment_id,
                    adjustment.credit_file_id,
                    adjustment.line_key,
                    adjustment.fiscal_year,
                    adjustment.model_dump_json(),
                ),
            )

    def list_adjustments(self, credit_file_id: str) -> list[SpreadAdjustment]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM adjustments WHERE credit_file_id = %s ORDER BY recorded_at, adjustment_id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                SpreadAdjustment.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_spread(self, spread: Spread) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO spreads (spread_id, credit_file_id, payload) "
                "VALUES (%s, %s, %s::jsonb) ON CONFLICT (spread_id) DO NOTHING",
                (spread.spread_id, spread.credit_file_id, spread.model_dump_json()),
            )

    def get_latest_spread(self, credit_file_id: str) -> Spread | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM spreads WHERE credit_file_id = %s ORDER BY id DESC LIMIT 1",
                (credit_file_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return Spread.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_spreads(self, credit_file_id: str) -> list[Spread]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM spreads WHERE credit_file_id = %s ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                Spread.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_computation_pack(self, pack: ComputationPack) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO computation_packs (pack_id, credit_file_id, payload) "
                "VALUES (%s, %s, %s::jsonb) ON CONFLICT (pack_id) DO NOTHING",
                (pack.pack_id, pack.credit_file_id, pack.model_dump_json()),
            )

    def get_computation_pack(self, pack_id: str) -> ComputationPack | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM computation_packs WHERE pack_id = %s",
                (pack_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return ComputationPack.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_computation_packs(self, credit_file_id: str) -> list[ComputationPack]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM computation_packs WHERE credit_file_id = %s "
                "ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                ComputationPack.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_policy_report(self, report: PolicyReport) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO policy_reports (report_id, credit_file_id, policy_version, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (report_id) DO NOTHING",
                (
                    report.report_id,
                    report.credit_file_id,
                    report.policy_version,
                    report.model_dump_json(),
                ),
            )

    def get_policy_report(self, report_id: str) -> PolicyReport | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM policy_reports WHERE report_id = %s",
                (report_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return PolicyReport.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_policy_reports(self, credit_file_id: str) -> list[PolicyReport]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM policy_reports WHERE credit_file_id = %s "
                "ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                PolicyReport.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_memo(self, memo: CreditMemo) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO memos (memo_id, credit_file_id, policy_version, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (memo_id) DO NOTHING",
                (
                    memo.memo_id,
                    memo.credit_file_id,
                    memo.policy_version,
                    memo.model_dump_json(),
                ),
            )

    def get_memo(self, memo_id: str) -> CreditMemo | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM memos WHERE memo_id = %s",
                (memo_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return CreditMemo.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_memos(self, credit_file_id: str) -> list[CreditMemo]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM memos WHERE credit_file_id = %s "
                "ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                CreditMemo.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_memo_review_event(self, event: MemoReviewEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO memo_review_events (event_id, credit_file_id, memo_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (event_id) DO NOTHING",
                (
                    event.event_id,
                    event.credit_file_id,
                    event.memo_id,
                    event.model_dump_json(),
                ),
            )

    def list_memo_review_events(
        self, credit_file_id: str, memo_id: str | None = None
    ) -> list[MemoReviewEvent]:
        with self._connect() as conn:
            if memo_id is None:
                rows = conn.execute(
                    "SELECT payload FROM memo_review_events "
                    "WHERE credit_file_id = %s ORDER BY id",
                    (credit_file_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM memo_review_events "
                    "WHERE credit_file_id = %s AND memo_id = %s "
                    "ORDER BY id",
                    (credit_file_id, memo_id),
                ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                MemoReviewEvent.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_memo_diff(self, diff: MemoDiff) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO memo_diffs (diff_id, credit_file_id, from_memo_id, to_memo_id, payload) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (diff_id) DO NOTHING",
                (
                    diff.diff_id,
                    diff.credit_file_id,
                    diff.from_memo_id,
                    diff.to_memo_id,
                    diff.model_dump_json(),
                ),
            )

    def get_memo_diff(self, diff_id: str) -> MemoDiff | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM memo_diffs WHERE diff_id = %s",
                (diff_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return MemoDiff.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def get_memo_diff_by_final(self, memo_id: str) -> MemoDiff | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM memo_diffs WHERE to_memo_id = %s",
                (memo_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return MemoDiff.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_memo_diffs(self, credit_file_id: str) -> list[MemoDiff]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM memo_diffs WHERE credit_file_id = %s "
                "ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                MemoDiff.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False
