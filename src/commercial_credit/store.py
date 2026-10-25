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
    AnnualReviewOutcome,
    AnnualReviewPack,
    AuditEvent,
    BreachCase,
    CommitteeDecision,
    CommitteeReferral,
    ComputationPack,
    CovenantAmendment,
    CovenantSet,
    CovenantTestResult,
    CreditFile,
    CreditMemo,
    ExtractedValue,
    MemoDiff,
    MemoReviewEvent,
    PolicyReport,
    RatingAssignment,
    RatingOverride,
    ReadinessReport,
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
        self._readiness: dict[str, ReadinessReport] = {}
        self._readiness_order: dict[str, list[str]] = {}
        self._memos: dict[str, CreditMemo] = {}
        self._memo_order: dict[str, list[str]] = {}
        self._review_events: dict[str, list[MemoReviewEvent]] = {}
        self._diffs: dict[str, MemoDiff] = {}
        self._diff_order: dict[str, list[str]] = {}
        self._decisions: dict[str, CommitteeDecision] = {}
        self._decision_order: dict[str, list[str]] = {}
        self._covenant_sets: dict[str, CovenantSet] = {}
        self._covenant_set_order: dict[str, list[str]] = {}
        self._amendments: dict[str, list[CovenantAmendment]] = {}
        self._test_results: dict[str, CovenantTestResult] = {}
        self._test_result_order: dict[str, list[str]] = {}
        self._breaches: dict[str, BreachCase] = {}
        self._breach_order: dict[str, list[str]] = {}
        self._review_packs: dict[str, AnnualReviewPack] = {}
        self._review_pack_order: dict[str, list[str]] = {}
        self._review_outcomes: dict[str, AnnualReviewOutcome] = {}
        self._review_outcome_order: dict[str, list[str]] = {}
        self._referrals: dict[str, CommitteeReferral] = {}
        self._referral_order: dict[str, list[str]] = {}
        self._rating_assignments: dict[str, RatingAssignment] = {}
        self._rating_assignment_order: dict[str, list[str]] = {}
        self._rating_overrides: dict[str, RatingOverride] = {}
        self._rating_override_order: dict[str, list[str]] = {}

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

    def save_readiness_report(self, report: ReadinessReport) -> None:
        # Content-addressed, like the policy reports: storing the
        # same report twice records it once.
        if report.report_id in self._readiness:
            return
        self._readiness[report.report_id] = report.model_copy(deep=True)
        self._readiness_order.setdefault(report.credit_file_id, []).append(
            report.report_id
        )

    def get_readiness_report(
        self, report_id: str
    ) -> ReadinessReport | None:
        report = self._readiness.get(report_id)
        return report.model_copy(deep=True) if report else None

    def list_readiness_reports(
        self, credit_file_id: str
    ) -> list[ReadinessReport]:
        return [
            self._readiness[report_id].model_copy(deep=True)
            for report_id in self._readiness_order.get(credit_file_id, [])
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

    def save_committee_decision(self, decision: CommitteeDecision) -> None:
        # Append-only and content-addressed: a recorded decision is
        # never rewritten; a deferral superseded at re-presentment
        # is a second record, not an edit of the first.
        if decision.decision_id in self._decisions:
            return
        self._decisions[decision.decision_id] = decision.model_copy(deep=True)
        self._decision_order.setdefault(decision.credit_file_id, []).append(
            decision.decision_id
        )

    def get_committee_decision(
        self, decision_id: str
    ) -> CommitteeDecision | None:
        decision = self._decisions.get(decision_id)
        return decision.model_copy(deep=True) if decision else None

    def list_committee_decisions(
        self, credit_file_id: str, memo_id: str | None = None
    ) -> list[CommitteeDecision]:
        return [
            self._decisions[decision_id].model_copy(deep=True)
            for decision_id in self._decision_order.get(credit_file_id, [])
            if memo_id is None
            or self._decisions[decision_id].memo_id == memo_id
        ]

    def save_covenant_set(self, covenant_set: CovenantSet) -> None:
        # Insert-once, like every content-addressed artefact: the
        # set a decision created is never rewritten. Amendments
        # are separate artefacts beside it.
        if covenant_set.set_id in self._covenant_sets:
            return
        self._covenant_sets[covenant_set.set_id] = covenant_set.model_copy(
            deep=True
        )
        self._covenant_set_order.setdefault(
            covenant_set.credit_file_id, []
        ).append(covenant_set.set_id)

    def get_covenant_set(self, set_id: str) -> CovenantSet | None:
        covenant_set = self._covenant_sets.get(set_id)
        return covenant_set.model_copy(deep=True) if covenant_set else None

    def list_covenant_sets(
        self, credit_file_id: str
    ) -> list[CovenantSet]:
        return [
            self._covenant_sets[set_id].model_copy(deep=True)
            for set_id in self._covenant_set_order.get(credit_file_id, [])
        ]

    def save_covenant_amendment(
        self, amendment: CovenantAmendment
    ) -> None:
        # Append-only: the amendment record of a set is its full
        # history; nothing here is updated or removed.
        existing = self._amendments.setdefault(amendment.credit_file_id, [])
        if any(a.amendment_id == amendment.amendment_id for a in existing):
            return
        existing.append(amendment.model_copy(deep=True))

    def list_covenant_amendments(
        self, credit_file_id: str
    ) -> list[CovenantAmendment]:
        return [
            a.model_copy(deep=True)
            for a in self._amendments.get(credit_file_id, [])
        ]

    def save_covenant_test_result(
        self, result: CovenantTestResult
    ) -> None:
        # Insert-once, like the packs the result pins: the same
        # test over the same inputs is the same artefact.
        if result.result_id in self._test_results:
            return
        self._test_results[result.result_id] = result.model_copy(deep=True)
        self._test_result_order.setdefault(
            result.credit_file_id, []
        ).append(result.result_id)

    def get_covenant_test_result(
        self, result_id: str
    ) -> CovenantTestResult | None:
        result = self._test_results.get(result_id)
        return result.model_copy(deep=True) if result else None

    def list_covenant_test_results(
        self, credit_file_id: str
    ) -> list[CovenantTestResult]:
        return [
            self._test_results[result_id].model_copy(deep=True)
            for result_id in self._test_result_order.get(credit_file_id, [])
        ]

    def save_breach_case(self, breach: BreachCase) -> None:
        # A living record, upserted as its acts accumulate — the
        # credit file's own pattern. Its events only ever grow.
        if breach.breach_id not in self._breaches:
            self._breach_order.setdefault(
                breach.credit_file_id, []
            ).append(breach.breach_id)
        self._breaches[breach.breach_id] = breach.model_copy(deep=True)

    def get_breach_case(self, breach_id: str) -> BreachCase | None:
        breach = self._breaches.get(breach_id)
        return breach.model_copy(deep=True) if breach else None

    def list_breach_cases(
        self, credit_file_id: str | None = None
    ) -> list[BreachCase]:
        if credit_file_id is not None:
            return [
                self._breaches[breach_id].model_copy(deep=True)
                for breach_id in self._breach_order.get(credit_file_id, [])
            ]
        return [
            breach.model_copy(deep=True)
            for breach in self._breaches.values()
        ]

    # -- Round 12: annual review packs, outcomes, referrals.
    # All three are content-addressed artefacts, stored
    # insert-once like the packs and reports they sit beside:
    # the same assembly, the same stance, the same referral is
    # the same record, stored once.

    def save_review_pack(self, pack: AnnualReviewPack) -> None:
        if pack.pack_id in self._review_packs:
            return
        self._review_packs[pack.pack_id] = pack.model_copy(deep=True)
        self._review_pack_order.setdefault(
            pack.credit_file_id, []
        ).append(pack.pack_id)

    def get_review_pack(
        self, pack_id: str
    ) -> AnnualReviewPack | None:
        pack = self._review_packs.get(pack_id)
        return pack.model_copy(deep=True) if pack else None

    def list_review_packs(
        self, credit_file_id: str
    ) -> list[AnnualReviewPack]:
        return [
            self._review_packs[pack_id].model_copy(deep=True)
            for pack_id in self._review_pack_order.get(credit_file_id, [])
        ]

    def save_review_outcome(self, outcome: AnnualReviewOutcome) -> None:
        if outcome.outcome_id in self._review_outcomes:
            return
        self._review_outcomes[outcome.outcome_id] = outcome.model_copy(
            deep=True
        )
        self._review_outcome_order.setdefault(
            outcome.credit_file_id, []
        ).append(outcome.outcome_id)

    def get_review_outcome(
        self, outcome_id: str
    ) -> AnnualReviewOutcome | None:
        outcome = self._review_outcomes.get(outcome_id)
        return outcome.model_copy(deep=True) if outcome else None

    def list_review_outcomes(
        self, credit_file_id: str
    ) -> list[AnnualReviewOutcome]:
        return [
            self._review_outcomes[outcome_id].model_copy(deep=True)
            for outcome_id in self._review_outcome_order.get(
                credit_file_id, []
            )
        ]

    def save_committee_referral(
        self, referral: CommitteeReferral
    ) -> None:
        if referral.referral_id in self._referrals:
            return
        self._referrals[referral.referral_id] = referral.model_copy(
            deep=True
        )
        self._referral_order.setdefault(
            referral.credit_file_id, []
        ).append(referral.referral_id)

    def get_committee_referral(
        self, referral_id: str
    ) -> CommitteeReferral | None:
        referral = self._referrals.get(referral_id)
        return referral.model_copy(deep=True) if referral else None

    def list_committee_referrals(
        self, credit_file_id: str
    ) -> list[CommitteeReferral]:
        return [
            self._referrals[referral_id].model_copy(deep=True)
            for referral_id in self._referral_order.get(credit_file_id, [])
        ]

    def save_rating_assignment(
        self, assignment: RatingAssignment
    ) -> None:
        if assignment.assignment_id in self._rating_assignments:
            return
        self._rating_assignments[assignment.assignment_id] = (
            assignment.model_copy(deep=True)
        )
        self._rating_assignment_order.setdefault(
            assignment.credit_file_id, []
        ).append(assignment.assignment_id)

    def get_rating_assignment(
        self, assignment_id: str
    ) -> RatingAssignment | None:
        assignment = self._rating_assignments.get(assignment_id)
        return assignment.model_copy(deep=True) if assignment else None

    def list_rating_assignments(
        self, credit_file_id: str
    ) -> list[RatingAssignment]:
        return [
            self._rating_assignments[assignment_id].model_copy(deep=True)
            for assignment_id in self._rating_assignment_order.get(
                credit_file_id, []
            )
        ]

    def save_rating_override(self, override: RatingOverride) -> None:
        if override.override_id in self._rating_overrides:
            return
        self._rating_overrides[override.override_id] = override.model_copy(
            deep=True
        )
        self._rating_override_order.setdefault(
            override.credit_file_id, []
        ).append(override.override_id)

    def get_rating_override(
        self, override_id: str
    ) -> RatingOverride | None:
        override = self._rating_overrides.get(override_id)
        return override.model_copy(deep=True) if override else None

    def list_rating_overrides(
        self, credit_file_id: str
    ) -> list[RatingOverride]:
        return [
            self._rating_overrides[override_id].model_copy(deep=True)
            for override_id in self._rating_override_order.get(
                credit_file_id, []
            )
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
            CREATE TABLE IF NOT EXISTS readiness_reports (
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
            CREATE TABLE IF NOT EXISTS committee_decisions (
                decision_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, memo_id TEXT NOT NULL,
                outcome TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS covenant_sets (
                set_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, decision_id TEXT NOT NULL,
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS covenant_amendments (
                amendment_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, set_id TEXT NOT NULL,
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS covenant_test_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_id TEXT NOT NULL UNIQUE,
                credit_file_id TEXT NOT NULL, instance_id TEXT NOT NULL,
                covenant_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS breach_cases (
                breach_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS annual_review_packs (
                pack_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, set_id TEXT NOT NULL,
                review_number INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS annual_review_outcomes (
                outcome_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, set_id TEXT NOT NULL,
                review_number INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS committee_referrals (
                referral_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL, outcome_id TEXT NOT NULL,
                payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rating_assignments (
                assignment_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL,
                scorecard_version TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS rating_overrides (
                override_id TEXT PRIMARY KEY,
                credit_file_id TEXT NOT NULL,
                assignment_id TEXT NOT NULL, payload TEXT NOT NULL);
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

    def save_readiness_report(self, report: ReadinessReport) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO readiness_reports (report_id, credit_file_id, payload) "
                "VALUES (?, ?, ?) ON CONFLICT(report_id) DO NOTHING",
                (report.report_id, report.credit_file_id, report.model_dump_json()),
            )

    def get_readiness_report(
        self, report_id: str
    ) -> ReadinessReport | None:
        row = self._conn.execute(
            "SELECT payload FROM readiness_reports WHERE report_id = ?",
            (report_id,),
        ).fetchone()
        return ReadinessReport.model_validate_json(row[0]) if row else None

    def list_readiness_reports(
        self, credit_file_id: str
    ) -> list[ReadinessReport]:
        rows = self._conn.execute(
            "SELECT payload FROM readiness_reports WHERE credit_file_id = ? "
            "ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [ReadinessReport.model_validate_json(r[0]) for r in rows]

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

    def save_committee_decision(self, decision: CommitteeDecision) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO committee_decisions (decision_id, credit_file_id, memo_id, outcome, payload) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(decision_id) DO NOTHING",
                (
                    decision.decision_id,
                    decision.credit_file_id,
                    decision.memo_id,
                    decision.outcome,
                    decision.model_dump_json(),
                ),
            )

    def get_committee_decision(
        self, decision_id: str
    ) -> CommitteeDecision | None:
        row = self._conn.execute(
            "SELECT payload FROM committee_decisions WHERE decision_id = ?",
            (decision_id,),
        ).fetchone()
        return CommitteeDecision.model_validate_json(row[0]) if row else None

    def list_committee_decisions(
        self, credit_file_id: str, memo_id: str | None = None
    ) -> list[CommitteeDecision]:
        if memo_id is None:
            rows = self._conn.execute(
                "SELECT payload FROM committee_decisions "
                "WHERE credit_file_id = ? ORDER BY rowid",
                (credit_file_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT payload FROM committee_decisions "
                "WHERE credit_file_id = ? AND memo_id = ? ORDER BY rowid",
                (credit_file_id, memo_id),
            ).fetchall()
        return [CommitteeDecision.model_validate_json(r[0]) for r in rows]

    def save_covenant_set(self, covenant_set: CovenantSet) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO covenant_sets (set_id, credit_file_id, decision_id, payload) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(set_id) DO NOTHING",
                (
                    covenant_set.set_id,
                    covenant_set.credit_file_id,
                    covenant_set.decision_id,
                    covenant_set.model_dump_json(),
                ),
            )

    def get_covenant_set(self, set_id: str) -> CovenantSet | None:
        row = self._conn.execute(
            "SELECT payload FROM covenant_sets WHERE set_id = ?",
            (set_id,),
        ).fetchone()
        return CovenantSet.model_validate_json(row[0]) if row else None

    def list_covenant_sets(
        self, credit_file_id: str
    ) -> list[CovenantSet]:
        rows = self._conn.execute(
            "SELECT payload FROM covenant_sets WHERE credit_file_id = ? "
            "ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [CovenantSet.model_validate_json(r[0]) for r in rows]

    def save_covenant_amendment(
        self, amendment: CovenantAmendment
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO covenant_amendments (amendment_id, credit_file_id, set_id, payload) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(amendment_id) DO NOTHING",
                (
                    amendment.amendment_id,
                    amendment.credit_file_id,
                    amendment.set_id,
                    amendment.model_dump_json(),
                ),
            )

    def list_covenant_amendments(
        self, credit_file_id: str
    ) -> list[CovenantAmendment]:
        rows = self._conn.execute(
            "SELECT payload FROM covenant_amendments "
            "WHERE credit_file_id = ? ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [CovenantAmendment.model_validate_json(r[0]) for r in rows]

    def save_covenant_test_result(
        self, result: CovenantTestResult
    ) -> None:
        # Insert-once: re-running the same test over the same
        # inputs stores nothing twice.
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO covenant_test_results "
                "(result_id, credit_file_id, instance_id, covenant_id, "
                "payload) VALUES (?, ?, ?, ?, ?)",
                (
                    result.result_id, result.credit_file_id,
                    result.instance_id, result.covenant_id,
                    result.model_dump_json(),
                ),
            )

    def get_covenant_test_result(
        self, result_id: str
    ) -> CovenantTestResult | None:
        row = self._conn.execute(
            "SELECT payload FROM covenant_test_results "
            "WHERE result_id = ?",
            (result_id,),
        ).fetchone()
        return CovenantTestResult.model_validate_json(row[0]) if row else None

    def list_covenant_test_results(
        self, credit_file_id: str
    ) -> list[CovenantTestResult]:
        rows = self._conn.execute(
            "SELECT payload FROM covenant_test_results "
            "WHERE credit_file_id = ? ORDER BY id",
            (credit_file_id,),
        ).fetchall()
        return [CovenantTestResult.model_validate_json(r[0]) for r in rows]

    def save_breach_case(self, breach: BreachCase) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO breach_cases (breach_id, credit_file_id, "
                "payload) VALUES (?, ?, ?) "
                "ON CONFLICT(breach_id) DO UPDATE SET "
                "payload = excluded.payload",
                (
                    breach.breach_id, breach.credit_file_id,
                    breach.model_dump_json(),
                ),
            )

    # -- Round 12: annual review packs, outcomes, referrals —
    # content-addressed artefacts, insert-once like the reports.

    def save_review_pack(self, pack: AnnualReviewPack) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO annual_review_packs (pack_id, "
                "credit_file_id, set_id, review_number, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(pack_id) DO NOTHING",
                (
                    pack.pack_id, pack.credit_file_id, pack.set_id,
                    pack.review_number, pack.model_dump_json(),
                ),
            )

    def get_review_pack(
        self, pack_id: str
    ) -> AnnualReviewPack | None:
        row = self._conn.execute(
            "SELECT payload FROM annual_review_packs "
            "WHERE pack_id = ?",
            (pack_id,),
        ).fetchone()
        return AnnualReviewPack.model_validate_json(row[0]) if row else None

    def list_review_packs(
        self, credit_file_id: str
    ) -> list[AnnualReviewPack]:
        rows = self._conn.execute(
            "SELECT payload FROM annual_review_packs "
            "WHERE credit_file_id = ? ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [AnnualReviewPack.model_validate_json(r[0]) for r in rows]

    def save_review_outcome(self, outcome: AnnualReviewOutcome) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO annual_review_outcomes (outcome_id, "
                "credit_file_id, set_id, review_number, payload) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(outcome_id) DO NOTHING",
                (
                    outcome.outcome_id, outcome.credit_file_id,
                    outcome.set_id, outcome.review_number,
                    outcome.model_dump_json(),
                ),
            )

    def get_review_outcome(
        self, outcome_id: str
    ) -> AnnualReviewOutcome | None:
        row = self._conn.execute(
            "SELECT payload FROM annual_review_outcomes "
            "WHERE outcome_id = ?",
            (outcome_id,),
        ).fetchone()
        return (
            AnnualReviewOutcome.model_validate_json(row[0]) if row else None
        )

    def list_review_outcomes(
        self, credit_file_id: str
    ) -> list[AnnualReviewOutcome]:
        rows = self._conn.execute(
            "SELECT payload FROM annual_review_outcomes "
            "WHERE credit_file_id = ? ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [
            AnnualReviewOutcome.model_validate_json(r[0]) for r in rows
        ]

    def save_committee_referral(
        self, referral: CommitteeReferral
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO committee_referrals (referral_id, "
                "credit_file_id, outcome_id, payload) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(referral_id) DO NOTHING",
                (
                    referral.referral_id, referral.credit_file_id,
                    referral.outcome_id, referral.model_dump_json(),
                ),
            )

    def get_committee_referral(
        self, referral_id: str
    ) -> CommitteeReferral | None:
        row = self._conn.execute(
            "SELECT payload FROM committee_referrals "
            "WHERE referral_id = ?",
            (referral_id,),
        ).fetchone()
        return (
            CommitteeReferral.model_validate_json(row[0]) if row else None
        )

    def list_committee_referrals(
        self, credit_file_id: str
    ) -> list[CommitteeReferral]:
        rows = self._conn.execute(
            "SELECT payload FROM committee_referrals "
            "WHERE credit_file_id = ? ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [
            CommitteeReferral.model_validate_json(r[0]) for r in rows
        ]

    def save_rating_assignment(
        self, assignment: RatingAssignment
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO rating_assignments (assignment_id, "
                "credit_file_id, scorecard_version, payload) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(assignment_id) DO NOTHING",
                (
                    assignment.assignment_id, assignment.credit_file_id,
                    assignment.scorecard_version,
                    assignment.model_dump_json(),
                ),
            )

    def get_rating_assignment(
        self, assignment_id: str
    ) -> RatingAssignment | None:
        row = self._conn.execute(
            "SELECT payload FROM rating_assignments "
            "WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()
        return (
            RatingAssignment.model_validate_json(row[0]) if row else None
        )

    def list_rating_assignments(
        self, credit_file_id: str
    ) -> list[RatingAssignment]:
        rows = self._conn.execute(
            "SELECT payload FROM rating_assignments "
            "WHERE credit_file_id = ? ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [
            RatingAssignment.model_validate_json(r[0]) for r in rows
        ]

    def save_rating_override(self, override: RatingOverride) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO rating_overrides (override_id, "
                "credit_file_id, assignment_id, payload) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(override_id) DO NOTHING",
                (
                    override.override_id, override.credit_file_id,
                    override.assignment_id, override.model_dump_json(),
                ),
            )

    def get_rating_override(
        self, override_id: str
    ) -> RatingOverride | None:
        row = self._conn.execute(
            "SELECT payload FROM rating_overrides "
            "WHERE override_id = ?",
            (override_id,),
        ).fetchone()
        return (
            RatingOverride.model_validate_json(row[0]) if row else None
        )

    def list_rating_overrides(
        self, credit_file_id: str
    ) -> list[RatingOverride]:
        rows = self._conn.execute(
            "SELECT payload FROM rating_overrides "
            "WHERE credit_file_id = ? ORDER BY rowid",
            (credit_file_id,),
        ).fetchall()
        return [
            RatingOverride.model_validate_json(r[0]) for r in rows
        ]

    def get_breach_case(self, breach_id: str) -> BreachCase | None:
        row = self._conn.execute(
            "SELECT payload FROM breach_cases WHERE breach_id = ?",
            (breach_id,),
        ).fetchone()
        return BreachCase.model_validate_json(row[0]) if row else None

    def list_breach_cases(
        self, credit_file_id: str | None = None
    ) -> list[BreachCase]:
        if credit_file_id is not None:
            rows = self._conn.execute(
                "SELECT payload FROM breach_cases "
                "WHERE credit_file_id = ? ORDER BY rowid",
                (credit_file_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT payload FROM breach_cases ORDER BY rowid"
            ).fetchall()
        return [BreachCase.model_validate_json(r[0]) for r in rows]

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

    def save_readiness_report(self, report: ReadinessReport) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO readiness_reports (report_id, credit_file_id, as_of, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (report_id) DO NOTHING",
                (
                    report.report_id,
                    report.credit_file_id,
                    report.as_of,
                    report.model_dump_json(),
                ),
            )

    def get_readiness_report(
        self, report_id: str
    ) -> ReadinessReport | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM readiness_reports WHERE report_id = %s",
                (report_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return ReadinessReport.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_readiness_reports(
        self, credit_file_id: str
    ) -> list[ReadinessReport]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM readiness_reports WHERE credit_file_id = %s "
                "ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                ReadinessReport.model_validate(
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

    def save_committee_decision(self, decision: CommitteeDecision) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO committee_decisions (decision_id, credit_file_id, memo_id, outcome, payload) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (decision_id) DO NOTHING",
                (
                    decision.decision_id,
                    decision.credit_file_id,
                    decision.memo_id,
                    decision.outcome,
                    decision.model_dump_json(),
                ),
            )

    def get_committee_decision(
        self, decision_id: str
    ) -> CommitteeDecision | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM committee_decisions WHERE decision_id = %s",
                (decision_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return CommitteeDecision.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_committee_decisions(
        self, credit_file_id: str, memo_id: str | None = None
    ) -> list[CommitteeDecision]:
        with self._connect() as conn:
            if memo_id is None:
                rows = conn.execute(
                    "SELECT payload FROM committee_decisions "
                    "WHERE credit_file_id = %s ORDER BY id",
                    (credit_file_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM committee_decisions "
                    "WHERE credit_file_id = %s AND memo_id = %s "
                    "ORDER BY id",
                    (credit_file_id, memo_id),
                ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                CommitteeDecision.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_covenant_set(self, covenant_set: CovenantSet) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO covenant_sets (set_id, credit_file_id, decision_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (set_id) DO NOTHING",
                (
                    covenant_set.set_id,
                    covenant_set.credit_file_id,
                    covenant_set.decision_id,
                    covenant_set.model_dump_json(),
                ),
            )

    def get_covenant_set(self, set_id: str) -> CovenantSet | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM covenant_sets WHERE set_id = %s",
                (set_id,),
            ).fetchone()
        if not row:
            return None
        payload = row["payload"]
        return CovenantSet.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_covenant_sets(
        self, credit_file_id: str
    ) -> list[CovenantSet]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM covenant_sets WHERE credit_file_id = %s "
                "ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                CovenantSet.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_covenant_amendment(
        self, amendment: CovenantAmendment
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO covenant_amendments (amendment_id, credit_file_id, set_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) ON CONFLICT (amendment_id) DO NOTHING",
                (
                    amendment.amendment_id,
                    amendment.credit_file_id,
                    amendment.set_id,
                    amendment.model_dump_json(),
                ),
            )

    def list_covenant_amendments(
        self, credit_file_id: str
    ) -> list[CovenantAmendment]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM covenant_amendments "
                "WHERE credit_file_id = %s ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                CovenantAmendment.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_covenant_test_result(
        self, result: CovenantTestResult
    ) -> None:
        # Insert-once, like the packs the result pins.
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO covenant_test_results "
                "(result_id, credit_file_id, instance_id, covenant_id, "
                "payload) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (result_id) DO NOTHING",
                (
                    result.result_id, result.credit_file_id,
                    result.instance_id, result.covenant_id,
                    result.model_dump_json(),
                ),
            )

    def get_covenant_test_result(
        self, result_id: str
    ) -> CovenantTestResult | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM covenant_test_results "
                "WHERE result_id = %s",
                (result_id,),
            ).fetchone()
        if row is None:
            return None
        payload = row["payload"]
        return CovenantTestResult.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_covenant_test_results(
        self, credit_file_id: str
    ) -> list[CovenantTestResult]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM covenant_test_results "
                "WHERE credit_file_id = %s ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                CovenantTestResult.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    def save_breach_case(self, breach: BreachCase) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO breach_cases "
                "(breach_id, credit_file_id, payload) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (breach_id) DO UPDATE SET "
                "payload = EXCLUDED.payload",
                (
                    breach.breach_id, breach.credit_file_id,
                    breach.model_dump_json(),
                ),
            )

    def get_breach_case(self, breach_id: str) -> BreachCase | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM breach_cases WHERE breach_id = %s",
                (breach_id,),
            ).fetchone()
        if row is None:
            return None
        payload = row["payload"]
        return BreachCase.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def list_breach_cases(
        self, credit_file_id: str | None = None
    ) -> list[BreachCase]:
        with self._connect() as conn:
            if credit_file_id is not None:
                rows = conn.execute(
                    "SELECT payload FROM breach_cases "
                    "WHERE credit_file_id = %s ORDER BY id",
                    (credit_file_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT payload FROM breach_cases ORDER BY id"
                ).fetchall()
        out = []
        for row in rows:
            payload = row["payload"]
            out.append(
                BreachCase.model_validate(
                    payload if isinstance(payload, dict) else json.loads(payload)
                )
            )
        return out

    # -- Round 12: annual review packs, outcomes, referrals —
    # content-addressed artefacts, insert-once (migration 0011).

    @staticmethod
    def _validate(model, row):  # noqa: ANN001, ANN202
        if row is None:
            return None
        payload = row["payload"]
        return model.model_validate(
            payload if isinstance(payload, dict) else json.loads(payload)
        )

    def save_review_pack(self, pack: AnnualReviewPack) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO annual_review_packs (pack_id, "
                "credit_file_id, set_id, review_number, payload) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (pack_id) DO NOTHING",
                (
                    pack.pack_id, pack.credit_file_id, pack.set_id,
                    pack.review_number, pack.model_dump_json(),
                ),
            )

    def get_review_pack(
        self, pack_id: str
    ) -> AnnualReviewPack | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM annual_review_packs "
                "WHERE pack_id = %s",
                (pack_id,),
            ).fetchone()
        return self._validate(AnnualReviewPack, row)

    def list_review_packs(
        self, credit_file_id: str
    ) -> list[AnnualReviewPack]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM annual_review_packs "
                "WHERE credit_file_id = %s ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        return [self._validate(AnnualReviewPack, row) for row in rows]

    def save_review_outcome(self, outcome: AnnualReviewOutcome) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO annual_review_outcomes (outcome_id, "
                "credit_file_id, set_id, review_number, payload) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (outcome_id) DO NOTHING",
                (
                    outcome.outcome_id, outcome.credit_file_id,
                    outcome.set_id, outcome.review_number,
                    outcome.model_dump_json(),
                ),
            )

    def get_review_outcome(
        self, outcome_id: str
    ) -> AnnualReviewOutcome | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM annual_review_outcomes "
                "WHERE outcome_id = %s",
                (outcome_id,),
            ).fetchone()
        return self._validate(AnnualReviewOutcome, row)

    def list_review_outcomes(
        self, credit_file_id: str
    ) -> list[AnnualReviewOutcome]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM annual_review_outcomes "
                "WHERE credit_file_id = %s ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        return [self._validate(AnnualReviewOutcome, row) for row in rows]

    def save_committee_referral(
        self, referral: CommitteeReferral
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO committee_referrals (referral_id, "
                "credit_file_id, outcome_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) "
                "ON CONFLICT (referral_id) DO NOTHING",
                (
                    referral.referral_id, referral.credit_file_id,
                    referral.outcome_id, referral.model_dump_json(),
                ),
            )

    def get_committee_referral(
        self, referral_id: str
    ) -> CommitteeReferral | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM committee_referrals "
                "WHERE referral_id = %s",
                (referral_id,),
            ).fetchone()
        return self._validate(CommitteeReferral, row)

    def list_committee_referrals(
        self, credit_file_id: str
    ) -> list[CommitteeReferral]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM committee_referrals "
                "WHERE credit_file_id = %s ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        return [self._validate(CommitteeReferral, row) for row in rows]

    def save_rating_assignment(
        self, assignment: RatingAssignment
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rating_assignments (assignment_id, "
                "credit_file_id, scorecard_version, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) "
                "ON CONFLICT (assignment_id) DO NOTHING",
                (
                    assignment.assignment_id, assignment.credit_file_id,
                    assignment.scorecard_version,
                    assignment.model_dump_json(),
                ),
            )

    def get_rating_assignment(
        self, assignment_id: str
    ) -> RatingAssignment | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM rating_assignments "
                "WHERE assignment_id = %s",
                (assignment_id,),
            ).fetchone()
        return self._validate(RatingAssignment, row)

    def list_rating_assignments(
        self, credit_file_id: str
    ) -> list[RatingAssignment]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM rating_assignments "
                "WHERE credit_file_id = %s ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        return [self._validate(RatingAssignment, row) for row in rows]

    def save_rating_override(self, override: RatingOverride) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO rating_overrides (override_id, "
                "credit_file_id, assignment_id, payload) "
                "VALUES (%s, %s, %s, %s::jsonb) "
                "ON CONFLICT (override_id) DO NOTHING",
                (
                    override.override_id, override.credit_file_id,
                    override.assignment_id, override.model_dump_json(),
                ),
            )

    def get_rating_override(
        self, override_id: str
    ) -> RatingOverride | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM rating_overrides "
                "WHERE override_id = %s",
                (override_id,),
            ).fetchone()
        return self._validate(RatingOverride, row)

    def list_rating_overrides(
        self, credit_file_id: str
    ) -> list[RatingOverride]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM rating_overrides "
                "WHERE credit_file_id = %s ORDER BY id",
                (credit_file_id,),
            ).fetchall()
        return [self._validate(RatingOverride, row) for row in rows]

    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False
