"""成果、协议版本、许可签发、改进归属与分期收益的事务用例。

关键不变量：

1. 许可、改进、收益都通过 ``(agreement_id, version_no)`` 外键绑定到签发或
   发生时生效的协议版本；协议新版本生效只终结版本状态，从不改写旧版本条款；
2. 签发时把授权范围物化到 ``license_grants``，与全部在授许可网格做
   时间×地域×领域相交检测，冲突即拒；续约必须显式更替旧许可，
   谈判进行中的旧许可不得延期；
3. 每期收益先暂估后核定，争议只冻结相关收款份额，已结算金额在后续核定中
   作为锁定量从可分配池中扣除，任何新协议都无法回写。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import (
    AchievementRegistration,
    AgreementDraft,
    ImprovementFiling,
    LicenseIssue,
    OwnershipTerm,
    RevenueEstimate,
)
from .rules import (
    HUNDRED,
    ZERO,
    LicenseGrant,
    allocate_pool,
    allocate_revenue,
    canonical_json,
    decimal_text,
    digest,
    effective_grants,
    find_grant_conflicts,
    quantize_money,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "manager": {
        "achievement.write",
        "agreement.draft",
        "license.write",
        "renewal.write",
        "improvement.write",
        "chain.read",
        "revenue.read",
    },
    "legal": {
        "agreement.ratify",
        "license.write",
        "renewal.write",
        "chain.read",
        "revenue.read",
    },
    "finance": {
        "revenue.write",
        "dispute.write",
        "chain.read",
        "revenue.read",
    },
    "auditor": {"audit.read", "chain.read", "revenue.read"},
}


def _json_default(value: Decimal) -> str:
    return decimal_text(value)


class LicensingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM lic_users WHERE user_id=?", (user_id,)
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
            "SELECT event_hash FROM lic_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": _jsonable(payload),
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO lic_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(_jsonable(payload)),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotent(self, scope: str, key: str, raw: Mapping[str, Any]) -> dict[str, Any] | None:
        request_digest = digest(raw)
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM lic_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同的请求内容")
        return json.loads(stored["response_json"])

    def _save_idempotent(self, scope: str, key: str, raw: Mapping[str, Any], response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO lic_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(raw), canonical_json(_jsonable(response)), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO lic_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------ 成果

    def register_achievement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "achievement.write")
        achievement = AchievementRegistration.from_dict(raw)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO achievements(achievement_id,title,field_kind,completed_on,basis_note,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        achievement.achievement_id,
                        achievement.title,
                        achievement.field_kind,
                        achievement.completed_on,
                        achievement.basis_note,
                        actor_id,
                        self._now(),
                    ),
                )
                self._audit("achievement", achievement.achievement_id, "achievement.registered", actor_id, {
                    "title": achievement.title, "field_kind": achievement.field_kind,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("成果编号已经存在") from exc
        return {"achievement_id": achievement.achievement_id, "state": "registered"}

    # -------------------------------------------------------------- 协议版本

    def draft_agreement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "agreement.draft")
        draft = AgreementDraft.from_dict(raw)
        ownership = OwnershipTerm.from_dict(raw).entries
        achievement = self.connection.execute(
            "SELECT achievement_id FROM achievements WHERE achievement_id=?",
            (draft.achievement_id,),
        ).fetchone()
        if achievement is None:
            raise NotFound("成果不存在")
        previous = self.connection.execute(
            "SELECT max(version_no) version_no FROM agreements WHERE agreement_id=?",
            (draft.agreement_id,),
        ).fetchone()
        version_no = 1 if previous["version_no"] is None else int(previous["version_no"]) + 1
        if version_no > 1:
            prior = self.connection.execute(
                "SELECT achievement_id FROM agreements WHERE agreement_id=? AND version_no=?",
                (draft.agreement_id, version_no - 1),
            ).fetchone()
            if prior is None or prior["achievement_id"] != draft.achievement_id:
                raise Conflict("协议新版本必须沿用同一成果")
        content = {
            "draft": _jsonable(draft.terms),
            "ownership": _jsonable(ownership),
        }
        content_sha256 = hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO agreements(agreement_id,version_no,achievement_id,version_label,state,"
                    "effective_on,expires_on,basis_summary,terms_json,content_sha256,supersedes_version_no,"
                    "created_by,created_at) VALUES(?,?,?,?,'draft',?,?,?,?,?,?,?,?)",
                    (
                        draft.agreement_id,
                        version_no,
                        draft.achievement_id,
                        draft.version_label,
                        draft.effective_on,
                        draft.expires_on,
                        draft.basis_summary,
                        canonical_json(_jsonable(draft.terms)),
                        content_sha256,
                        None if version_no == 1 else version_no - 1,
                        actor_id,
                        self._now(),
                    ),
                )
                for position, entry in enumerate(ownership):
                    self.connection.execute(
                        "INSERT INTO ownership_terms(agreement_id,version_no,position,owner_id,"
                        "share_percent,kind,note) VALUES(?,?,?,?,?,?,?)",
                        (
                            draft.agreement_id,
                            version_no,
                            position,
                            entry["owner_id"],
                            decimal_text(entry["share_percent"]),
                            entry["kind"],
                            entry["note"],
                        ),
                    )
                self._audit("agreement", f"{draft.agreement_id}:v{version_no}", "agreement.drafted", actor_id, {
                    "agreement_id": draft.agreement_id,
                    "version_no": version_no,
                    "achievement_id": draft.achievement_id,
                    "ownership": ownership,
                    "sha256": content_sha256,
                })
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议版本冲突或成果不存在") from exc
        return {
            "agreement_id": draft.agreement_id,
            "version_no": version_no,
            "state": "draft",
            "sha256": content_sha256,
        }

    def _get_version(self, agreement_id: str, version_no: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM agreements WHERE agreement_id=? AND version_no=?",
            (agreement_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        return row

    def _ownership(self, agreement_id: str, version_no: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT owner_id,share_percent,kind,note FROM ownership_terms "
            "WHERE agreement_id=? AND version_no=? ORDER BY position",
            (agreement_id, version_no),
        ).fetchall()
        return [
            {
                "owner_id": row["owner_id"],
                "share_percent": row["share_percent"],
                "kind": row["kind"],
                "note": row["note"],
            }
            for row in rows
        ]

    def ratify_agreement(self, actor_id: str, agreement_id: str, version_no: int) -> dict[str, Any]:
        self._require(actor_id, "agreement.ratify")
        with transaction(self.connection, immediate=True):
            version = self._get_version(agreement_id, version_no)
            if version["state"] != "draft":
                raise InvalidState("只有草案协议可以生效")
            self.connection.execute(
                "UPDATE agreements SET state='effective',ratified_at=? "
                "WHERE agreement_id=? AND version_no=? AND state='draft'",
                (self._now(), agreement_id, version_no),
            )
            superseded: list[int] = []
            prior_rows = self.connection.execute(
                "SELECT version_no FROM agreements WHERE agreement_id=? AND version_no<>? AND state='effective'",
                (agreement_id, version_no),
            ).fetchall()
            for prior in prior_rows:
                self.connection.execute(
                    "UPDATE agreements SET state='superseded' WHERE agreement_id=? AND version_no=?",
                    (agreement_id, prior["version_no"]),
                )
                superseded.append(prior["version_no"])
            self._audit("agreement", f"{agreement_id}:v{version_no}", "agreement.ratified", actor_id, {
                "agreement_id": agreement_id,
                "version_no": version_no,
                "superseded_versions": superseded,
            })
        return {"agreement_id": agreement_id, "version_no": version_no, "state": "effective"}

    def agreement_snapshot(self, agreement_id: str, version_no: int) -> dict[str, Any]:
        version = self._get_version(agreement_id, version_no)
        return {
            "agreement_id": agreement_id,
            "version_no": version_no,
            "achievement_id": version["achievement_id"],
            "version_label": version["version_label"],
            "state": version["state"],
            "effective_on": version["effective_on"],
            "expires_on": version["expires_on"],
            "basis_summary": version["basis_summary"],
            "terms": json.loads(version["terms_json"]),
            "ownership": self._ownership(agreement_id, version_no),
            "content_sha256": version["content_sha256"],
            "ratified_at": version["ratified_at"],
        }

    # ----------------------------------------------------------- 活跃授权网格

    def _active_grants(self, achievement_id: str, exclude_license: str | None = None) -> list[LicenseGrant]:
        sql = (
            "SELECT g.* FROM license_grants g JOIN licenses l ON l.license_id=g.license_id "
            "WHERE l.achievement_id=? AND l.state='issued'"
        )
        params: list[Any] = [achievement_id]
        if exclude_license is not None:
            sql += " AND g.license_id<>?"
            params.append(exclude_license)
        sql += " ORDER BY g.license_id"
        rows = self.connection.execute(sql, params).fetchall()
        return [
            LicenseGrant(
                license_id=row["license_id"],
                licensee_id=row["licensee_id"],
                license_type=row["license_type"],
                region_code=row["region_code"],
                field_scope=row["field_scope"],
                valid_from=row["valid_from"],
                valid_until=row["valid_until"],
            )
            for row in rows
        ]

    def _check_conflicts(self, achievement_id: str, candidate: Sequence[LicenseGrant], *, exclude: str | None) -> None:
        existing = self._active_grants(achievement_id, exclude)
        conflicts = find_grant_conflicts(candidate, existing)
        if conflicts:
            raise Conflict(
                "授权范围与在授许可相交，阻止签发",
                [
                    {
                        "existing_license_id": item.existing_license_id,
                        "existing_licensee_id": item.existing_licensee_id,
                        "existing_type": item.existing_type,
                        "region_code": item.region_code,
                        "field_scope": item.field_scope,
                        "reason": item.reason,
                    }
                    for item in conflicts
                ],
            )

    def _persist_license(
        self,
        request: LicenseIssue,
        version: sqlite3.Row,
        *,
        renewal_of: str | None,
        actor_id: str,
    ) -> str:
        grants = effective_grants(
            request.license_id,
            request.licensee_id,
            request.license_type,
            request.regions,
            request.field_scope_globally,
            request.valid_from,
            request.valid_until,
        )
        if not grants:
            raise ValidationFailed("许可的全局限定与地域限定互不相交，没有可签发的授权范围")
        self._check_conflicts(version["achievement_id"], grants, exclude=request.license_id)
        cursor = self.connection.execute(
            "INSERT INTO licenses(license_id,agreement_id,version_no,achievement_id,licensee_id,"
            "license_type,field_scope,valid_from,valid_until,improvement_policy,state,"
            "renewal_of_license_id,idempotency_key,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'issued',?,?,?,?)",
            (
                request.license_id,
                request.agreement_id,
                version["version_no"],
                version["achievement_id"],
                request.licensee_id,
                request.license_type,
                request.field_scope_globally,
                request.valid_from,
                request.valid_until,
                request.improvement_policy,
                renewal_of,
                request.idempotency_key,
                actor_id,
                self._now(),
            ),
        )
        for position, region in enumerate(request.regions):
            self.connection.execute(
                "INSERT INTO license_regions(license_id,position,region_code,field_scope) VALUES(?,?,?,?)",
                (request.license_id, position, region["region_code"], region["field_scope"]),
            )
        for position, line in enumerate(request.revenue_split):
            self.connection.execute(
                "INSERT INTO license_revenue_split(license_id,position,payee_id,share_percent,source) "
                "VALUES(?,?,?,?,?)",
                (
                    request.license_id,
                    position,
                    line["payee_id"],
                    decimal_text(line["share_percent"]),
                    line["source"],
                ),
            )
        for grant in grants:
            self.connection.execute(
                "INSERT INTO license_grants(license_id,licensee_id,license_type,region_code,field_scope,"
                "valid_from,valid_until) VALUES(?,?,?,?,?,?,?)",
                (
                    grant.license_id,
                    grant.licensee_id,
                    grant.license_type,
                    grant.region_code,
                    grant.field_scope,
                    grant.valid_from,
                    grant.valid_until,
                ),
            )
        return str(cursor.lastrowid or request.license_id)

    # ---------------------------------------------------------------- 许可

    def issue_license(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "license.write")
        agreement_id = str(raw.get("agreement_id", "")).strip()
        version_no = int(raw.get("version_no")) if raw.get("version_no") is not None else 0
        if not agreement_id or version_no <= 0:
            raise ValidationFailed("必须指定生效的 agreement_id 与 version_no")
        cached = self._idempotent("license.issue", str(raw.get("idempotency_key", "")), raw)
        if cached is not None:
            return cached
        with transaction(self.connection, immediate=True):
            version = self._get_version(agreement_id, version_no)
            if version["state"] != "effective":
                raise InvalidState("只能依据已生效的协议版本签发许可")
            request = LicenseIssue.from_dict(raw, self._ownership(agreement_id, version_no))
            self._validate_license_window(request, version)
            self._persist_license(request, version, renewal_of=None, actor_id=actor_id)
            response = {
                "license_id": request.license_id,
                "agreement_id": agreement_id,
                "version_no": version_no,
                "state": "issued",
            }
            self._save_idempotent("license.issue", request.idempotency_key, raw, response)
            self._audit("license", request.license_id, "license.issued", actor_id, {
                "agreement_id": agreement_id,
                "version_no": version_no,
                "licensee_id": request.licensee_id,
                "license_type": request.license_type,
                "regions": request.regions,
                "valid_from": request.valid_from,
                "valid_until": request.valid_until,
                "improvement_policy": request.improvement_policy,
                "revenue_split": [
                    {"payee_id": line["payee_id"], "share_percent": decimal_text(line["share_percent"])}
                    for line in request.revenue_split
                ],
            })
        return response

    @staticmethod
    def _validate_license_window(request: LicenseIssue, version: sqlite3.Row) -> None:
        if request.valid_from < version["effective_on"]:
            raise InvalidState("许可生效日早于协议版本生效日")
        if version["expires_on"] is not None and (
            request.valid_from > version["expires_on"]
            or (request.valid_until is not None and request.valid_until > version["expires_on"])
        ):
            raise InvalidState("许可有效期超出协议版本有效期")

    def _get_license(self, license_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM licenses WHERE license_id=?", (license_id,)).fetchone()
        if row is None:
            raise NotFound("许可不存在")
        return row

    def license_snapshot(self, license_id: str) -> dict[str, Any]:
        row = self._get_license(license_id)
        regions = self.connection.execute(
            "SELECT region_code,field_scope FROM license_regions ORDER BY position"
        ).fetchall() if False else self.connection.execute(
            "SELECT region_code,field_scope FROM license_regions WHERE license_id=? ORDER BY position",
            (license_id,),
        ).fetchall()
        split = self.connection.execute(
            "SELECT payee_id,share_percent,source FROM license_revenue_split WHERE license_id=? ORDER BY position",
            (license_id,),
        ).fetchall()
        return {
            "license_id": license_id,
            "agreement_id": row["agreement_id"],
            "version_no": row["version_no"],
            "achievement_id": row["achievement_id"],
            "licensee_id": row["licensee_id"],
            "license_type": row["license_type"],
            "field_scope": row["field_scope"],
            "regions": [dict(item) for item in regions],
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "improvement_policy": row["improvement_policy"],
            "state": row["state"],
            "renewal_of_license_id": row["renewal_of_license_id"],
            "superseded_by_license_id": row["superseded_by_license_id"],
            "revenue_split": [
                {"payee_id": item["payee_id"], "share_percent": item["share_percent"], "source": item["source"]}
                for item in split
            ],
        }

    def terminate_license(self, actor_id: str, license_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "license.write")
        with transaction(self.connection, immediate=True):
            row = self._get_license(license_id)
            if row["state"] != "issued":
                raise InvalidState("只有在授许可可以终止")
            open_renewal = self.connection.execute(
                "SELECT negotiation_id FROM renewal_negotiations WHERE old_license_id=? AND state='negotiating'",
                (license_id,),
            ).fetchone()
            if open_renewal is not None:
                raise InvalidState("续约谈判进行中，必须先完成或放弃谈判，不能直接处置旧许可")
            self.connection.execute(
                "UPDATE licenses SET state='terminated' WHERE license_id=?",
                (license_id,),
            )
            self.connection.execute("DELETE FROM license_grants WHERE license_id=?", (license_id,))
            self._audit("license", license_id, "license.terminated", actor_id, {"reason": reason})
        return {"license_id": license_id, "state": "terminated"}

    # ---------------------------------------------------------------- 续约

    def open_renewal(self, actor_id: str, license_id: str, note: str = "") -> dict[str, Any]:
        self._require(actor_id, "renewal.write")
        with transaction(self.connection, immediate=True):
            row = self._get_license(license_id)
            if row["state"] != "issued":
                raise InvalidState("只有在授许可可以进入续约谈判")
            existing = self.connection.execute(
                "SELECT negotiation_id FROM renewal_negotiations WHERE old_license_id=? AND state='negotiating'",
                (license_id,),
            ).fetchone()
            if existing is not None:
                raise Conflict("该许可已有进行中的续约谈判")
            cursor = self.connection.execute(
                "INSERT INTO renewal_negotiations(old_license_id,note,opened_by,opened_at) VALUES(?,?,?,?)",
                (license_id, note, actor_id, self._now()),
            )
            negotiation_id = int(cursor.lastrowid)
            self._audit("renewal", str(negotiation_id), "renewal.opened", actor_id, {
                "old_license_id": license_id, "note": note,
            })
        return {"negotiation_id": negotiation_id, "old_license_id": license_id, "state": "negotiating"}

    def extend_license(self, actor_id: str, license_id: str, new_valid_until: str) -> dict[str, Any]:
        """显式延长旧许可；续约谈判进行中一律拒绝。"""
        self._require(actor_id, "license.write")
        from .models import date_text  # 局部导入避免循环依赖

        until = date_text(new_valid_until, "new_valid_until")
        with transaction(self.connection, immediate=True):
            row = self._get_license(license_id)
            if row["state"] != "issued":
                raise InvalidState("只有在授许可可以延期")
            open_renewal = self.connection.execute(
                "SELECT negotiation_id FROM renewal_negotiations WHERE old_license_id=? AND state='negotiating'",
                (license_id,),
            ).fetchone()
            if open_renewal is not None:
                raise InvalidState("续约谈判期间不得擅自延长旧许可")
            if row["valid_until"] is None or until <= row["valid_until"]:
                raise ValidationFailed("新到期日必须晚于当前到期日")
            version = self._get_version(row["agreement_id"], row["version_no"])
            if version["expires_on"] is not None and until > version["expires_on"]:
                raise InvalidState("延期超出协议版本有效期，应签订新协议版本后续约")
            # 用延期后的真实授权网格重跑冲突检测。
            extended = [
                LicenseGrant(
                    license_id=license_id,
                    licensee_id=region["licensee_id"],
                    license_type=region["license_type"],
                    region_code=region["region_code"],
                    field_scope=region["field_scope"],
                    valid_from=region["valid_from"],
                    valid_until=until,
                )
                for region in self.connection.execute(
                    "SELECT licensee_id,license_type,region_code,field_scope,valid_from "
                    "FROM license_grants WHERE license_id=?",
                    (license_id,),
                ).fetchall()
            ]
            candidate = self._active_grants(row["achievement_id"], exclude_license=license_id)
            from .rules import find_grant_conflicts as _find

            conflicts = _find(extended, candidate)
            if conflicts:
                raise Conflict("延期窗口与其他在授许可冲突", [
                    {"existing_license_id": item.existing_license_id, "reason": item.reason}
                    for item in conflicts
                ])
            self.connection.execute(
                "UPDATE licenses SET valid_until=? WHERE license_id=?",
                (until, license_id),
            )
            self.connection.execute(
                "UPDATE license_grants SET valid_until=? WHERE license_id=?",
                (until, license_id),
            )
            self._audit("license", license_id, "license.extended", actor_id, {
                "previous_valid_until": row["valid_until"], "new_valid_until": until,
            })
        return {"license_id": license_id, "valid_until": until, "state": "issued"}

    def complete_renewal(self, actor_id: str, negotiation_id: int, raw: Mapping[str, Any]) -> dict[str, Any]:
        """以一份新许可完成续约：旧许可即刻被更替并移出冲突网格。"""
        self._require(actor_id, "renewal.write")
        cached = self._idempotent("license.issue", str(raw.get("idempotency_key", "")), raw)
        if cached is not None:
            if cached.get("renewal_of"):
                return cached
            raise Conflict("该签发请求已作为普通许可处理，不能用于续约")
        with transaction(self.connection, immediate=True):
            negotiation = self.connection.execute(
                "SELECT * FROM renewal_negotiations WHERE negotiation_id=?",
                (negotiation_id,),
            ).fetchone()
            if negotiation is None:
                raise NotFound("续约谈判不存在")
            if negotiation["state"] != "negotiating":
                raise InvalidState("续约谈判已结束")
            old = self._get_license(negotiation["old_license_id"])
            if old["state"] != "issued":
                raise InvalidState("旧许可已不在授状态")
            agreement_id = str(raw.get("agreement_id", "")).strip()
            version_no = int(raw.get("version_no")) if raw.get("version_no") is not None else 0
            if not agreement_id or version_no <= 0:
                raise ValidationFailed("必须指定生效的 agreement_id 与 version_no")
            version = self._get_version(agreement_id, version_no)
            if version["state"] != "effective":
                raise InvalidState("只能依据已生效的协议版本续约")
            if version["achievement_id"] != old["achievement_id"]:
                raise Conflict("续约许可必须指向同一成果")
            request = LicenseIssue.from_dict(raw, self._ownership(agreement_id, version_no))
            if request.licensee_id != old["licensee_id"]:
                raise Conflict("续约承接方必须与旧许可一致")
            self._validate_license_window(request, version)
            # 先把旧许可移出冲突网格，再签发新许可，最后回写更替指针
            # （superseded_by 外键要求新许可已存在）。
            self.connection.execute("DELETE FROM license_grants WHERE license_id=?", (old["license_id"],))
            self._persist_license(request, version, renewal_of=old["license_id"], actor_id=actor_id)
            self.connection.execute(
                "UPDATE licenses SET state='superseded',superseded_by_license_id=? WHERE license_id=?",
                (request.license_id, old["license_id"]),
            )
            self.connection.execute(
                "UPDATE renewal_negotiations SET state='completed',new_license_id=?,closed_at=? "
                "WHERE negotiation_id=? AND state='negotiating'",
                (request.license_id, self._now(), negotiation_id),
            )
            response = {
                "license_id": request.license_id,
                "agreement_id": agreement_id,
                "version_no": version_no,
                "renewal_of": old["license_id"],
                "state": "issued",
            }
            self._save_idempotent("license.issue", request.idempotency_key, raw, response)
            self._audit("renewal", str(negotiation_id), "renewal.completed", actor_id, {
                "old_license_id": old["license_id"],
                "new_license_id": request.license_id,
                "agreement_id": agreement_id,
                "version_no": version_no,
            })
        return response

    def abandon_renewal(self, actor_id: str, negotiation_id: int, note: str = "") -> dict[str, Any]:
        self._require(actor_id, "renewal.write")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE renewal_negotiations SET state='abandoned',closed_at=? "
                "WHERE negotiation_id=? AND state='negotiating'",
                (self._now(), negotiation_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("谈判不存在或已结束")
            old = self.connection.execute(
                "SELECT old_license_id FROM renewal_negotiations WHERE negotiation_id=?",
                (negotiation_id,),
            ).fetchone()
            self._audit("renewal", str(negotiation_id), "renewal.abandoned", actor_id, {
                "old_license_id": old["old_license_id"], "note": note,
            })
        return {"negotiation_id": negotiation_id, "state": "abandoned"}

    # ---------------------------------------------------------------- 改进

    def file_improvement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "improvement.write")
        filing = ImprovementFiling.from_dict(raw)
        cached = self._idempotent("improvement.file", filing.idempotency_key, raw)
        if cached is not None:
            return cached
        with transaction(self.connection, immediate=True):
            parent = self._get_license(filing.parent_license_id)
            # 改进归属按其产生时所依据的协议版本与许可政策确定。
            resolved_ownership = filing.ownership
            if resolved_ownership == "case_by_case":
                resolved_ownership = parent["improvement_policy"]
                if resolved_ownership == "case_by_case":
                    raise InvalidState("协议未约定改进归属，必须在登记时明确 licensor/licensee/joint")
            owner_shares = filing.owner_shares
            if resolved_ownership == "joint" and not owner_shares:
                raise InvalidState("共有改进必须提供 owner_shares")
            self.connection.execute(
                "INSERT INTO improvements(improvement_id,achievement_id,parent_license_id,agreement_id,"
                "version_no,title,ownership,idempotency_key,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    filing.improvement_id,
                    parent["achievement_id"],
                    parent["license_id"],
                    parent["agreement_id"],
                    parent["version_no"],
                    filing.title,
                    resolved_ownership,
                    filing.idempotency_key,
                    actor_id,
                    self._now(),
                ),
            )
            for position, entry in enumerate(owner_shares):
                self.connection.execute(
                    "INSERT INTO improvement_owners(improvement_id,position,owner_id,share_percent) "
                    "VALUES(?,?,?,?)",
                    (
                        filing.improvement_id,
                        position,
                        entry["owner_id"],
                        decimal_text(entry["share_percent"]),
                    ),
                )
            response = {
                "improvement_id": filing.improvement_id,
                "parent_license_id": parent["license_id"],
                "agreement_id": parent["agreement_id"],
                "version_no": parent["version_no"],
                "ownership": resolved_ownership,
            }
            self._save_idempotent("improvement.file", filing.idempotency_key, raw, response)
            self._audit("improvement", filing.improvement_id, "improvement.filed", actor_id, response)
        return response

    # ---------------------------------------------------------------- 收益

    def _split_snapshot(self, license_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payee_id,share_percent,source FROM license_revenue_split WHERE license_id=? ORDER BY position",
            (license_id,),
        ).fetchall()
        if not rows:
            raise InvalidState("许可缺少分成方案")
        return [
            {"payee_id": row["payee_id"], "share_percent": Decimal(row["share_percent"]), "source": row["source"]}
            for row in rows
        ]

    def estimate_revenue(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "revenue.write")
        estimate = RevenueEstimate.from_dict(raw)
        cached = self._idempotent("revenue.estimate", estimate.idempotency_key, raw)
        if cached is not None:
            return cached
        with transaction(self.connection, immediate=True):
            license_row = self._get_license(estimate.license_id)
            if license_row["state"] != "issued":
                raise InvalidState("只能为在授许可登记收益")
            split = self._split_snapshot(estimate.license_id)
            lines = allocate_revenue(estimate.gross_amount, split)
            self.connection.execute(
                "INSERT INTO revenue_installments(installment_id,license_id,agreement_id,version_no,"
                "period_label,estimated_amount,gross_amount,currency,state,idempotency_key,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,'estimated',?,?,?)",
                (
                    estimate.installment_id,
                    estimate.license_id,
                    license_row["agreement_id"],
                    license_row["version_no"],
                    estimate.period_label,
                    decimal_text(quantize_money(estimate.gross_amount)),
                    decimal_text(quantize_money(estimate.gross_amount)),
                    estimate.currency,
                    estimate.idempotency_key,
                    actor_id,
                    self._now(),
                ),
            )
            for line in lines:
                basis = {
                    "stage": "estimated",
                    "formula": "gross_amount * share_percent / 100",
                    "license_id": estimate.license_id,
                    "agreement_id": license_row["agreement_id"],
                    "version_no": license_row["version_no"],
                    "period_label": estimate.period_label,
                    "share_percent": decimal_text(line["share_percent"]),
                    "gross_amount": decimal_text(quantize_money(estimate.gross_amount)),
                }
                self.connection.execute(
                    "INSERT INTO revenue_allocations(installment_id,payee_id,share_percent,"
                    "allocated_amount,status,source,basis_json) VALUES(?,?,?,?,'pending',?,?)",
                    (
                        estimate.installment_id,
                        line["payee_id"],
                        decimal_text(line["share_percent"]),
                        decimal_text(line["allocated_amount"]),
                        line["source"],
                        canonical_json(basis),
                    ),
                )
            response = {
                "installment_id": estimate.installment_id,
                "state": "estimated",
                "gross_amount": decimal_text(quantize_money(estimate.gross_amount)),
                "allocations": [
                    {"payee_id": line["payee_id"], "allocated_amount": decimal_text(line["allocated_amount"])}
                    for line in lines
                ],
            }
            self._save_idempotent("revenue.estimate", estimate.idempotency_key, raw, response)
            self._audit("installment", estimate.installment_id, "revenue.estimated", actor_id, {
                "license_id": estimate.license_id,
                "agreement_id": license_row["agreement_id"],
                "version_no": license_row["version_no"],
                "period_label": estimate.period_label,
                "gross_amount": decimal_text(quantize_money(estimate.gross_amount)),
                "currency": estimate.currency,
            })
        return response

    def _get_installment(self, installment_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM revenue_installments WHERE installment_id=?",
            (installment_id,),
        ).fetchone()
        if row is None:
            raise NotFound("收益期次不存在")
        return row

    def _allocations(self, installment_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM revenue_allocations WHERE installment_id=? ORDER BY allocation_id",
            (installment_id,),
        ))

    def assess_revenue(self, actor_id: str, installment_id: str, gross_amount: object, note: str = "") -> dict[str, Any]:
        """核定毛收入并复算分配。

        已结算行保持不动，剩余金额只在未结算行之间按原比例的相对值重分；
        冻结行随核定更新名义金额，但仍不可支付。
        """
        self._require(actor_id, "revenue.write")
        from .models import decimal_value

        gross = quantize_money(decimal_value(gross_amount, "gross_amount", minimum=ZERO))
        with transaction(self.connection, immediate=True):
            installment = self._get_installment(installment_id)
            if installment["state"] == "settled":
                raise InvalidState("期次已全部结算，不能再核定")
            allocations = self._allocations(installment_id)
            settled_lines = [line for line in allocations if line["status"] == "settled"]
            open_lines = [line for line in allocations if line["status"] != "settled"]
            settled_sum = sum((Decimal(line["settled_amount"] or line["allocated_amount"]) for line in settled_lines), ZERO)
            if gross < settled_sum:
                raise InvalidState(
                    f"核定金额 {decimal_text(gross)} 低于已结算金额 {decimal_text(settled_sum)}，"
                    "已结算部分不得倒改"
                )
            pool = quantize_money(gross - settled_sum)
            weights = [
                (line["payee_id"], Decimal(line["share_percent"]), line["source"])
                for line in open_lines
            ]
            reallocated = {
                item["payee_id"]: item["allocated_amount"]
                for item in allocate_pool(pool, weights)
            }
            frozen_payees: list[str] = []
            for line in open_lines:
                amount = quantize_money(reallocated[line["payee_id"]])
                basis = {
                    "stage": "assessed",
                    "formula": "(assessed_gross - settled_sum) * share / sum(unsettled_shares)",
                    "license_id": installment["license_id"],
                    "agreement_id": installment["agreement_id"],
                    "version_no": installment["version_no"],
                    "period_label": installment["period_label"],
                    "share_percent": line["share_percent"],
                    "assessed_gross": decimal_text(gross),
                    "settled_sum": decimal_text(settled_sum),
                }
                self.connection.execute(
                    "UPDATE revenue_allocations SET allocated_amount=?,basis_json=? WHERE allocation_id=?",
                    (decimal_text(amount), canonical_json(basis), line["allocation_id"]),
                )
                if line["status"] == "frozen":
                    frozen_payees.append(line["payee_id"])
            new_state = "disputed" if self._has_frozen(installment_id) else "assessed"
            self.connection.execute(
                "UPDATE revenue_installments SET gross_amount=?,assessed=1,state=?,"
                "assessed_by=?,assessed_at=? WHERE installment_id=?",
                (decimal_text(gross), new_state, actor_id, self._now(), installment_id),
            )
            self._audit("installment", installment_id, "revenue.assessed", actor_id, {
                "installment_id": installment_id,
                "previous_gross_amount": installment["gross_amount"],
                "assessed_gross_amount": decimal_text(gross),
                "settled_sum": decimal_text(settled_sum),
                "frozen_payees": frozen_payees,
                "note": note,
            })
        return self.installment_snapshot(installment_id)

    def _has_frozen(self, installment_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM revenue_allocations WHERE installment_id=? AND status='frozen' LIMIT 1",
            (installment_id,),
        ).fetchone()
        return row is not None

    def settle_revenue(self, actor_id: str, installment_id: str, payee_ids: Sequence[str] | None = None) -> dict[str, Any]:
        """结算未冻结份额；指定收款方则只结算对应份额。"""
        self._require(actor_id, "revenue.write")
        with transaction(self.connection, immediate=True):
            installment = self._get_installment(installment_id)
            if installment["state"] not in {"assessed", "disputed"}:
                raise InvalidState("期次必须先核定才能结算")
            allocations = self._allocations(installment_id)
            targets = set(payee_ids) if payee_ids else None
            settled: list[str] = []
            for line in allocations:
                if line["status"] != "pending":
                    continue
                if targets is not None and line["payee_id"] not in targets:
                    continue
                amount = Decimal(line["allocated_amount"])
                self.connection.execute(
                    "UPDATE revenue_allocations SET status='settled',settled_amount=?,settled_at=? "
                    "WHERE allocation_id=?",
                    (decimal_text(amount), self._now(), line["allocation_id"]),
                )
                settled.append(line["payee_id"])
            if not settled:
                raise InvalidState("没有可结算的未冻结份额")
            if targets is not None:
                missing = targets - {line["payee_id"] for line in allocations}
                if missing:
                    raise NotFound(f"收款方不存在: {sorted(missing)}")
            remaining = self.connection.execute(
                "SELECT count(*) n FROM revenue_allocations WHERE installment_id=? AND status<>'settled'",
                (installment_id,),
            ).fetchone()["n"]
            if remaining == 0:
                new_state = "settled"
                self.connection.execute(
                    "UPDATE revenue_installments SET state='settled',settled_at=? WHERE installment_id=?",
                    (self._now(), installment_id),
                )
            else:
                new_state = "disputed" if self._has_frozen(installment_id) else "assessed"
                self.connection.execute(
                    "UPDATE revenue_installments SET state=? WHERE installment_id=?",
                    (new_state, installment_id),
                )
            self._audit("installment", installment_id, "revenue.settled", actor_id, {
                "installment_id": installment_id,
                "settled_payees": settled,
                "remaining_lines": remaining,
                "state": new_state,
            })
        return self.installment_snapshot(installment_id)

    # ---------------------------------------------------------------- 争议

    def open_dispute(self, actor_id: str, installment_id: str, payee_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "dispute.write")
        with transaction(self.connection, immediate=True):
            installment = self._get_installment(installment_id)
            line = self.connection.execute(
                "SELECT * FROM revenue_allocations WHERE installment_id=? AND payee_id=?",
                (installment_id, payee_id),
            ).fetchone()
            if line is None:
                raise NotFound("该收款方在此期次中没有分配份额")
            if line["status"] == "settled":
                raise InvalidState("已结算份额不能因争议倒改冻结")
            if line["status"] == "frozen":
                raise Conflict("该份额已在争议冻结中")
            cursor = self.connection.execute(
                "INSERT INTO revenue_disputes(installment_id,payee_id,frozen_amount,reason,opened_by,opened_at) "
                "VALUES(?,?,?,?,?,?)",
                (installment_id, payee_id, line["allocated_amount"], reason, actor_id, self._now()),
            )
            dispute_id = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE revenue_allocations SET status='frozen' WHERE allocation_id=?",
                (line["allocation_id"],),
            )
            self.connection.execute(
                "UPDATE revenue_installments SET state='disputed' WHERE installment_id=?",
                (installment_id,),
            )
            self._audit("installment", installment_id, "dispute.opened", actor_id, {
                "dispute_id": dispute_id,
                "payee_id": payee_id,
                "frozen_amount": line["allocated_amount"],
                "reason": reason,
            })
        return self.installment_snapshot(installment_id)

    def resolve_dispute(
        self,
        actor_id: str,
        dispute_id: int,
        resolution: str,
        *,
        adjusted_amount: object | None = None,
    ) -> dict[str, Any]:
        """解除某一份额的冻结。

        若给出 ``adjusted_amount``，则把该份额调整为核定后的剩余金额
        （毛收入减去其他已结算份额），保证期次合计仍等于毛收入；
        未结算的非争议份额在此一并按相对比例重算。
        """
        self._require(actor_id, "dispute.write")
        with transaction(self.connection, immediate=True):
            dispute = self.connection.execute(
                "SELECT * FROM revenue_disputes WHERE dispute_id=?",
                (dispute_id,),
            ).fetchone()
            if dispute is None:
                raise NotFound("争议不存在")
            if dispute["state"] != "open":
                raise InvalidState("争议已解决")
            installment = self._get_installment(dispute["installment_id"])
            gross = Decimal(installment["gross_amount"])
            allocations = self._allocations(dispute["installment_id"])
            if adjusted_amount is not None:
                from .models import decimal_value

                target_amount = quantize_money(
                    decimal_value(adjusted_amount, "adjusted_amount", minimum=ZERO)
                )
                settled_sum = sum(
                    (Decimal(line["settled_amount"] or line["allocated_amount"])
                     for line in allocations if line["status"] == "settled"),
                    ZERO,
                )
                residual = quantize_money(gross - settled_sum)
                # 其他在途的未结算份额（不含本争议份额、不含已结算）。
                other_pending = [
                    line for line in allocations
                    if line["payee_id"] != dispute["payee_id"] and line["status"] == "pending"
                ]
                if target_amount > residual:
                    raise InvalidState("调整后金额超过毛收入扣除已结算后的余额")
                self.connection.execute(
                    "UPDATE revenue_allocations SET status='pending',allocated_amount=?,basis_json=? "
                    "WHERE installment_id=? AND payee_id=?",
                    (
                        decimal_text(target_amount),
                        canonical_json({
                            "stage": "dispute_resolved",
                            "formula": "adjusted_amount fixed by dispute resolution",
                            "license_id": installment["license_id"],
                            "agreement_id": installment["agreement_id"],
                            "version_no": installment["version_no"],
                            "period_label": installment["period_label"],
                            "share_percent": next(
                                (line["share_percent"] for line in allocations if line["payee_id"] == dispute["payee_id"]),
                                None,
                            ),
                            "assessed_gross": decimal_text(gross),
                            "settled_sum": decimal_text(settled_sum),
                        }),
                        dispute["installment_id"],
                        dispute["payee_id"],
                    ),
                )
                remaining_pool = quantize_money(residual - target_amount)
                if other_pending:
                    weights = [(line["payee_id"], Decimal(line["share_percent"]), line["source"]) for line in other_pending]
                    reallocated = {
                        item["payee_id"]: item["allocated_amount"]
                        for item in allocate_pool(remaining_pool, weights)
                    }
                    for line in other_pending:
                        self.connection.execute(
                            "UPDATE revenue_allocations SET allocated_amount=?,basis_json=? WHERE allocation_id=?",
                            (
                                decimal_text(reallocated[line["payee_id"]]),
                                canonical_json({
                                    "stage": "dispute_resolved",
                                    "formula": "(residual - adjusted_disputed_amount) * share / sum(other_pending_shares)",
                                    "license_id": installment["license_id"],
                                    "agreement_id": installment["agreement_id"],
                                    "version_no": installment["version_no"],
                                    "period_label": installment["period_label"],
                                    "share_percent": line["share_percent"],
                                    "assessed_gross": decimal_text(gross),
                                    "settled_sum": decimal_text(settled_sum),
                                }),
                                line["allocation_id"],
                            ),
                        )
            else:
                self.connection.execute(
                    "UPDATE revenue_allocations SET status='pending' WHERE installment_id=? AND payee_id=?",
                    (dispute["installment_id"], dispute["payee_id"]),
                )
            self.connection.execute(
                "UPDATE revenue_disputes SET state='resolved',resolved_by=?,resolved_at=?,resolution=? "
                "WHERE dispute_id=? AND state='open'",
                (actor_id, self._now(), resolution, dispute_id),
            )
            new_state = "assessed" if not self._has_frozen(dispute["installment_id"]) else "disputed"
            self.connection.execute(
                "UPDATE revenue_installments SET state=? WHERE installment_id=?",
                (new_state, dispute["installment_id"]),
            )
            self._audit("installment", dispute["installment_id"], "dispute.resolved", actor_id, {
                "dispute_id": dispute_id,
                "payee_id": dispute["payee_id"],
                "adjusted_amount": adjusted_amount,
                "resolution": resolution,
            })
        return self.installment_snapshot(dispute["installment_id"])

    # -------------------------------------------------------------- 查询解释

    def installment_snapshot(self, installment_id: str) -> dict[str, Any]:
        installment = self._get_installment(installment_id)
        allocations = self._allocations(installment_id)
        total = sum((Decimal(line["allocated_amount"]) for line in allocations), ZERO)
        gross_total = Decimal(installment["gross_amount"])
        return {
            "installment_id": installment_id,
            "license_id": installment["license_id"],
            "agreement_id": installment["agreement_id"],
            "version_no": installment["version_no"],
            "period_label": installment["period_label"],
            "currency": installment["currency"],
            "state": installment["state"],
            "estimated_amount": installment["estimated_amount"],
            "gross_amount": installment["gross_amount"],
            "allocated_total": decimal_text(quantize_money(total)),
            "retained_amount": decimal_text(quantize_money(gross_total - total)),
            "allocations": [
                {
                    "payee_id": line["payee_id"],
                    "share_percent": line["share_percent"],
                    "allocated_amount": line["allocated_amount"],
                    "status": line["status"],
                    "settled_amount": line["settled_amount"],
                }
                for line in allocations
            ],
        }

    def explain_installment(self, actor_id: str, installment_id: str) -> dict[str, Any]:
        """解释每笔收入的分配依据：协议版本快照 + 逐行公式 + 审计时间线。"""
        self._require(actor_id, "revenue.read")
        snapshot = self.installment_snapshot(installment_id)
        lines = []
        for row in self._allocations(installment_id):
            basis = json.loads(row["basis_json"])
            lines.append({
                "payee_id": row["payee_id"],
                "share_percent": row["share_percent"],
                "allocated_amount": row["allocated_amount"],
                "status": row["status"],
                "settled_amount": row["settled_amount"],
                "basis": basis,
                "explanation": (
                    f"{row['payee_id']} 按协议 {basis['agreement_id']} v{basis['version_no']} "
                    f"（期次 {basis['period_label']}）持有 {basis['share_percent']}% 分成；"
                    f"{basis['stage']} 阶段依「{basis['formula']}」计算，"
                    f"当前金额 {row['allocated_amount']} {snapshot['currency']}。"
                ),
            })
        disputes = self.connection.execute(
            "SELECT dispute_id,payee_id,frozen_amount,reason,state,opened_at,resolved_at,resolution "
            "FROM revenue_disputes WHERE installment_id=? ORDER BY dispute_id",
            (installment_id,),
        ).fetchall()
        timeline = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM lic_audit_events "
            "WHERE entity_type='installment' AND entity_id=? ORDER BY event_id",
            (installment_id,),
        ).fetchall()
        return {
            **snapshot,
            "agreement_snapshot": self.agreement_snapshot(snapshot["agreement_id"], snapshot["version_no"]),
            "license_snapshot": self.license_snapshot(snapshot["license_id"]),
            "allocation_basis": lines,
            "disputes": [dict(item) for item in disputes],
            "timeline": [
                {"event_type": row["event_type"], "actor_id": row["actor_id"],
                 "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]}
                for row in timeline
            ],
        }

    def rebuild_license_chain(
        self,
        actor_id: str,
        *,
        achievement_id: str | None = None,
        licensee_id: str | None = None,
    ) -> dict[str, Any]:
        """按成果或承接方重建许可链：协议版本 → 许可 → 续约/改进/收益。"""
        self._require(actor_id, "chain.read")
        if not achievement_id and not licensee_id:
            raise ValidationFailed("必须提供 achievement_id 或 licensee_id")
        where: list[str] = []
        params: list[Any] = []
        if achievement_id:
            where.append("achievement_id=?")
            params.append(achievement_id)
        if licensee_id:
            where.append("licensee_id=?")
            params.append(licensee_id)
        license_rows = self.connection.execute(
            "SELECT * FROM licenses WHERE " + " AND ".join(where) + " ORDER BY valid_from,license_id",
            params,
        ).fetchall()
        chain_licenses = []
        for row in license_rows:
            improvements = self.connection.execute(
                "SELECT improvement_id,title,ownership,agreement_id,version_no FROM improvements "
                "WHERE parent_license_id=? ORDER BY improvement_id",
                (row["license_id"],),
            ).fetchall()
            installments = self.connection.execute(
                "SELECT installment_id,period_label,state,gross_amount,currency FROM revenue_installments "
                "WHERE license_id=? ORDER BY period_label",
                (row["license_id"],),
            ).fetchall()
            regions = self.connection.execute(
                "SELECT region_code,field_scope FROM license_regions WHERE license_id=? ORDER BY position",
                (row["license_id"],),
            ).fetchall()
            chain_licenses.append({
                "license_id": row["license_id"],
                "licensee_id": row["licensee_id"],
                "license_type": row["license_type"],
                "state": row["state"],
                "agreement_id": row["agreement_id"],
                "version_no": row["version_no"],
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "regions": [dict(item) for item in regions],
                "renewal_of_license_id": row["renewal_of_license_id"],
                "superseded_by_license_id": row["superseded_by_license_id"],
                "improvements": [dict(item) for item in improvements],
                "installments": [dict(item) for item in installments],
            })
        # 汇总相关协议版本（只列与这些许可相关的版本）。
        agreement_keys = sorted({(item["agreement_id"], item["version_no"]) for item in chain_licenses})
        agreements = [self.agreement_snapshot(key[0], key[1]) for key in agreement_keys]
        # 续约谈判记录。
        license_ids = [item["license_id"] for item in chain_licenses]
        renewals: list[dict[str, Any]] = []
        if license_ids:
            placeholders = ",".join("?" for _ in license_ids)
            renewal_rows = self.connection.execute(
                f"SELECT negotiation_id,old_license_id,new_license_id,state,note,opened_at,closed_at "
                f"FROM renewal_negotiations WHERE old_license_id IN ({placeholders}) "
                f"OR new_license_id IN ({placeholders}) ORDER BY negotiation_id",
                license_ids + license_ids,
            ).fetchall()
            renewals = [dict(item) for item in renewal_rows]
        roots = [
            item["license_id"]
            for item in chain_licenses
            if item["renewal_of_license_id"] is None
        ]
        return {
            "achievement_id": achievement_id,
            "licensee_id": licensee_id,
            "agreement_versions": agreements,
            "licenses": chain_licenses,
            "renewals": renewals,
            "chain_roots": roots,
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM lic_audit_events ORDER BY event_id").fetchall()
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
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}


def _jsonable(value: Any) -> Any:
    """把 Decimal 等转为 canonical_json 可序列化的形式。"""
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value
