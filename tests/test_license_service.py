from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from license_office.clock import FrozenClock
from license_office.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from license_office.service import LicenseOfficeService


def agreement(agreement_id="agr-1", version="v1", effective="2025-01-01", expiry="2030-12-31",
              shares=(("uni", "university_team", "0.6"), ("co", "enterprise", "0.4")), supersedes=None,
              achievement_id="ach-1"):
    return {
        "agreement_id": agreement_id,
        "achievement_id": achievement_id,
        "title": "协议",
        "version": version,
        "effective_date": effective,
        "expiry_date": expiry,
        "supersedes_agreement_id": supersedes,
        "ownership": [
            {"owner_id": o, "owner_kind": kind, "share": s, "basis": "依据"}
            for o, kind, s in shares
        ],
    }


def grant(grant_id="g1", agreement_id="agr-1", licensee_id="l1", kind="non_exclusive",
          regions=("CN-NORTH",), start="2025-02-01", end="2027-12-31", rate="0.05", royalty_type="sales_rate", fixed=None):
    return {
        "grant_id": grant_id,
        "agreement_id": agreement_id,
        "licensee_id": licensee_id,
        "grant_kind": kind,
        "territories": [{"region_code": r, "region_name": r} for r in regions],
        "fields_of_use": "商业实施",
        "start_date": start,
        "end_date": end,
        "royalty": {"royalty_type": royalty_type, "rate": rate, "fixed_amount": fixed, "note": ""},
    }


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        self.service = LicenseOfficeService(self.connection, self.clock)
        for user_id, role in (("mgr", "manager"), ("legal", "legal"), ("fin", "finance"), ("aud", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.create_achievement("mgr", {"achievement_id": "ach-1", "name": "新工艺", "domain": "材料"})
        for licensee_id in ("l1", "l2", "l3"):
            self.service.register_licensee(
                "mgr",
                {"licensee_id": licensee_id, "name": licensee_id, "credit_code": f"C-{licensee_id}", "contact": "x"},
            )
        self.service.register_agreement("legal", agreement())

    def tearDown(self) -> None:
        self.connection.close()


class AgreementTests(ServiceTestCase):
    def test_ownership_shares_must_total_one(self) -> None:
        raw = agreement("agr-bad", "vX", shares=(("uni", "university_team", "0.6"), ("co", "enterprise", "0.3")))
        with self.assertRaises(ValidationFailed):
            self.service.register_agreement("legal", raw)

    def test_superseded_version_cannot_issue_grants(self) -> None:
        self.service.register_agreement(
            "legal", agreement("agr-2", "v2", effective="2026-01-01", expiry="2032-12-31", supersedes="agr-1")
        )
        self.assertEqual(self.service.agreement("agr-1")["state"], "superseded")
        with self.assertRaises(InvalidState):
            self.service.issue_grant("mgr", grant(grant_id="g-old", agreement_id="agr-1", start="2026-06-01", end="2026-12-31"))

    def test_grant_window_must_fit_agreement(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.issue_grant("mgr", grant(grant_id="g-wide", start="2024-12-01", end="2026-01-01"))


class GrantConflictTests(ServiceTestCase):
    def test_overlapping_exclusive_is_blocked(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1", regions=("CN-NORTH",)))
        with self.assertRaises(Conflict):
            self.service.issue_grant("mgr", grant("g2", licensee_id="l2", kind="exclusive", regions=("CN-NORTH",)))

    def test_non_overlapping_regions_coexist(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1", regions=("CN-NORTH",), kind="exclusive"))
        issued = self.service.issue_grant("mgr", grant("g2", licensee_id="l2", regions=("CN-SOUTH",), kind="exclusive"))
        self.assertEqual(issued["grant_id"], "g2")

    def test_same_licensee_double_issue_blocked(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1"))
        with self.assertRaises(Conflict):
            self.service.issue_grant("mgr", grant("g2", licensee_id="l1"))

    def test_terminated_grant_frees_scope(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1", kind="exclusive"))
        self.service.terminate_grant("mgr", "g1")
        issued = self.service.issue_grant("mgr", grant("g2", licensee_id="l2", kind="exclusive"))
        self.assertEqual(issued["state"], "active")

    def test_finance_cannot_issue_grants(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.issue_grant("fin", grant("g2", licensee_id="l2"))


class RenewalTests(ServiceTestCase):
    def test_extension_and_new_issue_blocked_during_negotiation(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1", end="2026-12-31"))
        self.service.open_renewal("legal", "g1", "谈续约")
        with self.assertRaises(InvalidState):
            self.service.extend_grant("mgr", "g1", "2027-06-30")
        with self.assertRaises(InvalidState):
            self.service.issue_grant("mgr", grant("g1b", licensee_id="l1", start="2027-01-01", end="2027-12-31"))
        # 旧许可原封不动
        self.assertEqual(self.service.grant("g1")["end_date"], "2026-12-31")

    def test_extension_conflict_checked_against_others(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1", regions=("CN-NORTH",), start="2025-01-01", end="2026-06-30"))
        self.service.issue_grant("mgr", grant("g2", licensee_id="l2", kind="exclusive", regions=("CN-NORTH",), start="2026-07-01", end="2027-12-31"))
        with self.assertRaises(Conflict):
            self.service.extend_grant("mgr", "g1", "2026-12-31")

    def test_sign_renewal_replaces_old_grant_in_one_transaction(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1", end="2026-12-31"))
        self.service.register_agreement(
            "legal", agreement("agr-2", "v2", effective="2026-06-01", expiry="2032-12-31", supersedes="agr-1")
        )
        renewal = self.service.open_renewal("legal", "g1", "续约")
        result = self.service.sign_renewal(
            "legal",
            renewal["renewal_id"],
            grant("g2", agreement_id="agr-2", licensee_id="l1", start="2027-01-01", end="2028-12-31", rate="0.04"),
        )
        self.assertEqual(result["state"], "signed")
        self.assertEqual(self.service.grant("g1")["state"], "terminated")
        self.assertEqual(result["new_grant"]["agreement_id"], "agr-2")

    def test_abandoned_renewal_releases_lock(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1", end="2026-12-31"))
        renewal = self.service.open_renewal("legal", "g1", "续约")
        self.service.abandon_renewal("legal", renewal["renewal_id"], "未达成")
        extended = self.service.extend_grant("mgr", "g1", "2027-06-30")
        self.assertEqual(extended["end_date"], "2027-06-30")


class ImprovementTests(ServiceTestCase):
    def test_improvement_binds_agreement_version(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1"))
        recorded = self.service.record_improvement("mgr", {
            "improvement_id": "imp-1",
            "parent_achievement_id": "ach-1",
            "agreement_id": "agr-1",
            "title": "低温前处理",
            "ownership_rule": "joint",
            "contributor_licensee_id": "l1",
            "derived_grant_id": "g1",
        })
        self.assertEqual(recorded["ownership_rule"], "joint")

    def test_improvement_agreement_must_match_achievement(self) -> None:
        self.service.create_achievement("mgr", {"achievement_id": "ach-2", "name": "其他", "domain": ""})
        self.service.register_agreement("legal", agreement("agr-other", achievement_id="ach-2"))
        with self.assertRaises(ValidationFailed):
            self.service.record_improvement("mgr", {
                "improvement_id": "imp-x",
                "parent_achievement_id": "ach-1",
                "agreement_id": "agr-other",
                "title": "x",
                "ownership_rule": "joint",
            })


class RevenueTests(ServiceTestCase):
    def _period(self, period_id="p1", amount="100000", grant_id="g1", date="2026-03-31", key="k1"):
        self.service.issue_grant("mgr", grant(grant_id, licensee_id="l1"))
        return self.service.estimate_revenue("fin", {
            "period_id": period_id,
            "grant_id": grant_id,
            "period_date": date,
            "estimated_amount": amount,
            "idempotency_key": key,
        })

    def test_estimate_then_assess_uses_assessed_amount(self) -> None:
        self._period(amount="100000")
        assessed = self.service.assess_revenue("fin", "p1", "102000")
        self.assertEqual(assessed["status"], "assessed")
        lines = {row["owner_id"]: row for row in assessed["allocations"]}
        self.assertEqual(Decimal(lines["uni"]["gross_amount"]), Decimal("61200.00"))
        self.assertEqual(Decimal(lines["co"]["gross_amount"]), Decimal("40800.00"))

    def test_settled_period_cannot_be_reassessed(self) -> None:
        self._period()
        self.service.assess_revenue("fin", "p1", "100000")
        self.service.settle_revenue("fin", "p1")
        with self.assertRaises(InvalidState):
            self.service.assess_revenue("fin", "p1", "1")

    def test_idempotent_estimate_replay(self) -> None:
        payload = {
            "period_id": "p1", "grant_id": "g1", "period_date": "2026-03-31",
            "estimated_amount": "100000", "idempotency_key": "k1",
        }
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1"))
        first = self.service.estimate_revenue("fin", payload)
        second = self.service.estimate_revenue("fin", payload)
        self.assertEqual(first, second)
        changed = dict(payload, estimated_amount="200000")
        with self.assertRaises(Conflict):
            self.service.estimate_revenue("fin", changed)

    def test_dispute_freezes_only_named_share_and_settlement_is_partial(self) -> None:
        self._period(amount="80000")
        self.service.assess_revenue("fin", "p1", "80000")
        self.service.open_dispute("fin", reason="比例争议", subject_owner_ids=["co"], period_id="p1")
        partial = self.service.settle_revenue("fin", "p1")
        self.assertEqual(partial["status"], "assessed")
        self.assertEqual(Decimal(partial["totals"]["settled_amount"]), Decimal("48000.00"))
        self.assertEqual(Decimal(partial["totals"]["frozen_amount"]), Decimal("32000.00"))
        disputes = self.connection.execute("SELECT dispute_id FROM disputes").fetchall()
        self.service.resolve_dispute("fin", disputes[0]["dispute_id"], "维持原比例")
        final = self.service.settle_revenue("fin", "p1")
        self.assertEqual(final["status"], "settled")
        self.assertEqual(Decimal(final["totals"]["settled_amount"]), Decimal("80000.00"))
        self.assertEqual(Decimal(final["totals"]["frozen_amount"]), Decimal("0.00"))

    def test_new_dispute_never_claws_back_settled_amount(self) -> None:
        self._period(amount="100000")
        self.service.assess_revenue("fin", "p1", "100000")
        self.service.settle_revenue("fin", "p1")
        self.service.open_dispute("fin", reason="事后争议", subject_owner_ids=["co"], period_id="p1")
        period = self.service.period("p1")
        self.assertEqual(period["status"], "settled")
        self.assertEqual(Decimal(period["totals"]["frozen_amount"]), Decimal("0.00"))
        self.assertEqual(Decimal(period["totals"]["settled_amount"]), Decimal("100000.00"))

    def test_new_agreement_does_not_rewrite_history(self) -> None:
        self._period(amount="100000")
        self.service.assess_revenue("fin", "p1", "100000")
        self.service.settle_revenue("fin", "p1")
        self.service.register_agreement(
            "legal",
            agreement("agr-2", "v2", effective="2026-07-01", expiry="2032-12-31",
                      shares=(("uni", "university_team", "0.5"), ("co", "enterprise", "0.3"), ("pf", "platform", "0.2")),
                      supersedes="agr-1"),
        )
        history = self.service.period("p1")
        owners = {row["owner_id"]: row["gross_amount"] for row in history["allocations"]}
        self.assertEqual(owners, {"uni": "60000.00", "co": "40000.00"})

    def test_grant_level_dispute_covers_periods_recorded_afterwards(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1", start="2025-02-01", end="2027-12-31"))
        self.service.open_dispute("fin", reason="授权级争议", subject_owner_ids=["co"], grant_id="g1")
        self.service.estimate_revenue("fin", {
            "period_id": "p-late", "grant_id": "g1", "period_date": "2026-09-30",
            "estimated_amount": "50000", "idempotency_key": "k-late",
        })
        period = self.service.period("p-late")
        co = next(row for row in period["allocations"] if row["owner_id"] == "co")
        self.assertEqual(Decimal(co["frozen_amount"]), Decimal("20000.00"))

    def test_estimate_rejects_period_outside_grant_window(self) -> None:
        self._period()
        with self.assertRaises(ValidationFailed):
            self.service.estimate_revenue("fin", {
                "period_id": "p2", "grant_id": "g1", "period_date": "2024-12-31",
                "estimated_amount": "10", "idempotency_key": "k2",
            })


class ChainAndExplainTests(ServiceTestCase):
    def test_chain_and_explain(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1"))
        self._make_period()
        chain = self.service.license_chain("mgr", achievement_id="ach-1")
        self.assertEqual([item["grant_id"] for item in chain["grants"]], ["g1"])
        chain_licensee = self.service.license_chain("mgr", licensee_id="l1")
        self.assertEqual(chain_licensee["scope"], "licensee")
        explanation = self.service.explain_revenue("aud", "p1")
        self.assertEqual(explanation["agreement"]["agreement_id"], "agr-1")
        self.assertTrue(any("权属依据" in reason for reason in explanation["reasons"]))

    def _make_period(self) -> None:
        self.service.estimate_revenue("fin", {
            "period_id": "p1", "grant_id": "g1", "period_date": "2026-03-31",
            "estimated_amount": "100000", "idempotency_key": "k1",
        })

    def test_chain_requires_scope(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.license_chain("mgr")

    def test_audit_chain_is_valid(self) -> None:
        self.service.issue_grant("mgr", grant("g1", licensee_id="l1"))
        result = self.service.audit_chain("aud")
        self.assertTrue(result["valid"])
        self.assertGreater(result["events"], 0)


if __name__ == "__main__":
    unittest.main()
