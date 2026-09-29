from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from license_revenue.clock import FrozenClock
from license_revenue.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from license_revenue.service import LicensingService


OWNERSHIP_60_40 = [
    {"owner_id": "univ", "share_percent": "60"},
    {"owner_id": "equipco", "share_percent": "40"},
]


class ServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.service = LicensingService(self.connection, self.clock)
        for user_id, role in (("tm", "manager"), ("legal", "legal"), ("fin", "finance"), ("audit", "auditor")):
            self.service.create_user(user_id, user_id, role)
        self.service.register_achievement("tm", {
            "achievement_id": "mat-1", "title": "新材料工艺",
            "field_kind": "patent", "completed_on": "2025-11-01",
        })

    def tearDown(self) -> None:
        self.connection.close()

    def draft_and_ratify(self, version_label: str = "v1", ownership=None, effective_on: str = "2026-01-01") -> None:
        self.service.draft_agreement("tm", {
            "agreement_id": "agr-1", "achievement_id": "mat-1",
            "version_label": version_label, "effective_on": effective_on,
            "expires_on": "2030-12-31",
            "ownership": ownership or OWNERSHIP_60_40,
        })
        version_no = 1 if version_label == "v1" else 2
        self.service.ratify_agreement("legal", "agr-1", version_no)

    def issue(self, license_id: str, licensee: str = "co-a", license_type: str = "exclusive",
              regions=None, version_no: int = 1, key: str | None = None,
              valid_from: str = "2026-02-01", valid_until: str | None = "2027-12-31") -> dict:
        return self.service.issue_license("legal", {
            "license_id": license_id, "agreement_id": "agr-1", "version_no": version_no,
            "licensee_id": licensee, "license_type": license_type,
            "regions": regions or ["CN:BJ"],
            "valid_from": valid_from, "valid_until": valid_until,
            "improvement_policy": "joint", "idempotency_key": key or license_id,
        })


