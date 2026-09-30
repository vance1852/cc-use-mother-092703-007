"""贯通协议版本、授权签发、续约管控、改进成果、分期收益与争议冻结的离线验收。

场景：高校团队与装备企业共同完成新材料工艺，技术转移办公室向多家承接单位授权，
不同协议版本中权属比例写法不一。验收覆盖：
1. 权属依据、许可条件、承接方、改进成果、分期收益绑定各自生效的协议版本；
2. 授权范围相交时阻止冲突签发；续约谈判期间不得延长旧许可、不得另签新许可；
3. 收益先暂估再核定；争议只冻结相关主体份额；已结算部分不因新协议倒改；
4. 按成果、按承接方重建许可链；逐笔解释收入分配依据。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict, InvalidState
from .service import LicenseOfficeService


def _agreement(agreement_id: str, version: str, effective: str, expiry: str, shares: list[tuple[str, str, str]], supersedes: str | None = None) -> dict:
    return {
        "agreement_id": agreement_id,
        "achievement_id": "mat-2026-01",
        "title": "高强韧复合涂层新工艺许可协议",
        "version": version,
        "effective_date": effective,
        "expiry_date": expiry,
        "supersedes_agreement_id": supersedes,
        "ownership": [
            {"owner_id": owner, "owner_kind": kind, "share": share, "basis": basis}
            for owner, kind, share, basis in shares
        ],
    }


V1_SHARES = [
    ("uni-lab", "university_team", "0.60", "高校团队成果转化办法 v1：职务发明 60%"),
    ("equip-co", "enterprise", "0.40", "合作协议第 8 条：装备企业作价出资 40%"),
]
V2_SHARES = [
    ("uni-lab", "university_team", "0.55", "高校团队成果转化办法 v2：职务发明 55%"),
    ("equip-co", "enterprise", "0.40", "合作协议第 8 条：装备企业作价出资 40%"),
    ("tto-platform", "platform", "0.05", "学校平台服务补偿 5%"),
]
V3_SHARES = [
    ("uni-lab", "university_team", "0.50", "高校团队成果转化办法 v3：职务发明 50%"),
    ("equip-co", "enterprise", "0.35", "补充协议：装备企业 35%"),
    ("tto-platform", "platform", "0.15", "学校平台服务补偿 15%"),
]


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = LicenseOfficeService(connection, FrozenClock(datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)))
    blocked: list[str] = []

    for user_id, role in (("mgr", "manager"), ("legal", "legal"), ("fin", "finance"), ("aud", "auditor")):
        service.create_user(user_id, user_id, role)

    service.create_achievement("mgr", {"achievement_id": "mat-2026-01", "name": "高强韧复合涂层新工艺", "domain": "新材料/表面工程"})
    for licensee_id, name in (
        ("ent-north", "北方装备制造有限公司"),
        ("ent-south", "南方新材股份有限公司"),
        ("ent-east", "东方涂层科技有限公司"),
        ("ent-oem", "通用代工厂"),
    ):
        service.register_licensee("mgr", {"licensee_id": licensee_id, "name": name, "credit_code": f"91{licensee_id}", "contact": "法务部"})

    # 协议版本 v1：权属 60/40
    service.register_agreement("legal", _agreement("agr-v1", "v1-2025", "2025-01-01", "2027-12-31", V1_SHARES))

    def grant(grant_id, agreement_id, licensee_id, kind, regions, start, end, royalty_type="sales_rate", rate="0.05", fixed=None):
        return {
            "grant_id": grant_id,
            "agreement_id": agreement_id,
            "licensee_id": licensee_id,
            "grant_kind": kind,
            "territories": [{"region_code": code, "region_name": label} for code, label in regions],
            "fields_of_use": "复合涂层工艺商业化实施",
            "start_date": start,
            "end_date": end,
            "royalty": {"royalty_type": royalty_type, "rate": rate, "fixed_amount": fixed, "note": "按销售额提成"},
        }

    NORTH = [("CN-NORTH", "华北")]
    SOUTH = [("CN-SOUTH", "华南")]
    EAST = [("CN-EAST", "华东")]

    service.issue_grant("mgr", grant("g-north", "agr-v1", "ent-north", "non_exclusive", NORTH, "2025-02-01", "2026-12-31"))
    service.issue_grant("mgr", grant("g-south", "agr-v1", "ent-south", "non_exclusive", SOUTH, "2025-03-01", "2026-12-31"))
    # 不同承接方、普通非排他授权在同一地域可以共存
    service.issue_grant("mgr", grant("g-oem-south", "agr-v1", "ent-oem", "non_exclusive", SOUTH, "2025-03-01", "2026-06-30", rate="0.06"))

    # 相交范围冲突：排他授权撞上已生效的华北普通授权
    try:
        service.issue_grant("mgr", grant("g-north-exclusive", "agr-v1", "ent-east", "exclusive", NORTH, "2026-01-01", "2026-12-31"))
    except Conflict as exc:
        blocked.append(f"exclusive-overlap:{exc}")
    # 相交范围冲突：同一承接方在华南重复签发
    try:
        service.issue_grant("mgr", grant("g-south-dup", "agr-v1", "ent-south", "non_exclusive", SOUTH, "2026-05-01", "2026-10-31"))
    except Conflict as exc:
        blocked.append(f"same-licensee-dup:{exc}")

    # 改进成果绑定到 v1（共同改进，归属按约定共有）
    service.record_improvement("mgr", {
        "improvement_id": "imp-01",
        "parent_achievement_id": "mat-2026-01",
        "agreement_id": "agr-v1",
        "title": "前处理工序低温改良",
        "ownership_rule": "joint",
        "contributor_licensee_id": "ent-south",
        "derived_grant_id": "g-south",
    })

    # 北方收益：暂估 100000 → 核定 102000（按 v1 的 60/40）
    service.estimate_revenue("fin", {"period_id": "p-north-2026q1", "grant_id": "g-north", "period_date": "2026-03-31", "estimated_amount": "100000", "idempotency_key": "est-north-q1"})
    assessed = service.assess_revenue("fin", "p-north-2026q1", "102000")
    settled = service.settle_revenue("fin", "p-north-2026q1")
    assert settled["status"] == "settled"
    lines = {row["owner_id"]: row for row in assessed["allocations"]}
    assert Decimal(lines["uni-lab"]["gross_amount"]) == Decimal("61200.00")
    assert Decimal(lines["equip-co"]["gross_amount"]) == Decimal("40800.00")
    # 已结算后再核定必须被阻止（不得倒改）
    try:
        service.assess_revenue("fin", "p-north-2026q1", "999999")
    except InvalidState as exc:
        blocked.append(f"reassess-settled:{exc}")

    # 协议版本 v2：权属改为 55/40/5，旧版本标记 superseded，已签发授权与已结算收益不动
    service.register_agreement("legal", _agreement("agr-v2", "v2-2026", "2026-07-01", "2028-12-31", V2_SHARES, supersedes="agr-v1"))
    assert service.agreement("agr-v1")["state"] == "superseded"
    # 旧协议失效后不能再据其签发新授权
    try:
        service.issue_grant("mgr", grant("g-late", "agr-v1", "ent-east", "non_exclusive", EAST, "2026-08-01", "2026-12-31"))
    except InvalidState as exc:
        blocked.append(f"issue-under-superseded:{exc}")
    service.issue_grant("mgr", grant("g-east", "agr-v2", "ent-east", "non_exclusive", EAST, "2026-08-01", "2027-12-31", royalty_type="tiered", rate=None, fixed=None))
    service.record_improvement("mgr", {
        "improvement_id": "imp-02",
        "parent_achievement_id": "mat-2026-01",
        "agreement_id": "agr-v2",
        "title": "固化剂配方调整",
        "ownership_rule": "licensee",
        "contributor_licensee_id": "ent-east",
        "derived_grant_id": "g-east",
    })

    # 续约谈判：打开后不得延长旧许可，也不得给同承接方另签新许可
    renewal = service.open_renewal("legal", "g-north", "承接方要求扩大地域并调整提成率")
    try:
        service.extend_grant("mgr", "g-north", "2027-06-30")
    except InvalidState as exc:
        blocked.append(f"extend-during-renewal:{exc}")
    try:
        service.issue_grant("mgr", grant("g-north-side", "agr-v2", "ent-north", "non_exclusive", NORTH, "2027-01-01", "2027-12-31"))
    except InvalidState as exc:
        blocked.append(f"issue-during-renewal:{exc}")
    # 谈判期间旧许可内容保持原样（到期日未被擅改）
    assert service.grant("g-north")["end_date"] == "2026-12-31"
    # 续约达成：按 v2 签发新授权，旧授权在同一事务内终止交接
    signed = service.sign_renewal("legal", renewal["renewal_id"], grant("g-north-2", "agr-v2", "ent-north", "non_exclusive", NORTH, "2027-01-01", "2028-06-30", rate="0.045"))
    assert signed["new_grant"]["agreement_id"] == "agr-v2"
    assert service.grant("g-north")["state"] == "terminated"

    # 华南收益：核定后发生争议，只冻结装备企业 40% 份额
    service.estimate_revenue("fin", {"period_id": "p-south-2026q2", "grant_id": "g-south", "period_date": "2026-06-30", "estimated_amount": "80000", "idempotency_key": "est-south-q2"})
    service.assess_revenue("fin", "p-south-2026q2", "80000")
    dispute = service.open_dispute("fin", reason="装备企业对作价出资比例提出异议", subject_owner_ids=["equip-co"], period_id="p-south-2026q2")
    partial = service.period("p-south-2026q2")
    south_lines = {row["owner_id"]: row for row in partial["allocations"]}
    assert Decimal(south_lines["equip-co"]["frozen_amount"]) == Decimal("32000.00")
    assert Decimal(south_lines["equip-co"]["settled_amount"]) == Decimal("0.00")
    # 第一次结算：高校份额正常结算，争议份额保持冻结，收益期不闭环
    first_settle = service.settle_revenue("fin", "p-south-2026q2")
    assert first_settle["status"] == "assessed"
    assert Decimal(first_settle["totals"]["settled_amount"]) == Decimal("48000.00")
    # 对已结算收益期再提争议，不产生任何回冲冻结
    service.open_dispute("fin", reason="历史凭证复核", subject_owner_ids=["equip-co"], period_id="p-north-2026q1")
    assert Decimal(service.period("p-north-2026q1")["totals"]["frozen_amount"]) == Decimal("0.00")
    # 争议解决 → 解冻 → 剩余份额结算
    service.resolve_dispute("fin", dispute["dispute_id"], "复核后确认原 40% 比例有效")
    final_settle = service.settle_revenue("fin", "p-south-2026q2")
    assert final_settle["status"] == "settled"
    assert Decimal(final_settle["totals"]["settled_amount"]) == Decimal("80000.00")

    # 协议版本 v3：50/35/15。已结算的历史收益分配快照不得变化
    service.register_agreement("legal", _agreement("agr-v3", "v3-2027", "2027-03-01", "2030-12-31", V3_SHARES, supersedes="agr-v2"))
    service.issue_grant("mgr", grant("g-east-2", "agr-v3", "ent-east", "non_exclusive", EAST, "2028-01-01", "2029-06-30", royalty_type="tiered", rate=None, fixed=None))
    service.estimate_revenue("fin", {"period_id": "p-east-2028q1", "grant_id": "g-east-2", "period_date": "2028-03-31", "estimated_amount": "100000", "idempotency_key": "est-east-2028q1"})
    east_period = service.period("p-east-2028q1")
    east_lines = {row["owner_id"]: row for row in east_period["allocations"]}
    assert Decimal(east_lines["uni-lab"]["gross_amount"]) == Decimal("50000.00")
    assert Decimal(east_lines["equip-co"]["gross_amount"]) == Decimal("35000.00")
    assert Decimal(east_lines["tto-platform"]["gross_amount"]) == Decimal("15000.00")
    # 历史快照：北方那期仍按 v1 的 60/40 解释
    north_explain = service.explain_revenue("aud", "p-north-2026q1")
    assert north_explain["agreement"]["agreement_id"] == "agr-v1"
    assert north_explain["period"]["status"] == "settled"

    # 许可链重建：按成果 / 按承接方
    chain_by_achievement = service.license_chain("mgr", achievement_id="mat-2026-01")
    chain_by_licensee = service.license_chain("mgr", licensee_id="ent-north")
    versions = [item["version"] for item in chain_by_achievement["agreement_versions"]]
    assert versions == ["v1-2025", "v2-2026", "v3-2027"]
    north_grants = [item["grant_id"] for item in chain_by_licensee["grants"]]
    assert north_grants == ["g-north", "g-north-2"]
    assert {item["improvement_id"] for item in chain_by_achievement["improvements"]} == {"imp-01", "imp-02"}

    audit = service.audit_chain("aud")
    assert audit["valid"]

    result = {
        "status": "ok",
        "workspace": workspace.name,
        "blocked_actions": blocked,
        "agreement_versions": versions,
        "north_history": {
            "period": "p-north-2026q1",
            "agreement_id": north_explain["agreement"]["agreement_id"],
            "ownership": [(item["owner_id"], item["share"]) for item in north_explain["ownership_basis"]],
            "settled_amount": service.period("p-north-2026q1")["totals"]["settled_amount"],
            "reasons": north_explain["reasons"],
        },
        "south_dispute": {
            "frozen_at_first_settlement": "32000.00",
            "settled_before_resolution": first_settle["totals"]["settled_amount"],
            "settled_after_resolution": final_settle["totals"]["settled_amount"],
        },
        "east_v3_split": {owner: east_lines[owner]["gross_amount"] for owner in east_lines},
        "chains": {
            "by_achievement": {
                "grants": [item["grant_id"] for item in chain_by_achievement["grants"]],
                "improvements": [item["improvement_id"] for item in chain_by_achievement["improvements"]],
                "renewals": [(item["grant_id"], item["state"], item["new_grant_id"]) for item in chain_by_achievement["renewals"]],
            },
            "by_licensee_north": north_grants,
        },
        "audit": audit,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行成果许可与收益管理服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
