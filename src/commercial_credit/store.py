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
from .models import AuditEvent, CreditFile, ExtractedValue, Spread, SpreadAdjustment


class InMemoryCreditStore:
    def __init__(self) -> None:
        self._files: dict[str, CreditFile] = {}
        self._extractions: dict[str, list[ExtractedValue]] = {}
        self._audit: dict[str, list[AuditEvent]] = {}
        self._adjustments: dict[str, list[SpreadAdjustment]] = {}
        self._spreads: dict[str, dict[str, Spread]] = {}
        self._spread_order: dict[str, list[str]] = {}

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

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False
