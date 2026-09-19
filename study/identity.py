"""身份库领域服务：家庭、成员、同意、搬家、退出、随访、风险与通知。

身份库是唯一保存真实身份与 analysis_id 对应关系的地方。所有写操作
经同一把锁串行化，并记录审计事件。
"""

import secrets
import time

from .errors import ConflictError, NotFoundError, ValidationError

AGE_BANDS = ("0-4", "5-11", "12-17", "18-59", "60+")
MEMBER_ROLES = ("woman", "child", "man", "elder", "other")

# 定向通知只允许使用固定话术模板，模板中不含任何暴露数值，
# 从机制上避免伦理侧把暴露水平写进面向家庭的通知。
REASON_TEMPLATES = {
    "symptom_followup": "健康随访提醒：请联系该家庭安排症状复查，具体关怀流程见现场手册（本通知不含监测数值）。",
    "data_quality": "监测安排核对：请与该家庭确认近期监测时间与设备使用情况（本通知不反馈暴露水平）。",
    "participation": "参与状态确认：请确认该家庭是否继续参与研究并更新联系方式。",
}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def new_analysis_id():
    return "A" + secrets.token_hex(6)


class IdentityService:
    def __init__(self, conn, lock):
        self.conn = conn
        self.lock = lock
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ethics_risks (
                risk_id     TEXT PRIMARY KEY,
                version_id  TEXT,
                region_code TEXT,
                reason      TEXT NOT NULL,
                severity    TEXT NOT NULL,
                created_by  TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                resolved_on TEXT
            );
            """
        )

    # ---- 审计 ----------------------------------------------------------
    def audit(self, actor, action, detail):
        with self.lock:
            self.conn.execute(
                "INSERT INTO audit_events(actor, action, detail, created_at) VALUES (?,?,?,?)",
                (actor, action, detail, _now()),
            )

    # ---- 家庭 ----------------------------------------------------------
    def register_household(self, actor, household_id, community, region_code,
                           contact_ref, enrolled_on):
        with self.lock:
            exists = self.conn.execute(
                "SELECT 1 FROM households WHERE household_id=?", (household_id,)
            ).fetchone()
            if exists:
                raise ConflictError(f"家庭已存在: {household_id}")
            self.conn.execute(
                "INSERT INTO households VALUES (?,?,?,?,?)",
                (household_id, community, region_code, contact_ref, enrolled_on),
            )
            self.conn.execute(
                "INSERT INTO household_region_history(household_id, region_code, effective_from)"
                " VALUES (?,?,?)",
                (household_id, region_code, enrolled_on),
            )
            self.audit(actor, "household.register", household_id)
        return {"household_id": household_id, "region_code": region_code}

    def record_move(self, actor, household_id, new_region_code, effective_date):
        """搬家：关闭旧地区区间（effective_to=生效日前一天），开启新区间。"""
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM household_region_history WHERE household_id=? AND effective_to IS NULL",
                (household_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"家庭不存在或无在住地区: {household_id}")
            if row["region_code"] == new_region_code:
                raise ValidationError("新地区与当前地区相同")
            prev_end = _shift_days(effective_date, -1)
            if prev_end < row["effective_from"]:
                raise ValidationError("搬家生效日期不得早于入住日期")
            self.conn.execute(
                "UPDATE household_region_history SET effective_to=? "
                "WHERE household_id=? AND effective_to IS NULL",
                (prev_end, household_id),
            )
            self.conn.execute(
                "INSERT INTO household_region_history(household_id, region_code, effective_from)"
                " VALUES (?,?,?)",
                (household_id, new_region_code, effective_date),
            )
            self.conn.execute(
                "UPDATE households SET region_code=? WHERE household_id=?",
                (new_region_code, household_id),
            )
            self.audit(actor, "household.move",
                       f"{household_id} {row['region_code']}->{new_region_code} @ {effective_date}")
        return {"household_id": household_id, "region_code": new_region_code,
                "effective_from": effective_date}

    def region_on(self, household_id, day):
        """返回某家庭在某日所在地区（人时归属用）。"""
        row = self.conn.execute(
            "SELECT region_code FROM household_region_history "
            "WHERE household_id=? AND effective_from<=? "
            "AND (effective_to IS NULL OR effective_to>=?) ORDER BY effective_from DESC",
            (household_id, day, day),
        ).fetchone()
        if row is None:
            return None
        return row["region_code"]

    # ---- 成员 ----------------------------------------------------------
    def register_member(self, actor, member_id, household_id, pseudonym,
                        age_band, role, enrolled_on, analysis_id=None):
        if age_band not in AGE_BANDS:
            raise ValidationError(f"年龄段必须是 {AGE_BANDS} 之一")
        if role not in MEMBER_ROLES:
            raise ValidationError(f"成员身份必须是 {MEMBER_ROLES} 之一")
        analysis_id = analysis_id or new_analysis_id()
        with self.lock:
            hh = self.conn.execute(
                "SELECT 1 FROM households WHERE household_id=?", (household_id,)
            ).fetchone()
            if hh is None:
                raise NotFoundError(f"家庭不存在: {household_id}")
            try:
                self.conn.execute(
                    "INSERT INTO members VALUES (?,?,?,?,?,?,NULL)",
                    (member_id, household_id, pseudonym, age_band, role, enrolled_on),
                )
            except Exception as exc:  # sqlite3.IntegrityError
                raise ConflictError(f"成员已存在: {member_id}") from exc
            self.conn.execute(
                "INSERT INTO analysis_links(analysis_id, member_id, household_id, created_on)"
                " VALUES (?,?,?,?)",
                (analysis_id, member_id, household_id, _today()),
            )
            self.audit(actor, "member.register", f"{member_id}->{analysis_id}")
        return {"member_id": member_id, "analysis_id": analysis_id,
                "age_band": age_band, "role": role}

    def withdraw_member(self, actor, member_id, withdrawn_on):
        with self.lock:
            row = self.conn.execute(
                "SELECT withdrawn_on FROM members WHERE member_id=?", (member_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"成员不存在: {member_id}")
            if row["withdrawn_on"]:
                raise ConflictError(f"成员已退出: {member_id}")
            self.conn.execute(
                "UPDATE members SET withdrawn_on=? WHERE member_id=?",
                (withdrawn_on, member_id),
            )
            self.audit(actor, "member.withdraw", f"{member_id} @ {withdrawn_on}")
        return {"member_id": member_id, "withdrawn_on": withdrawn_on}

    def resolve_analysis_id(self, analysis_id):
        """analysis_id -> member/household（仅身份库内调用）。"""
        row = self.conn.execute(
            """SELECT l.analysis_id, l.member_id, l.household_id,
                      m.age_band, m.role, m.withdrawn_on, h.contact_ref
               FROM analysis_links l
               JOIN members m ON m.member_id=l.member_id
               JOIN households h ON h.household_id=l.household_id
               WHERE l.analysis_id=?""",
            (analysis_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"分析编号不存在: {analysis_id}")
        return dict(row)

    def all_analysis_links(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT analysis_id, member_id, household_id FROM analysis_links"
        )]

    # ---- 同意 ----------------------------------------------------------
    def grant_consent(self, actor, consent_id, household_id, scope, version, granted_on):
        with self.lock:
            hh = self.conn.execute(
                "SELECT 1 FROM households WHERE household_id=?", (household_id,)
            ).fetchone()
            if hh is None:
                raise NotFoundError(f"家庭不存在: {household_id}")
            try:
                self.conn.execute(
                    "INSERT INTO consents VALUES (?,?,?,?,?,NULL)",
                    (consent_id, household_id, scope, version, granted_on),
                )
            except Exception as exc:
                raise ConflictError(f"同意记录已存在: {consent_id}") from exc
            self.audit(actor, "consent.grant", f"{consent_id} v{version} {household_id}")
        return {"consent_id": consent_id, "version": version, "granted_on": granted_on}

    def revoke_consent(self, actor, consent_id, revoked_on):
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM consents WHERE consent_id=?", (consent_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"同意记录不存在: {consent_id}")
            if row["revoked_on"]:
                raise ConflictError("同意已撤销")
            self.conn.execute(
                "UPDATE consents SET revoked_on=? WHERE consent_id=?",
                (revoked_on, consent_id),
            )
            self.audit(actor, "consent.revoke", f"{consent_id} @ {revoked_on}")
        return {"consent_id": consent_id, "revoked_on": revoked_on}

    def active_consent(self, household_id, day):
        """该家庭在某日是否存在覆盖该日的同意版本（撤销次日起失效）。"""
        return self.conn.execute(
            "SELECT * FROM consents WHERE household_id=? AND granted_on<=? "
            "AND (revoked_on IS NULL OR revoked_on>?) ORDER BY granted_on DESC",
            (household_id, day, day),
        ).fetchone()

    # ---- 随访 ----------------------------------------------------------
    def add_followup(self, actor, followup_id, member_id, observed_on, symptoms, note=""):
        if not isinstance(symptoms, list) or not symptoms:
            raise ValidationError("symptoms 必须是非空数组")
        with self.lock:
            m = self.conn.execute(
                "SELECT 1 FROM members WHERE member_id=?", (member_id,)
            ).fetchone()
            if m is None:
                raise NotFoundError(f"成员不存在: {member_id}")
            import json
            self.conn.execute(
                "INSERT INTO followups VALUES (?,?,?,?,?)",
                (followup_id, member_id, observed_on, json.dumps(symptoms, ensure_ascii=False), note),
            )
            self.audit(actor, "followup.add", f"{followup_id} member={member_id}")
        return {"followup_id": followup_id, "symptom_count": len(symptoms)}

    def symptom_alert(self, member_id, start, end):
        """统计成员在[start,end]内随访记录中单次症状数达标的次数。"""
        import json
        rows = self.conn.execute(
            "SELECT symptoms FROM followups WHERE member_id=? AND observed_on BETWEEN ? AND ?",
            (member_id, start, end),
        ).fetchall()
        from .config import SYMPTOM_ALERT_MIN
        return any(len(json.loads(r["symptoms"])) >= SYMPTOM_ALERT_MIN for r in rows)

    # ---- 伦理风险 ------------------------------------------------------
    def register_risk(self, actor, risk_id, reason, severity,
                      version_id=None, region_code=None):
        if severity not in ("low", "medium", "high"):
            raise ValidationError("severity 必须是 low/medium/high")
        with self.lock:
            self.conn.execute(
                "INSERT INTO ethics_risks VALUES (?,?,?,?,?,?,?,NULL)",
                (risk_id, version_id, region_code, reason, severity, actor, _now()),
            )
            self.audit(actor, "risk.register", f"{risk_id} {severity}")
        return {"risk_id": risk_id, "severity": severity}

    # ---- 定向通知 ------------------------------------------------------
    def create_notification(self, actor, notification_id, reason_code, analysis_ids,
                            risk_id=None):
        if reason_code not in REASON_TEMPLATES:
            raise ValidationError(f"reason_code 必须是 {sorted(REASON_TEMPLATES)} 之一")
        if not analysis_ids:
            raise ValidationError("至少指定一个分析编号")
        message = REASON_TEMPLATES[reason_code]
        with self.lock:
            self.conn.execute(
                "INSERT INTO notifications(notification_id, reason_code, message, created_by, created_at)"
                " VALUES (?,?,?,?,?)",
                (notification_id, reason_code, message, actor, _now()),
            )
            for aid in analysis_ids:
                link = self.conn.execute(
                    "SELECT member_id, household_id FROM analysis_links WHERE analysis_id=?",
                    (aid,),
                ).fetchone()
                if link is None:
                    raise NotFoundError(f"分析编号不存在: {aid}")
                self.conn.execute(
                    "INSERT INTO notification_targets VALUES (?,?,?,?)",
                    (notification_id, aid, link["member_id"], link["household_id"]),
                )
            self.audit(actor, "notification.create",
                       f"{notification_id} reason={reason_code} n={len(analysis_ids)} risk={risk_id}")
        return {"notification_id": notification_id, "reason_code": reason_code,
                "target_count": len(analysis_ids), "message": message}

    def deliver_notification(self, actor, notification_id, delivery_note=""):
        with self.lock:
            row = self.conn.execute(
                "SELECT delivered_at FROM notifications WHERE notification_id=?",
                (notification_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"通知不存在: {notification_id}")
            if row["delivered_at"]:
                raise ConflictError("通知已送达")
            self.conn.execute(
                "UPDATE notifications SET delivered_at=?, delivery_note=? WHERE notification_id=?",
                (_now(), delivery_note, notification_id),
            )
            self.audit(actor, "notification.deliver", notification_id)
        return {"notification_id": notification_id, "delivered_at": _now()}

    def list_notifications(self, include_undelivered_only=False,
                          include_contacts=False):
        """列通知。include_contacts=False（伦理视图）时只给分析编号与状态，
        联系方式与成员代称仅在现场送达（include_contacts=True）时返回。
        """
        sql = (
            "SELECT n.*, "
            "(SELECT COUNT(*) FROM notification_targets t WHERE t.notification_id=n.notification_id) AS n_targets "
            "FROM notifications n"
        )
        params = ()
        if include_undelivered_only:
            sql += " WHERE n.delivered_at IS NULL"
        rows = self.conn.execute(sql + " ORDER BY n.created_at", params).fetchall()
        result = []
        for r in rows:
            item = dict(r)
            if include_contacts:
                targets = self.conn.execute(
                    """SELECT t.analysis_id, t.member_id, t.household_id, h.contact_ref,
                              m.pseudonym
                       FROM notification_targets t
                       JOIN households h ON h.household_id=t.household_id
                       JOIN members m ON m.member_id=t.member_id
                       WHERE t.notification_id=?""",
                    (r["notification_id"],),
                ).fetchall()
                item["targets"] = [dict(t) for t in targets]
            else:
                targets = self.conn.execute(
                    "SELECT analysis_id FROM notification_targets WHERE notification_id=?",
                    (r["notification_id"],),
                ).fetchall()
                item["analysis_ids"] = [t["analysis_id"] for t in targets]
            result.append(item)
        return result

    def get_notification_for_delivery(self, notification_id):
        """现场协调员送达前获取单个通知及其家庭联系方式。"""
        row = self.conn.execute(
            "SELECT * FROM notifications WHERE notification_id=?", (notification_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"通知不存在: {notification_id}")
        item = dict(row)
        targets = self.conn.execute(
            """SELECT t.analysis_id, t.member_id, t.household_id, h.contact_ref,
                      m.pseudonym
               FROM notification_targets t
               JOIN households h ON h.household_id=t.household_id
               JOIN members m ON m.member_id=t.member_id
               WHERE t.notification_id=?""",
            (notification_id,),
        ).fetchall()
        item["targets"] = [dict(t) for t in targets]
        return item


def _shift_days(day, delta):
    import datetime
    d = datetime.date.fromisoformat(day)
    return (d + datetime.timedelta(days=delta)).isoformat()
