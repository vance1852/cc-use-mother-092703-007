from __future__ import annotations

import unittest
from decimal import Decimal

from license_revenue.rules import (
    LicenseGrant,
    allocate_pool,
    allocate_revenue,
    date_overlap,
    find_grant_conflicts,
    grants_intersect,
    region_overlap,
    scope_overlap,
)


def grant(
    license_id: str,
    licensee: str = "co-a",
    license_type: str = "non_exclusive",
    region: str = "CN:BJ",
    scope: str = "all",
    start: str = "2026-01-01",
    end: str | None = "2027-01-01",
) -> LicenseGrant:
    return LicenseGrant(license_id, licensee, license_type, region, scope, start, end)


class RegionTests(unittest.TestCase):
    def test_region_hierarchy(self) -> None:
        self.assertTrue(region_overlap("CN:BJ", "CN:BJ:HD"))
        self.assertTrue(region_overlap("CN", "CN:TJ"))
        self.assertTrue(region_overlap("WORLD", "JP"))
        self.assertFalse(region_overlap("CN:BJ", "CN:SH"))
        self.assertFalse(region_overlap("JP", "KR"))

    def test_scope(self) -> None:
        self.assertTrue(scope_overlap("all", "battery"))
        self.assertTrue(scope_overlap("battery", "battery"))
        self.assertFalse(scope_overlap("battery", "coating"))

    def test_date_overlap(self) -> None:
        self.assertTrue(date_overlap("2026-01-01", "2026-12-31", "2026-06-01", None))
        self.assertTrue(date_overlap("2026-01-01", None, "2099-01-01", None))
        self.assertTrue(date_overlap("2026-01-01", "2026-12-31", "2026-12-31", "2027-06-30"))
        self.assertFalse(date_overlap("2026-01-01", "2026-12-30", "2026-12-31", "2027-06-30"))
        self.assertFalse(date_overlap("2026-01-01", "2026-12-31", "2027-01-01", "2028-01-01"))


class ConflictTests(unittest.TestCase):
    def test_exclusive_vs_non_exclusive_conflicts(self) -> None:
        existing = [grant("lic-1", "co-a", "exclusive", "CN:BJ")]
        candidate = [grant("lic-2", "co-b", "non_exclusive", "CN:BJ:HD")]
        conflicts = find_grant_conflicts(candidate, existing)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].existing_license_id, "lic-1")

    def test_disjoint_region_no_conflict(self) -> None:
        existing = [grant("lic-1", "co-a", "exclusive", "CN:BJ")]
        candidate = [grant("lic-2", "co-b", "exclusive", "JP")]
        self.assertEqual(find_grant_conflicts(candidate, existing), [])

    def test_disjoint_time_no_conflict(self) -> None:
        existing = [grant("lic-1", "co-a", "exclusive", "CN:BJ", end="2027-12-31")]
        candidate = [grant("lic-2", "co-b", "exclusive", "CN:BJ", start="2028-01-01", end="2029-01-01")]
        self.assertEqual(find_grant_conflicts(candidate, existing), [])

    def test_disjoint_field_scope_no_conflict(self) -> None:
        existing = [grant("lic-1", "co-a", "exclusive", "CN:BJ", scope="battery")]
        candidate = [grant("lic-2", "co-b", "exclusive", "CN:BJ", scope="coating")]
        self.assertEqual(find_grant_conflicts(candidate, existing), [])
        self.assertTrue(grants_intersect(
            grant("lic-1", scope="all"), grant("lic-2", scope="coating")
        ))

    def test_sole_conflicts_with_other_licensee(self) -> None:
        existing = [grant("lic-1", "co-a", "sole", "CN")]
        candidate = [grant("lic-2", "co-b", "non_exclusive", "CN:BJ")]
        self.assertEqual(len(find_grant_conflicts(candidate, existing)), 1)

    def test_two_non_exclusive_different_licensees_coexist(self) -> None:
        existing = [grant("lic-1", "co-a", "non_exclusive", "CN:BJ")]
        candidate = [grant("lic-2", "co-b", "non_exclusive", "CN:BJ")]
        self.assertEqual(find_grant_conflicts(candidate, existing), [])

    def test_same_licensee_overlap_requires_explicit_renewal(self) -> None:
        existing = [grant("lic-1", "co-a", "non_exclusive", "CN:BJ")]
        candidate = [grant("lic-2", "co-a", "non_exclusive", "CN:BJ")]
        self.assertEqual(len(find_grant_conflicts(candidate, existing)), 1)


class AllocationTests(unittest.TestCase):
    def test_allocation_sums_to_gross_with_remainder_rule(self) -> None:
        lines = allocate_revenue(Decimal("100.00"), [
            {"payee_id": "a", "share_percent": Decimal("33.3333")},
            {"payee_id": "b", "share_percent": Decimal("33.3333")},
            {"payee_id": "c", "share_percent": Decimal("33.3334")},
        ])
        amounts = {line["payee_id"]: line["allocated_amount"] for line in lines}
        self.assertEqual(sum(amounts.values(), Decimal("0")), Decimal("100.00"))
        # 三行舍入后均为 33.33，余 1 分确定性归入编号最小的 a。
        self.assertEqual(amounts["a"], Decimal("33.34"))
        self.assertTrue(lines[0].get("remainder_adjusted"))

    def test_allocation_partial_share_leaves_retained(self) -> None:
        lines = allocate_revenue(Decimal("100.00"), [
            {"payee_id": "a", "share_percent": Decimal("60")},
        ])
        self.assertEqual(lines[0]["allocated_amount"], Decimal("60.00"))

    def test_allocate_pool_relative_weights(self) -> None:
        lines = allocate_pool(Decimal("100.00"), [
            ("a", Decimal("70"), "explicit"),
            ("b", Decimal("30"), "explicit"),
        ])
        amounts = {line["payee_id"]: line["allocated_amount"] for line in lines}
        self.assertEqual(amounts, {"a": Decimal("70.00"), "b": Decimal("30.00")})

    def test_allocate_pool_rejects_zero_weights(self) -> None:
        with self.assertRaises(ValueError):
            allocate_pool(Decimal("10.00"), [("a", Decimal("0"), "x")])


if __name__ == "__main__":
    unittest.main()
