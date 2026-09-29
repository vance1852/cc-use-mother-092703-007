"""贯通成果权属、协议版本、许可冲突、续约与分期收益的离线验收。

运行::

    PYTHONPATH=src python3 -m license_revenue.acceptance

场景：高校团队（univ）与装备企业（equipco）共有一项新材料工艺。
协议 v1（60/40）下签发北京独占许可；v2（70/30）生效后完成续约；
日本普通许可与北京范围不相交而共存；续约谈判期间延期被拒；
收益经历暂估→核定→部分结算→争议冻结→再核定（不倒改已结算）→裁定→结清，
并重建许可链、解释每笔收入。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict, InvalidState
from .service import LicensingService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = LicensingService(connection, FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    for user_id, role in (("tm", "manager"), ("legal", "legal"), ("fin", "finance"), ("audit", "auditor")):
        service.create_user(user_id, user_id, role)

    # 成果与权属依据。
    service.register_achievement("tm", {
        "achievement_id": "mat-x1",
        "title": "新型复合材料成型工艺",
        "field_kind": "patent",
        "completed_on": "2025-11-01",
        "basis_note": "高校团队与装备企业联合研发，协议为权属依据",
    })
    agreement_v1 = {
        "agreement_id": "agr-1",
        "achievement_id": "mat-x1",
        "version_label": "v1-60-40",
        "effective_on": "2026-01-01",
        "expires_on": "2030-12-31",
        "basis_summary": "高校 60% / 装备企业 40%，改进共有",
        "ownership": [
            {"owner_id": "univ", "share_percent": "60", "kind": "patent"},
            {"owner_id": "equipco", "share_percent": "40", "kind": "know_how"},
        ],
    }
    service.draft_agreement("tm", agreement_v1)
    service.ratify_agreement("legal", "agr-1", 1)

    # 北京独占许可。
    beijing_license = {
        "license_id": "lic-bj",
        "agreement_id": "agr-1",
        "version_no": 1,
        "licensee_id": "factory-bj",
        "license_type": "exclusive",
        "regions": ["CN:BJ"],
        "field_scope": "all",
        "valid_from": "2026-02-01",
        "valid_until": "2027-12-31",
        "improvement_policy": "joint",
        "idempotency_key": "lic-bj-1",
    }
    service.issue_license("legal", beijing_license)

    # 授权范围相交：北京海淀与 CN:BJ 相交，独占冲突，阻止签发。
    overlap_error: dict[str, object] = {}
    try:
        service.issue_license("legal", {
            **beijing_license,
            "license_id": "lic-bj-hd",
            "licensee_id": "factory-hd",
            "regions": ["CN:BJ:HD"],
            "idempotency_key": "lic-bj-hd-1",
        })
    except Conflict as exc:
        overlap_error = {"code": exc.code, "first_conflict": exc.details[0]}

    # 时间不相交（旧许可到期后）也不能签独占重叠范围——这里验证日期窗口。
    # 日本普通许可地域不相交，可以共存。
    service.issue_license("legal", {
        "license_id": "lic-jp",
        "agreement_id": "agr-1",
        "version_no": 1,
        "licensee_id": "factory-jp",
        "license_type": "non_exclusive",
        "regions": ["JP"],
        "valid_from": "2026-03-01",
        "valid_until": "2027-12-31",
        "improvement_policy": "licensor",
        "idempotency_key": "lic-jp-1",
    })

    # 改进成果绑定到旧许可及其协议版本。
    service.file_improvement("tm", {
        "improvement_id": "imp-001",
        "parent_license_id": "lic-bj",
        "title": "低温固化工艺改进",
        "ownership": "joint",
        "owner_shares": [
            {"owner_id": "univ", "share_percent": "50"},
            {"owner_id": "factory-bj", "share_percent": "50"},
        ],
        "idempotency_key": "imp-001-1",
    })

    # 续约谈判：打开后不得擅自延长旧许可。
    service.open_renewal("legal", "lic-bj", "2028 年续约谈判")
    extension_blocked = ""
    try:
        service.extend_license("legal", "lic-bj", "2028-06-30")
    except InvalidState as exc:
        extension_blocked = str(exc)

    # 新协议版本 v2：权属调整为 70/30；生效后旧版本被替代但不被改写。
    agreement_v2 = {
        **agreement_v1,
        "version_label": "v2-70-30",
        "effective_on": "2028-01-01",
        "basis_summary": "高校 70% / 装备企业 30%",
        "ownership": [
            {"owner_id": "univ", "share_percent": "70", "kind": "patent"},
            {"owner_id": "equipco", "share_percent": "30", "kind": "know_how"},
        ],
    }
    service.draft_agreement("tm", agreement_v2)
    service.ratify_agreement("legal", "agr-1", 2)

    renewed = service.complete_renewal("legal", 1, {
        "license_id": "lic-bj-2",
        "agreement_id": "agr-1",
        "version_no": 2,
        "licensee_id": "factory-bj",
        "license_type": "exclusive",
        "regions": ["CN:BJ"],
        "valid_from": "2028-01-01",
        "valid_until": "2030-06-30",
        "improvement_policy": "joint",
        "idempotency_key": "lic-bj-2-1",
    })

    # 分期收益：先暂估 1000，核定 1200（70/30 = 840/360）。
    service.estimate_revenue("fin", {
        "installment_id": "pay-2028h1",
        "license_id": "lic-bj-2",
        "period_label": "2028H1",
        "gross_amount": "1000.00",
        "currency": "CNY",
        "idempotency_key": "pay-2028h1-est",
    })
    service.assess_revenue("fin", "pay-2028h1", "1200.00", "核定金额上浮")
    # 先结算高校 840。
    service.settle_revenue("fin", "pay-2028h1", ["univ"])
    # 装备企业对其份额提争议，只冻结其份额。
    service.open_dispute("fin", "pay-2028h1", "equipco", "对提成口径有异议")
    # 再核定为 1500：已结算的 840 锁定不动，剩余 660 归冻结中的装备企业。
    reassessed = service.assess_revenue("fin", "pay-2028h1", "1500.00", "二次核定")
    # 核定金额不得低于已结算金额。
    low_blocked = ""
    try:
        service.assess_revenue("fin", "pay-2028h1", "100.00")
    except InvalidState as exc:
        low_blocked = str(exc)
    # 争议裁定为 600，余下 60 元作为机构留存。
    resolved = service.resolve_dispute("fin", 1, "调解后按 600 支付", adjusted_amount="600.00")
    final = service.settle_revenue("fin", "pay-2028h1")

    chain = service.rebuild_license_chain("tm", achievement_id="mat-x1")
    by_licensee = service.rebuild_license_chain("tm", licensee_id="factory-bj")
    explanation = service.explain_installment("audit", "pay-2028h1")
    audit = service.audit_chain("audit")

    result = {
        "status": "ok",
        "overlap_blocked": overlap_error,
        "extension_blocked_during_renewal": extension_blocked,
        "renewed_license": renewed,
        "agreement_versions": [
            {"version_no": item["version_no"], "state": item["state"]}
            for item in chain["agreement_versions"]
        ],
        "license_chain": [
            {
                "license_id": item["license_id"],
                "state": item["state"],
                "version_no": item["version_no"],
                "renewal_of": item["renewal_of_license_id"],
                "regions": item["regions"],
                "improvements": [imp["improvement_id"] for imp in item["improvements"]],
            }
            for item in chain["licenses"]
        ],
        "revenue": {
            "state": final["state"],
            "gross_amount": final["gross_amount"],
            "allocations": final["allocations"],
            "retained_amount": final["retained_amount"],
            "reassessed_amounts": [
                {"payee_id": line["payee_id"], "amount": line["allocated_amount"], "status": line["status"]}
                for line in reassessed["allocations"]
            ],
            "resolved_amounts": [
                {"payee_id": line["payee_id"], "amount": line["allocated_amount"], "status": line["status"]}
                for line in resolved["allocations"]
            ],
            "low_assess_blocked": low_blocked,
        },
        "chain_by_licensee_roots": by_licensee["chain_roots"],
        "explanation_basis": [
            {"payee_id": line["payee_id"], "stage": line["basis"]["stage"], "explanation": line["explanation"]}
            for line in explanation["allocation_basis"]
        ],
        "audit": audit,
        "workspace": workspace.name,
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
