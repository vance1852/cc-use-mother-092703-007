"""成果许可与收益管理的事务用例。

设计要点：
- 协议是不可变版本：权属份额、许可条件、承接方、改进成果、每期收益都通过外键
  绑定到签发当时生效的协议版本；新版本协议登记后旧版本标记 superseded，
  但已经绑定的授权与收益快照不变。
- 签发授权时在同一成果内做时间区间 × 地域相交检测，排他/独家与任何相交授权冲突，
  同一承接方重复签发同样阻止。
- 续约谈判期间，旧授权既不能延期也不能由该承接方取得新授权，必须经续约结论闭环。
- 收益分期：estimated（暂估）→ assessed（核定）→ settled（结算）。每次金额变化都
  重算分成明细，但任何主体已结算金额只增不改；争议只把相关主体的未结算份额冻结。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import AgreementInput, GrantInput, ImprovementInput, LicenseeInput
from .rules import (
    GrantShape,
    OwnerShare,
    canonical_json,
    decimal_text,
    digest,
    distribute_revenue,
    find_conflict,
    quantize_money,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "manager": {
        "achievement.write",
        "licensee.write",
        "agreement.write",
        "grant.write",
        "improvement.write",
        "renewal.write",
        "chain.read",
    },
    "legal": {
        "agreement.write",
        "grant.write",
        "renewal.write",
        "dispute.write",
        "chain.read",
    },
    "finance": {
        "revenue.write",
        "dispute.write",
        "explain.read",
        "chain.read",
    },
    "auditor": {"audit.read", "chain.read", "explain.read"},
}


class LicenseOfficeService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ----- 基础 -----

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM office_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM office_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = digest(body)
        self.connection.execute(
            "INSERT INTO office_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO office_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ----- 成果 -----

    def create_achievement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "achievement.write")
        achievement_id = raw.get("achievement_id")
        name = raw.get("name")
        domain = raw.get("domain", "")
        if not isinstance(achievement_id, str) or not achievement_id.strip():
            raise ValidationFailed("achievement_id 不能为空")
        if not isinstance(name, str) or not name.strip():
            raise ValidationFailed("name 不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO achievements(achievement_id,name,domain,created_at) VALUES(?,?,?,?)",
                    (achievement_id.strip(), name.strip(), str(domain).strip(), self._now()),
                )
                self._audit("achievement", achievement_id.strip(), "achievement.created", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("成果编号已经存在") from exc
        return {"achievement_id": achievement_id.strip(), "name": name.strip()}

    def _get_achievement(self, achievement_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM achievements WHERE achievement_id=?", (achievement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("成果不存在")
        return row

    # ----- 承接方 -----

    def register_licensee(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "licensee.write")
        licensee = LicenseeInput.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO licensees(licensee_id,name,credit_code,contact,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        licensee.licensee_id,
                        licensee.name,
                        licensee.credit_code,
                        licensee.contact,
                        self._now(),
                    ),
                )
                self._audit("licensee", licensee.licensee_id, "licensee.registered", actor_id, raw)
        except sqlite3.IntegrityError as exc:
            raise Conflict("承接方编号或统一社会信用代码已存在") from exc
        return {
            "licensee_id": licensee.licensee_id,
            "name": licensee.name,
            "credit_code": licensee.credit_code,
        }

    # ----- 协议版本与权属依据 -----

    def register_agreement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "agreement.write")
        agreement = AgreementInput.from_dict(raw)
        self._get_achievement(agreement.achievement_id)
        if agreement.supersedes_agreement_id is not None:
            previous = self.connection.execute(
                "SELECT * FROM agreements WHERE agreement_id=?",
                (agreement.supersedes_agreement_id,),
            ).fetchone()
            if previous is None:
                raise NotFound("被替代的协议版本不存在")
            if previous["achievement_id"] != agreement.achievement_id:
                raise ValidationFailed("只能替代同一成果的协议版本")
        content_sha256 = digest(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO agreements(agreement_id,achievement_id,title,version,effective_date,"
                    "expiry_date,supersedes_agreement_id,content_sha256,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        agreement.agreement_id,
                        agreement.achievement_id,
                        agreement.title,
                        agreement.version,
                        agreement.effective_date,
                        agreement.expiry_date,
                        agreement.supersedes_agreement_id,
                        content_sha256,
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO ownership_shares(agreement_id,owner_id,owner_kind,share,basis) "
                    "VALUES(?,?,?,?,?)",
                    [
                        (
                            agreement.agreement_id,
                            owner.owner_id,
                            owner.owner_kind,
                            decimal_text(owner.share),
                            owner.basis,
                        )
                        for owner in agreement.ownership
                    ],
                )
                if agreement.supersedes_agreement_id is not None:
                    self.connection.execute(
                        "UPDATE agreements SET state='superseded' WHERE agreement_id=?",
                        (agreement.supersedes_agreement_id,),
                    )
                self._audit(
                    "agreement",
                    agreement.agreement_id,
                    "agreement.registered",
                    actor_id,
                    {"version": agreement.version, "supersedes": agreement.supersedes_agreement_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议编号或成果内版本号冲突") from exc
        return self.agreement(agreement.agreement_id)

    def agreement(self, agreement_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        result = dict(row)
        result["ownership"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT owner_id,owner_kind,share,basis FROM ownership_shares "
                "WHERE agreement_id=? ORDER BY owner_id",
                (agreement_id,),
            ).fetchall()
        ]
        return result

    def _agreement_row(self, agreement_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        return row

    def _owners(self, agreement_id: str) -> list[OwnerShare]:
        rows = self.connection.execute(
            "SELECT owner_id,owner_kind,share,basis FROM ownership_shares WHERE agreement_id=?",
            (agreement_id,),
        ).fetchall()
        return [
            OwnerShare(
                owner_id=row["owner_id"],
                owner_kind=row["owner_kind"],
                share=Decimal(row["share"]),
                basis=row["basis"],
            )
            for row in rows
        ]

    # ----- 授权签发 -----

    def _grant_shape(self, row: sqlite3.Row) -> GrantShape:
        territories = frozenset(
            item["region_code"]
            for item in self.connection.execute(
                "SELECT region_code FROM grant_territories WHERE grant_id=?", (row["grant_id"],)
            ).fetchall()
        )
        return GrantShape(
            grant_id=row["grant_id"],
            agreement_id=row["agreement_id"],
            licensee_id=row["licensee_id"],
            exclusive=row["grant_kind"] in {"exclusive", "sole"},
            start_date=row["start_date"],
            end_date=row["end_date"],
            territories=territories,
            state=row["state"],
        )

    def issue_grant(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "grant.write")
        grant = GrantInput.from_dict(raw)
        agreement = self._agreement_row(grant.agreement_id)
        if agreement["state"] != "effective":
            raise InvalidState("只能依据当前生效的协议版本签发授权")
        self._licensee_row(grant.licensee_id)
        # 授权窗口必须落在协议有效期内
        if grant.start_date < agreement["effective_date"] or grant.end_date > agreement["expiry_date"]:
            raise ValidationFailed("授权有效期超出协议生效区间")
        candidate = GrantShape(
            grant_id=grant.grant_id,
            agreement_id=grant.agreement_id,
            licensee_id=grant.licensee_id,
            exclusive=grant.exclusive,
            start_date=grant.start_date,
            end_date=grant.end_date,
            territories=frozenset(item.region_code for item in grant.territories),
        )
        open_renewal = self.connection.execute(
            "SELECT renewal_id FROM renewals WHERE achievement_id=? AND licensee_id=? AND state='negotiating'",
            (agreement["achievement_id"], grant.licensee_id),
        ).fetchone()
        if open_renewal is not None:
            raise InvalidState("续约谈判期间不得为该承接方签发新授权或延长旧许可")
        try:
            with transaction(self.connection, immediate=True):
                existing_rows = self.connection.execute(
                    "SELECT * FROM grants WHERE achievement_id=? AND state='active'",
                    (agreement["achievement_id"],),
                ).fetchall()
                conflicting = find_conflict(candidate, [self._grant_shape(row) for row in existing_rows])
                if conflicting is not None:
                    raise Conflict(
                        f"授权范围与已签发授权 {conflicting.grant_id} 相交"
                        + ("（含排他/独家授权）" if (candidate.exclusive or conflicting.exclusive) else "（同一承接方重复签发）")
                    )
                self.connection.execute(
                    "INSERT INTO grants(grant_id,agreement_id,achievement_id,licensee_id,grant_kind,"
                    "fields_of_use,start_date,end_date,royalty_type,rate,fixed_amount,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        grant.grant_id,
                        grant.agreement_id,
                        agreement["achievement_id"],
                        grant.licensee_id,
                        grant.grant_kind,
                        grant.fields_of_use,
                        grant.start_date,
                        grant.end_date,
                        grant.royalty.royalty_type,
                        None if grant.royalty.rate is None else decimal_text(grant.royalty.rate),
                        None
                        if grant.royalty.fixed_amount is None
                        else decimal_text(grant.royalty.fixed_amount),
                        actor_id,
                        self._now(),
                    ),
                )
                self.connection.executemany(
                    "INSERT INTO grant_territories(grant_id,region_code,region_name) VALUES(?,?,?)",
                    [
                        (grant.grant_id, item.region_code, item.region_name)
                        for item in grant.territories
                    ],
                )
                self._audit(
                    "grant",
                    grant.grant_id,
                    "grant.issued",
                    actor_id,
                    {"agreement_id": grant.agreement_id, "licensee_id": grant.licensee_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("授权编号冲突或协议/承接方不存在") from exc
        return self.grant(grant.grant_id)

    def _licensee_row(self, licensee_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM licensees WHERE licensee_id=?", (licensee_id,)
        ).fetchone()
        if row is None:
            raise NotFound("承接方不存在")
        return row

    def grant(self, grant_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise NotFound("授权不存在")
        result = dict(row)
        result["territories"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT region_code,region_name FROM grant_territories WHERE grant_id=? ORDER BY region_code",
                (grant_id,),
            ).fetchall()
        ]
        return result

    def _grant_row(self, grant_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
        if row is None:
            raise NotFound("授权不存在")
        return row

    def _change_grant_state(self, actor_id: str, grant_id: str, new_state: str, event: str) -> dict[str, Any]:
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE grants SET state=? WHERE grant_id=? AND state='active'",
                (new_state, grant_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("授权不是生效状态，无法变更")
            self._audit("grant", grant_id, event, actor_id, {})
        return self.grant(grant_id)

    def suspend_grant(self, actor_id: str, grant_id: str) -> dict[str, Any]:
        self._require(actor_id, "grant.write")
        self._grant_row(grant_id)
        return self._change_grant_state(actor_id, grant_id, "suspended", "grant.suspended")

    def terminate_grant(self, actor_id: str, grant_id: str) -> dict[str, Any]:
        self._require(actor_id, "grant.write")
        self._grant_row(grant_id)
        return self._change_grant_state(actor_id, grant_id, "terminated", "grant.terminated")

    def extend_grant(self, actor_id: str, grant_id: str, new_end_date: str) -> dict[str, Any]:
        """延长旧授权。续约谈判期间明确阻止。"""
        self._require(actor_id, "grant.write")
        grant = self._grant_row(grant_id)
        if new_end_date <= grant["end_date"]:
            raise ValidationFailed("新到期日必须晚于当前到期日")
        open_renewal = self.connection.execute(
            "SELECT renewal_id FROM renewals WHERE achievement_id=? AND licensee_id=? AND state='negotiating'",
            (grant["achievement_id"], grant["licensee_id"]),
        ).fetchone()
        if open_renewal is not None:
            raise InvalidState("续约谈判期间不得擅自延长旧许可")
        candidate_shape = GrantShape(
            grant_id=grant["grant_id"],
            agreement_id=grant["agreement_id"],
            licensee_id=grant["licensee_id"],
            exclusive=grant["grant_kind"] in {"exclusive", "sole"},
            start_date=grant["start_date"],
            end_date=new_end_date,
            territories=frozenset(
                item["region_code"]
                for item in self.connection.execute(
                    "SELECT region_code FROM grant_territories WHERE grant_id=?", (grant_id,)
                ).fetchall()
            ),
        )
        with transaction(self.connection, immediate=True):
            others = [
                self._grant_shape(row)
                for row in self.connection.execute(
                    "SELECT * FROM grants WHERE achievement_id=? AND state='active' AND grant_id<>?",
                    (grant["achievement_id"], grant_id),
                ).fetchall()
            ]
            conflicting = find_conflict(candidate_shape, others)
            if conflicting is not None:
                raise Conflict(f"延期后与授权 {conflicting.grant_id} 的授权范围相交")
            agreement = self._agreement_row(grant["agreement_id"])
            if new_end_date > agreement["expiry_date"]:
                raise ValidationFailed("延期后超出协议生效区间")
            self.connection.execute(
                "UPDATE grants SET end_date=? WHERE grant_id=?", (new_end_date, grant_id)
            )
            self._audit("grant", grant_id, "grant.extended", actor_id, {"new_end_date": new_end_date})
        return self.grant(grant_id)

    # ----- 续约 -----

    def open_renewal(self, actor_id: str, grant_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "renewal.write")
        grant = self._grant_row(grant_id)
        existing = self.connection.execute(
            "SELECT renewal_id FROM renewals WHERE grant_id=? AND state='negotiating'",
            (grant_id,),
        ).fetchone()
        if existing is not None:
            raise Conflict("该授权已有进行中的续约谈判")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO renewals(grant_id,achievement_id,licensee_id,note,opened_by,opened_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    grant_id,
                    grant["achievement_id"],
                    grant["licensee_id"],
                    note,
                    actor_id,
                    self._now(),
                ),
            )
            renewal_id = int(cursor.lastrowid)
            self._audit("renewal", str(renewal_id), "renewal.opened", actor_id, {"grant_id": grant_id})
        return {"renewal_id": renewal_id, "grant_id": grant_id, "state": "negotiating"}

    def sign_renewal(self, actor_id: str, renewal_id: int, new_grant_raw: Mapping[str, Any]) -> dict[str, Any]:
        """续约达成：在同一事务内按新条款签发新授权并闭环谈判。"""
        self._require(actor_id, "renewal.write")
        renewal = self.connection.execute(
            "SELECT * FROM renewals WHERE renewal_id=?", (renewal_id,)
        ).fetchone()
        if renewal is None:
            raise NotFound("续约记录不存在")
        if renewal["state"] != "negotiating":
            raise InvalidState("续约谈判已结束")
        grant = GrantInput.from_dict(new_grant_raw)
        if grant.licensee_id != renewal["licensee_id"]:
            raise ValidationFailed("新授权承接方必须与续约承接方一致")
        agreement = self._agreement_row(grant.agreement_id)
        if agreement["achievement_id"] != renewal["achievement_id"]:
            raise ValidationFailed("新授权必须针对同一成果")
        if agreement["state"] != "effective":
            raise InvalidState("新授权只能依据当前生效的协议版本签发")
        if grant.start_date < agreement["effective_date"] or grant.end_date > agreement["expiry_date"]:
            raise ValidationFailed("授权有效期超出协议生效区间")
        candidate = GrantShape(
            grant_id=grant.grant_id,
            agreement_id=grant.agreement_id,
            licensee_id=grant.licensee_id,
            exclusive=grant.exclusive,
            start_date=grant.start_date,
            end_date=grant.end_date,
            territories=frozenset(item.region_code for item in grant.territories),
        )
        with transaction(self.connection, immediate=True):
            existing_rows = self.connection.execute(
                "SELECT * FROM grants WHERE achievement_id=? AND state='active' AND grant_id<>?",
                (renewal["achievement_id"], renewal["grant_id"]),
            ).fetchall()
            conflicting = find_conflict(candidate, [self._grant_shape(row) for row in existing_rows])
            if conflicting is not None:
                raise Conflict(f"新授权与已签发授权 {conflicting.grant_id} 的授权范围相交")
            self.connection.execute(
                "INSERT INTO grants(grant_id,agreement_id,achievement_id,licensee_id,grant_kind,"
                "fields_of_use,start_date,end_date,royalty_type,rate,fixed_amount,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    grant.grant_id,
                    grant.agreement_id,
                    renewal["achievement_id"],
                    grant.licensee_id,
                    grant.grant_kind,
                    grant.fields_of_use,
                    grant.start_date,
                    grant.end_date,
                    grant.royalty.royalty_type,
                    None if grant.royalty.rate is None else decimal_text(grant.royalty.rate),
                    None if grant.royalty.fixed_amount is None else decimal_text(grant.royalty.fixed_amount),
                    actor_id,
                    self._now(),
                ),
            )
            self.connection.executemany(
                "INSERT INTO grant_territories(grant_id,region_code,region_name) VALUES(?,?,?)",
                [(grant.grant_id, item.region_code, item.region_name) for item in grant.territories],
            )
            # 旧授权在新授权起始日交接：到期终止，避免新旧并行
            self.connection.execute(
                "UPDATE grants SET state='terminated' WHERE grant_id=? AND state='active'",
                (renewal["grant_id"],),
            )
            self.connection.execute(
                "UPDATE renewals SET state='signed',new_grant_id=?,concluded_at=? WHERE renewal_id=?",
                (grant.grant_id, self._now(), renewal_id),
            )
            self._audit(
                "renewal",
                str(renewal_id),
                "renewal.signed",
                actor_id,
                {"old_grant_id": renewal["grant_id"], "new_grant_id": grant.grant_id},
            )
        return {
            "renewal_id": renewal_id,
            "state": "signed",
            "old_grant_id": renewal["grant_id"],
            "new_grant": self.grant(grant.grant_id),
        }

    def abandon_renewal(self, actor_id: str, renewal_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "renewal.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE renewals SET state='abandoned',concluded_at=?,note=? "
                "WHERE renewal_id=? AND state='negotiating'",
                (self._now(), reason, renewal_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("续约谈判不存在或已结束")
            self._audit("renewal", str(renewal_id), "renewal.abandoned", actor_id, {"reason": reason})
        return {"renewal_id": renewal_id, "state": "abandoned"}

    # ----- 改进成果 -----

    def record_improvement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "improvement.write")
        improvement = ImprovementInput.from_dict(raw)
        self._get_achievement(improvement.parent_achievement_id)
        agreement = self._agreement_row(improvement.agreement_id)
        if agreement["achievement_id"] != improvement.parent_achievement_id:
            raise ValidationFailed("改进成果绑定的协议必须属于同一成果")
        if agreement["state"] not in {"effective", "superseded"}:
            raise InvalidState("改进成果只能绑定已生效过的协议版本")
        if improvement.contributor_licensee_id is not None:
            self._licensee_row(improvement.contributor_licensee_id)
        if improvement.derived_grant_id is not None:
            derived = self._grant_row(improvement.derived_grant_id)
            if derived["agreement_id"] != improvement.agreement_id:
                raise ValidationFailed("派生授权必须与改进成果绑定同一协议版本")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO improvements(improvement_id,parent_achievement_id,agreement_id,title,"
                    "ownership_rule,contributor_licensee_id,derived_grant_id,recorded_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        improvement.improvement_id,
                        improvement.parent_achievement_id,
                        improvement.agreement_id,
                        improvement.title,
                        improvement.ownership_rule,
                        improvement.contributor_licensee_id,
                        improvement.derived_grant_id,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit(
                    "improvement",
                    improvement.improvement_id,
                    "improvement.recorded",
                    actor_id,
                    {"agreement_id": improvement.agreement_id, "ownership_rule": improvement.ownership_rule},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("改进成果编号冲突") from exc
        return {
            "improvement_id": improvement.improvement_id,
            "parent_achievement_id": improvement.parent_achievement_id,
            "agreement_id": improvement.agreement_id,
            "ownership_rule": improvement.ownership_rule,
        }

    # ----- 收益分期 -----

    def _idempotency(self, scope: str, key: str, request_digest: str, response: dict[str, Any]) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM office_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同请求内容")
        return json.loads(stored["response_json"])

    def _write_allocation_snapshot(
        self, period_id: str, agreement_id: str, gross: Decimal, timestamp: str
    ) -> None:
        owners = self._owners(agreement_id)
        distribution = distribute_revenue(gross, owners)
        for line in distribution["lines"]:
            self.connection.execute(
                "INSERT INTO revenue_allocations(period_id,owner_id,owner_kind,share,basis,"
                "gross_amount,frozen_amount,settled_amount,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    period_id,
                    line["owner_id"],
                    line["owner_kind"],
                    line["share"],
                    line["basis"],
                    line["gross_amount"],
                    "0.00",
                    "0.00",
                    timestamp,
                ),
            )

    def estimate_revenue(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记一期暂估收益，分成快照同时落库。"""
        self._require(actor_id, "revenue.write")
        period_id = str(raw.get("period_id", "")).strip()
        grant_id = str(raw.get("grant_id", "")).strip()
        period_date = str(raw.get("period_date", "")).strip()
        key = str(raw.get("idempotency_key", "")).strip()
        if not period_id or not grant_id or not period_date or not key:
            raise ValidationFailed("period_id、grant_id、period_date、idempotency_key 均不能为空")
        from datetime import date

        try:
            date.fromisoformat(period_date)
        except ValueError as exc:
            raise ValidationFailed("period_date 必须是 YYYY-MM-DD 日期") from exc
        estimated = Decimal(str(raw.get("estimated_amount")))
        if estimated < 0:
            raise ValidationFailed("estimated_amount 不能为负数")
        grant = self._grant_row(grant_id)
        if not (grant["start_date"] <= period_date <= grant["end_date"]):
            raise ValidationFailed("收益期不在授权有效期内")
        request_digest = digest(raw)
        cached = self._idempotency("revenue.estimate", key, request_digest, {})
        if cached is not None:
            return cached
        gross = quantize_money(estimated)
        response = {
            "period_id": period_id,
            "grant_id": grant_id,
            "agreement_id": grant["agreement_id"],
            "status": "estimated",
            "estimated_amount": decimal_text(gross),
            "gross_amount": decimal_text(gross),
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO revenue_periods(period_id,grant_id,agreement_id,achievement_id,licensee_id,"
                    "period_date,estimated_amount,gross_amount,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        period_id,
                        grant_id,
                        grant["agreement_id"],
                        grant["achievement_id"],
                        grant["licensee_id"],
                        period_date,
                        decimal_text(gross),
                        decimal_text(gross),
                        key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._write_allocation_snapshot(period_id, grant["agreement_id"], gross, self._now())
                # 授权级未决争议对新登记收益期同样立即生效
                self._refresh_period_freezes(period_id)
                self.connection.execute(
                    "INSERT INTO office_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
                    "VALUES('revenue.estimate',?,?,?,?)",
                    (key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("revenue_period", period_id, "revenue.estimated", actor_id, {"gross": decimal_text(gross)})
        except sqlite3.IntegrityError as exc:
            raise Conflict("收益期编号、授权+期次或幂等键冲突") from exc
        return response

    def _period_row(self, period_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM revenue_periods WHERE period_id=?", (period_id,)
        ).fetchone()
        if row is None:
            raise NotFound("收益期不存在")
        return row

    def _refresh_period_freezes(self, period_id: str) -> None:
        """依据当前所有未决争议重算某收益期的冻结份额。

        只冻结 gross - settled 的部分：已结算金额任何情况下都不被回冲。
        """
        period = self._period_row(period_id)
        if period["status"] == "settled":
            return
        lines = self.connection.execute(
            "SELECT * FROM revenue_allocations WHERE period_id=?", (period_id,)
        ).fetchall()
        disputes = self.connection.execute(
            "SELECT d.dispute_id,s.owner_id FROM disputes d "
            "JOIN dispute_subjects s ON s.dispute_id=d.dispute_id "
            "WHERE d.state='open' AND ("
            "d.period_id=? OR (d.period_id IS NULL AND d.grant_id=?))",
            (period_id, period["grant_id"]),
        ).fetchall()
        covering: dict[str, int] = {}
        for row in disputes:
            previous = covering.get(row["owner_id"])
            if previous is None or row["dispute_id"] < previous:
                covering[row["owner_id"]] = row["dispute_id"]
        for line in lines:
            gross = Decimal(line["gross_amount"])
            settled = Decimal(line["settled_amount"])
            dispute_id = covering.get(line["owner_id"])
            frozen = quantize_money(gross - settled) if dispute_id is not None else Decimal("0.00")
            if frozen < 0:
                frozen = Decimal("0.00")
            self.connection.execute(
                "UPDATE revenue_allocations SET frozen_amount=?,dispute_id=?,updated_at=? "
                "WHERE allocation_id=?",
                (decimal_text(frozen), dispute_id, self._now(), line["allocation_id"]),
            )

    def assess_revenue(self, actor_id: str, period_id: str, assessed_amount: object) -> dict[str, Any]:
        """暂估 → 核定。已出现结算金额的收益期不得再核定（已结算不倒改）。"""
        self._require(actor_id, "revenue.write")
        amount = Decimal(str(assessed_amount))
        if amount < 0:
            raise ValidationFailed("assessed_amount 不能为负数")
        gross = quantize_money(amount)
        with transaction(self.connection, immediate=True):
            period = self._period_row(period_id)
            if period["status"] == "settled":
                raise InvalidState("收益期已结算，不能因新协议或新核定倒改")
            settled_any = self.connection.execute(
                "SELECT COALESCE(SUM(CAST(settled_amount AS REAL)),0) total FROM revenue_allocations WHERE period_id=?",
                (period_id,),
            ).fetchone()["total"]
            if Decimal(str(settled_any)) > 0:
                raise InvalidState("收益期存在已结算份额，不得重新核定")
            self.connection.execute(
                "DELETE FROM revenue_allocations WHERE period_id=?", (period_id,)
            )
            self._write_allocation_snapshot(period_id, period["agreement_id"], gross, self._now())
            self.connection.execute(
                "UPDATE revenue_periods SET assessed_amount=?,gross_amount=?,status='assessed',"
                "assessed_by=?,assessed_at=? WHERE period_id=?",
                (decimal_text(gross), decimal_text(gross), actor_id, self._now(), period_id),
            )
            self._refresh_period_freezes(period_id)
            self._audit("revenue_period", period_id, "revenue.assessed", actor_id, {"gross": decimal_text(gross)})
        return self.period(period_id)

    def settle_revenue(self, actor_id: str, period_id: str) -> dict[str, Any]:
        """结算未冻结份额；仍有冻结的收益期保持核定状态，剩余部分解冻后再结。"""
        self._require(actor_id, "revenue.write")
        with transaction(self.connection, immediate=True):
            period = self._period_row(period_id)
            if period["status"] not in {"assessed", "settled"}:
                raise InvalidState("只有已核定收益期可以结算")
            lines = self.connection.execute(
                "SELECT * FROM revenue_allocations WHERE period_id=?", (period_id,)
            ).fetchall()
            all_clear = True
            for line in lines:
                gross = Decimal(line["gross_amount"])
                settled = Decimal(line["settled_amount"])
                frozen = Decimal(line["frozen_amount"])
                payable = quantize_money(gross - settled - frozen)
                if frozen > 0:
                    all_clear = False
                if payable > 0:
                    self.connection.execute(
                        "UPDATE revenue_allocations SET settled_amount=?,updated_at=? WHERE allocation_id=?",
                        (decimal_text(settled + payable), self._now(), line["allocation_id"]),
                    )
            new_status = "settled" if all_clear else "assessed"
            self.connection.execute(
                "UPDATE revenue_periods SET status=? WHERE period_id=?", (new_status, period_id)
            )
            self._audit(
                "revenue_period",
                period_id,
                "revenue.settled",
                actor_id,
                {"fully_settled": new_status == "settled"},
            )
        return self.period(period_id)

    def period(self, period_id: str) -> dict[str, Any]:
        period = self._period_row(period_id)
        result = dict(period)
        result["allocations"] = [
            dict(item)
            for item in self.connection.execute(
                "SELECT owner_id,owner_kind,share,basis,gross_amount,frozen_amount,settled_amount,dispute_id "
                "FROM revenue_allocations WHERE period_id=? ORDER BY owner_id",
                (period_id,),
            ).fetchall()
        ]
        totals = self.connection.execute(
            "SELECT COALESCE(SUM(CAST(gross_amount AS REAL)),0) gross,"
            "COALESCE(SUM(CAST(frozen_amount AS REAL)),0) frozen,"
            "COALESCE(SUM(CAST(settled_amount AS REAL)),0) settled "
            "FROM revenue_allocations WHERE period_id=?",
            (period_id,),
        ).fetchone()
        result["totals"] = {
            "gross_amount": f"{Decimal(str(totals['gross'])):.2f}",
            "frozen_amount": f"{Decimal(str(totals['frozen'])):.2f}",
            "settled_amount": f"{Decimal(str(totals['settled'])):.2f}",
        }
        return result

    # ----- 争议与冻结 -----

    def open_dispute(
        self,
        actor_id: str,
        *,
        reason: str,
        subject_owner_ids: Sequence[str],
        period_id: str | None = None,
        grant_id: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "dispute.write")
        if not reason.strip():
            raise ValidationFailed("争议原因不能为空")
        subjects = [item.strip() for item in subject_owner_ids if isinstance(item, str) and item.strip()]
        if not subjects:
            raise ValidationFailed("争议必须至少涉及一个权属主体")
        if len(set(subjects)) != len(subjects):
            raise ValidationFailed("争议主体不能重复")
        if period_id is None and grant_id is None:
            raise ValidationFailed("争议必须关联到收益期或授权")
        affected: list[sqlite3.Row] = []
        if period_id is not None:
            affected = [self._period_row(period_id)]
            grant_id = affected[0]["grant_id"]
        else:
            self._grant_row(grant_id)  # type: ignore[arg-type]
            affected = self.connection.execute(
                "SELECT * FROM revenue_periods WHERE grant_id=? AND status<>'settled'",
                (grant_id,),
            ).fetchall()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO disputes(period_id,grant_id,reason,opened_by,opened_at) VALUES(?,?,?,?,?)",
                (period_id, grant_id, reason.strip(), actor_id, self._now()),
            )
            dispute_id = int(cursor.lastrowid)
            self.connection.executemany(
                "INSERT INTO dispute_subjects(dispute_id,owner_id) VALUES(?,?)",
                [(dispute_id, owner) for owner in subjects],
            )
            frozen_periods = sorted({row["period_id"] for row in affected})
            for frozen_period in frozen_periods:
                self._refresh_period_freezes(frozen_period)
            self._audit(
                "dispute",
                str(dispute_id),
                "dispute.opened",
                actor_id,
                {"subjects": subjects, "period_id": period_id, "grant_id": grant_id},
            )
        return {
            "dispute_id": dispute_id,
            "state": "open",
            "subjects": subjects,
            "frozen_periods": frozen_periods,
        }

    def resolve_dispute(self, actor_id: str, dispute_id: int, resolution: str) -> dict[str, Any]:
        self._require(actor_id, "dispute.write")
        if not resolution.strip():
            raise ValidationFailed("处理结论不能为空")
        with transaction(self.connection, immediate=True):
            dispute = self.connection.execute(
                "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if dispute is None:
                raise NotFound("争议不存在")
            if dispute["state"] != "open":
                raise InvalidState("争议已处理")
            affected = self.connection.execute(
                "SELECT DISTINCT period_id FROM revenue_periods rp WHERE "
                "(rp.period_id=? OR (?) AND rp.grant_id=?) AND rp.status<>'settled'",
                (dispute["period_id"], dispute["period_id"] is None, dispute["grant_id"]),
            ).fetchall()
            self.connection.execute(
                "UPDATE disputes SET state='resolved',resolved_at=?,resolution=? WHERE dispute_id=?",
                (self._now(), resolution.strip(), dispute_id),
            )
            for row in affected:
                self._refresh_period_freezes(row["period_id"])
            self._audit("dispute", str(dispute_id), "dispute.resolved", actor_id, {"resolution": resolution.strip()})
        return {"dispute_id": dispute_id, "state": "resolved"}

    # ----- 许可链重建与收入解释 -----

    def license_chain(
        self, actor_id: str, *, achievement_id: str | None = None, licensee_id: str | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "chain.read")
        if achievement_id is None and licensee_id is None:
            raise ValidationFailed("必须按成果或承接方查询许可链")
        grant_query = (
            "SELECT * FROM grants WHERE achievement_id=? ORDER BY start_date,grant_id"
            if achievement_id is not None
            else "SELECT * FROM grants WHERE licensee_id=? ORDER BY start_date,grant_id"
        )
        key = achievement_id if achievement_id is not None else licensee_id
        grants = [dict(row) for row in self.connection.execute(grant_query, (key,)).fetchall()]
        agreement_ids = sorted({row["agreement_id"] for row in grants})
        agreements = [self.agreement(agreement_id) for agreement_id in agreement_ids]
        achievement_ids = sorted({row["achievement_id"] for row in grants})
        improvements = []
        if achievement_id is not None:
            improvements = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM improvements WHERE parent_achievement_id=? ORDER BY created_at,improvement_id",
                    (achievement_id,),
                ).fetchall()
            ]
        else:
            improvements = [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM improvements WHERE parent_achievement_id IN ("
                    + ",".join("?" * len(achievement_ids)) + ") ORDER BY created_at,improvement_id",
                    achievement_ids,
                ).fetchall()
            ] if achievement_ids else []
        renewals = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM renewals WHERE "
                + ("achievement_id=?" if achievement_id is not None else "licensee_id=?")
                + " ORDER BY opened_at",
                (key,),
            ).fetchall()
        ]
        for grant in grants:
            grant["territories"] = [
                row["region_code"]
                for row in self.connection.execute(
                    "SELECT region_code FROM grant_territories WHERE grant_id=? ORDER BY region_code",
                    (grant["grant_id"],),
                ).fetchall()
            ]
            grant["periods"] = self._period_summary(grant_id=grant["grant_id"])
        return {
            "scope": "achievement" if achievement_id is not None else "licensee",
            "scope_id": key,
            "agreement_versions": agreements,
            "grants": grants,
            "improvements": improvements,
            "renewals": renewals,
        }

    def _period_summary(self, *, grant_id: str | None = None, achievement_id: str | None = None) -> dict[str, Any]:
        where = "p.grant_id=?" if grant_id is not None else "p.achievement_id=?"
        value = grant_id if grant_id is not None else achievement_id
        rows = self.connection.execute(
            "SELECT p.status status,COUNT(DISTINCT p.period_id) count,"
            "COALESCE(SUM(CAST(a.gross_amount AS REAL)),0) gross,"
            "COALESCE(SUM(CAST(a.frozen_amount AS REAL)),0) frozen,"
            "COALESCE(SUM(CAST(a.settled_amount AS REAL)),0) settled "
            "FROM revenue_periods p LEFT JOIN revenue_allocations a ON a.period_id=p.period_id "
            f"WHERE {where} GROUP BY p.status",
            (value,),
        ).fetchall()
        return {
            row["status"]: {
                "count": row["count"],
                "gross_amount": f"{Decimal(str(row['gross'])):.2f}",
                "frozen_amount": f"{Decimal(str(row['frozen'])):.2f}",
                "settled_amount": f"{Decimal(str(row['settled'])):.2f}",
            }
            for row in rows
        }

    def explain_revenue(self, actor_id: str, period_id: str) -> dict[str, Any]:
        """解释每笔（每期）收入的来龙去脉和每一分钱的分配依据。"""
        self._require(actor_id, "explain.read")
        period = self._period_row(period_id)
        grant = self._grant_row(period["grant_id"])
        agreement = self.agreement(period["agreement_id"])
        owners = self._owners(period["agreement_id"])
        lines = [
            dict(row)
            for row in self.connection.execute(
                "SELECT owner_id,owner_kind,share,basis,gross_amount,frozen_amount,settled_amount,dispute_id "
                "FROM revenue_allocations WHERE period_id=? ORDER BY owner_id",
                (period_id,),
            ).fetchall()
        ]
        disputes = [
            {
                "dispute_id": row["dispute_id"],
                "state": row["state"],
                "reason": row["reason"],
                "subjects": [
                    item["owner_id"]
                    for item in self.connection.execute(
                        "SELECT owner_id FROM dispute_subjects WHERE dispute_id=? ORDER BY owner_id",
                        (row["dispute_id"],),
                    ).fetchall()
                ],
            }
            for row in self.connection.execute(
                "SELECT * FROM disputes WHERE period_id=? OR (period_id IS NULL AND grant_id=?) "
                "ORDER BY dispute_id",
                (period_id, period["grant_id"]),
            ).fetchall()
        ]
        reasons: list[str] = []
        reasons.append(
            f"收益期 {period_id} 属于授权 {grant['grant_id']}（{grant['grant_kind']}），"
            f"承接方 {period['licensee_id']}，授权地域与期限以该授权记录为准。"
        )
        reasons.append(
            f"授权依据协议 {agreement['agreement_id']}（版本 {agreement['version']}，"
            f"{agreement['effective_date']} 至 {agreement['expiry_date']} 生效）；"
            f"分成比例使用该协议版本的权属依据，后续新协议不回溯改写本期。"
        )
        if period["status"] == "estimated":
            reasons.append("本期当前为暂估金额，核定后分成明细会按核定金额重算，结算前不产生已结算金额。")
        elif period["status"] == "assessed":
            reasons.append("本期已核定；未冻结份额可结算，冻结份额待争议处理后再结算。")
        else:
            reasons.append("本期已结算，金额与分成快照封存，不再因任何新协议变化。")
        if any(Decimal(line["frozen_amount"]) > 0 for line in lines):
            frozen_owners = [line["owner_id"] for line in lines if Decimal(line["frozen_amount"]) > 0]
            reasons.append(f"争议仅冻结相关主体份额：{', '.join(frozen_owners)}；其他主体份额正常结算。")
        return {
            "period": dict(period),
            "grant": {
                "grant_id": grant["grant_id"],
                "grant_kind": grant["grant_kind"],
                "licensee_id": grant["licensee_id"],
                "start_date": grant["start_date"],
                "end_date": grant["end_date"],
                "royalty_type": grant["royalty_type"],
                "rate": grant["rate"],
                "fixed_amount": grant["fixed_amount"],
                "territories": [
                    row["region_code"]
                    for row in self.connection.execute(
                        "SELECT region_code FROM grant_territories WHERE grant_id=?",
                        (period["grant_id"],),
                    ).fetchall()
                ],
            },
            "agreement": {
                "agreement_id": agreement["agreement_id"],
                "achievement_id": agreement["achievement_id"],
                "version": agreement["version"],
                "effective_date": agreement["effective_date"],
                "expiry_date": agreement["expiry_date"],
                "state": agreement["state"],
            },
            "ownership_basis": agreement["ownership"],
            "allocations": lines,
            "disputes": disputes,
            "reasons": reasons,
        }

    # ----- 审计 -----

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM office_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = digest(body)
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