class AgreementTests(ServiceTestBase):
    def test_ownership_must_total_one_hundred(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.draft_agreement("tm", {
                "agreement_id": "agr-1", "achievement_id": "mat-1", "version_label": "v1",
                "effective_on": "2026-01-01", "ownership": [
                    {"owner_id": "univ", "share_percent": "60"},
                    {"owner_id": "equipco", "share_percent": "30"},
                ],
            })

    def test_duplicate_owner_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.draft_agreement("tm", {
                "agreement_id": "agr-1", "achievement_id": "mat-1", "version_label": "v1",
                "effective_on": "2026-01-01", "ownership": [
                    {"owner_id": "univ", "share_percent": "60"},
                    {"owner_id": "univ", "share_percent": "40"},
                ],
            })

    def test_only_legal_ratifies_and_old_version_superseded(self) -> None:
        self.draft_and_ratify()
        with self.assertRaises(Forbidden):
            self.service.ratify_agreement("tm", "agr-1", 1)
        self.service.draft_agreement("tm", {
            "agreement_id": "agr-1", "achievement_id": "mat-1", "version_label": "v2",
            "effective_on": "2028-01-01", "ownership": [
                {"owner_id": "univ", "share_percent": "70"},
                {"owner_id": "equipco", "share_percent": "30"},
            ],
        })
        self.service.ratify_agreement("legal", "agr-1", 2)
        v1 = self.service.agreement_snapshot("agr-1", 1)
        self.assertEqual(v1["state"], "superseded")
        # 旧版本的权属比例原样保留，不被新协议倒改。
        self.assertEqual(v1["ownership"][0]["share_percent"], "60.0000")

    def test_license_requires_effective_version(self) -> None:
        self.service.draft_agreement("tm", {
            "agreement_id": "agr-1", "achievement_id": "mat-1", "version_label": "v1",
            "effective_on": "2026-01-01", "ownership": OWNERSHIP_60_40,
        })
        with self.assertRaises(InvalidState):
            self.issue("lic-1")


class LicenseConflictTests(ServiceTestBase):
    def test_overlapping_grant_blocks_issue(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a", license_type="exclusive", regions=["CN:BJ"])
        with self.assertRaises(Conflict) as caught:
            self.issue("lic-b", licensee="co-b", license_type="non_exclusive", regions=["CN:BJ:HD"], key="lic-b")
        self.assertEqual(caught.exception.details[0]["existing_license_id"], "lic-a")

    def test_disjoint_regions_coexist(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a", regions=["CN:BJ"])
        self.issue("lic-b", licensee="co-b", regions=["JP"], key="lic-b")

    def test_non_exclusives_coexist_but_same_licensee_overlap_blocked(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a", licensee="co-a", license_type="non_exclusive")
        self.issue("lic-b", licensee="co-b", license_type="non_exclusive", key="lic-b")
        with self.assertRaises(Conflict):
            self.issue("lic-c", licensee="co-a", license_type="non_exclusive", key="lic-c")

    def test_license_window_bounded_by_agreement(self) -> None:
        self.draft_and_ratify()
        with self.assertRaises(InvalidState):
            self.issue("lic-a", valid_until="2031-01-01")

    def test_idempotent_replay_returns_same_result(self) -> None:
        self.draft_and_ratify()
        first = self.issue("lic-a", key="same-key")
        second = self.service.issue_license("legal", {
            "license_id": "lic-a", "agreement_id": "agr-1", "version_no": 1,
            "licensee_id": "co-a", "license_type": "exclusive", "regions": ["CN:BJ"],
            "valid_from": "2026-02-01", "valid_until": "2027-12-31",
            "improvement_policy": "joint", "idempotency_key": "same-key",
        })
        self.assertEqual(first, second)


class RenewalTests(ServiceTestBase):
    def test_extension_blocked_during_negotiation(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a")
        self.service.open_renewal("legal", "lic-a", "续约中")
        with self.assertRaises(InvalidState):
            self.service.extend_license("legal", "lic-a", "2028-06-30")

    def test_extension_allowed_after_negotiation_abandoned(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a")
        negotiation = self.service.open_renewal("legal", "lic-a", "续约中")
        self.service.abandon_renewal("legal", negotiation["negotiation_id"])
        result = self.service.extend_license("legal", "lic-a", "2028-06-30")
        self.assertEqual(result["valid_until"], "2028-06-30")

    def test_completed_renewal_supersedes_old_and_binds_new_version(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a")
        self.service.open_renewal("legal", "lic-a", "续约")
        self.service.draft_agreement("tm", {
            "agreement_id": "agr-1", "achievement_id": "mat-1", "version_label": "v2",
            "effective_on": "2028-01-01", "ownership": [
                {"owner_id": "univ", "share_percent": "70"},
                {"owner_id": "equipco", "share_percent": "30"},
            ],
        })
        self.service.ratify_agreement("legal", "agr-1", 2)
        result = self.service.complete_renewal("legal", 1, {
            "license_id": "lic-a2", "agreement_id": "agr-1", "version_no": 2,
            "licensee_id": "co-a", "license_type": "exclusive", "regions": ["CN:BJ"],
            "valid_from": "2028-01-01", "valid_until": "2030-06-30",
            "improvement_policy": "joint", "idempotency_key": "lic-a2",
        })
        self.assertEqual(result["renewal_of"], "lic-a")
        old = self.service.license_snapshot("lic-a")
        self.assertEqual(old["state"], "superseded")
        self.assertEqual(old["superseded_by_license_id"], "lic-a2")

    def test_renewal_rejects_different_licensee(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a")
        self.service.open_renewal("legal", "lic-a")
        with self.assertRaises(Conflict):
            self.service.complete_renewal("legal", 1, {
                "license_id": "lic-a2", "agreement_id": "agr-1", "version_no": 1,
                "licensee_id": "someone-else", "license_type": "exclusive", "regions": ["CN:BJ"],
                "valid_from": "2028-01-01", "valid_until": "2029-01-01",
                "improvement_policy": "joint", "idempotency_key": "lic-a2",
            })


class ImprovementTests(ServiceTestBase):
    def test_improvement_binds_to_parent_license_version(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a")
        result = self.service.file_improvement("tm", {
            "improvement_id": "imp-1", "parent_license_id": "lic-a",
            "title": "改进", "ownership": "joint",
            "owner_shares": [
                {"owner_id": "univ", "share_percent": "50"},
                {"owner_id": "co-a", "share_percent": "50"},
            ],
            "idempotency_key": "imp-1",
        })
        self.assertEqual(result["version_no"], 1)

    def test_case_by_case_resolves_from_license_policy(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a")
        # 许可政策为 joint 且登记时仍写 case_by_case：按 joint 处理，缺份额即报错。
        with self.assertRaises(InvalidState):
            self.service.file_improvement("tm", {
                "improvement_id": "imp-1", "parent_license_id": "lic-a",
                "title": "改进", "ownership": "case_by_case", "idempotency_key": "imp-1",
            })

    def test_case_by_case_resolves_to_licensor_policy(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a", license_type="non_exclusive")
        # 非独占许可仍可带 licensor 政策：单独再签一张 licensor 政策许可。
        self.service.issue_license("legal", {
            "license_id": "lic-l", "agreement_id": "agr-1", "version_no": 1,
            "licensee_id": "co-l", "license_type": "non_exclusive", "regions": ["JP"],
            "valid_from": "2026-02-01", "valid_until": "2027-12-31",
            "improvement_policy": "licensor", "idempotency_key": "lic-l",
        })
        result = self.service.file_improvement("tm", {
            "improvement_id": "imp-9", "parent_license_id": "lic-l",
            "title": "改进", "ownership": "case_by_case", "idempotency_key": "imp-9",
        })
        self.assertEqual(result["ownership"], "licensor")

    def test_joint_requires_shares(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a")
        with self.assertRaises((InvalidState, ValidationFailed)):
            self.service.file_improvement("tm", {
                "improvement_id": "imp-1", "parent_license_id": "lic-a",
                "title": "改进", "ownership": "joint", "idempotency_key": "imp-1",
            })


class RevenueLifecycleTests(ServiceTestBase):
    def _setup_license(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a")

    def _estimate(self, gross: str = "1000.00") -> dict:
        return self.service.estimate_revenue("fin", {
            "installment_id": "pay-1", "license_id": "lic-a",
            "period_label": "2026Q1", "gross_amount": gross, "currency": "CNY",
            "idempotency_key": "pay-1",
        })

    def test_estimate_then_assess_allocates_by_share(self) -> None:
        self._setup_license()
        self._estimate()
        result = self.service.assess_revenue("fin", "pay-1", "1200.00")
        amounts = {row["payee_id"]: row["allocated_amount"] for row in result["allocations"]}
        self.assertEqual(amounts, {"univ": "720.00", "equipco": "480.00"})
        self.assertEqual(result["allocated_total"], "1200.00")

    def test_dispute_freezes_only_related_share(self) -> None:
        self._setup_license()
        self._estimate()
        self.service.assess_revenue("fin", "pay-1", "1000.00")
        result = self.service.open_dispute("fin", "pay-1", "equipco", "异议")
        statuses = {row["payee_id"]: row["status"] for row in result["allocations"]}
        self.assertEqual(statuses, {"univ": "pending", "equipco": "frozen"})
        # 未冻结份额照常结算。
        settled = self.service.settle_revenue("fin", "pay-1", ["univ"])
        univ = next(row for row in settled["allocations"] if row["payee_id"] == "univ")
        self.assertEqual(univ["status"], "settled")
        # 已结算份额不能再被争议冻结。
        with self.assertRaises(InvalidState):
            self.service.open_dispute("fin", "pay-1", "univ", "再异议")

    def test_reassessment_does_not_rewrite_settled(self) -> None:
        self._setup_license()
        self._estimate()
        self.service.assess_revenue("fin", "pay-1", "1200.00")
        self.service.settle_revenue("fin", "pay-1", ["univ"])  # 720
        self.service.open_dispute("fin", "pay-1", "equipco", "异议")
        result = self.service.assess_revenue("fin", "pay-1", "1500.00")
        rows = {row["payee_id"]: row for row in result["allocations"]}
        self.assertEqual(rows["univ"]["allocated_amount"], "720.00")
        self.assertEqual(rows["univ"]["settled_amount"], "720.00")
        self.assertEqual(rows["equipco"]["allocated_amount"], "780.00")
        self.assertEqual(rows["equipco"]["status"], "frozen")
        with self.assertRaises(InvalidState):
            self.service.assess_revenue("fin", "pay-1", "700.00")

    def test_new_agreement_cannot_change_historical_installment_basis(self) -> None:
        self._setup_license()
        self._estimate()
        self.service.assess_revenue("fin", "pay-1", "1000.00")
        self.service.settle_revenue("fin", "pay-1")
        # 之后出现的新协议版本不得影响已结算期次。
        self.service.draft_agreement("tm", {
            "agreement_id": "agr-1", "achievement_id": "mat-1", "version_label": "v2",
            "effective_on": "2028-01-01", "ownership": [
                {"owner_id": "univ", "share_percent": "90"},
                {"owner_id": "equipco", "share_percent": "10"},
            ],
        })
        self.service.ratify_agreement("legal", "agr-1", 2)
        snapshot = self.service.installment_snapshot("pay-1")
        self.assertEqual(snapshot["version_no"], 1)
        self.assertEqual(snapshot["state"], "settled")
        explanation = self.service.explain_installment("audit", "pay-1")
        self.assertEqual(explanation["agreement_snapshot"]["version_no"], 1)

    def test_settle_requires_assessment(self) -> None:
        self._setup_license()
        self._estimate()
        with self.assertRaises(InvalidState):
            self.service.settle_revenue("fin", "pay-1")

    def test_finance_cannot_issue_license(self) -> None:
        self.draft_and_ratify()
        with self.assertRaises(Forbidden):
            self.service.issue_license("fin", {
                "license_id": "lic-x", "agreement_id": "agr-1", "version_no": 1,
                "licensee_id": "co-a", "license_type": "exclusive", "regions": ["CN:BJ"],
                "valid_from": "2026-02-01", "valid_until": "2027-12-31",
                "improvement_policy": "joint", "idempotency_key": "lic-x",
            })


class ChainAndExplainTests(ServiceTestBase):
    def test_chain_rebuild_and_explain(self) -> None:
        self.draft_and_ratify()
        self.issue("lic-a")
        self.service.file_improvement("tm", {
            "improvement_id": "imp-1", "parent_license_id": "lic-a", "title": "改进",
            "ownership": "licensor", "idempotency_key": "imp-1",
        })
        self.service.estimate_revenue("fin", {
            "installment_id": "pay-1", "license_id": "lic-a", "period_label": "2026Q1",
            "gross_amount": "500.00", "idempotency_key": "pay-1",
        })
        chain = self.service.rebuild_license_chain("tm", achievement_id="mat-1")
        self.assertEqual([item["license_id"] for item in chain["licenses"]], ["lic-a"])
        self.assertEqual(chain["licenses"][0]["improvements"][0]["improvement_id"], "imp-1")
        self.assertEqual(chain["licenses"][0]["installments"][0]["installment_id"], "pay-1")
        by_licensee = self.service.rebuild_license_chain("tm", licensee_id="co-a")
        self.assertEqual(by_licensee["chain_roots"], ["lic-a"])
        explanation = self.service.explain_installment("audit", "pay-1")
        self.assertTrue(explanation["allocation_basis"][0]["explanation"])
        self.assertEqual(explanation["timeline"][0]["event_type"], "revenue.estimated")

    def test_chain_requires_filter(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.rebuild_license_chain("tm")

    def test_audit_chain_valid(self) -> None:
        self.draft_and_ratify()
        audit = self.service.audit_chain("audit")
        self.assertTrue(audit["valid"])
        self.assertGreater(audit["events"], 0)


if __name__ == "__main__":
    unittest.main()
