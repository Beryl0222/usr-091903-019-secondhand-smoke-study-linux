"""身份保险库：家庭、成员、同意区间与分析编号分库存放。

隐私边界：

- 身份库保存姓名/联系方式/住址，以及成员真实编号；
- 分析库只保存不重复的随机样式编号 ``analysis_id``，映射表单独立表，
  只有伦理/管理员角色可解析，且每次解析都写审计；
- 分析与社区角色看到的成员视图不含姓名、联系方式、住址与出生日期，
  年龄段由系统按日期计算后输出。
"""

import threading

from study.errors import Conflict, NotFound, PermissionDenied, ValidationError
from study.util import iso, member_age_band, new_id, parse_date

# 允许触及身份明文的角色；现场角色仅用于执行经伦理发起的定向通知。
IDENTITY_ROLES = {"ethics", "admin", "field"}
ANALYSIS_ROLES = {"analyst", "admin", "ethics"}


class IdentityVault:
    def __init__(self, catalog, audit):
        self._catalog = catalog
        self._audit = audit
        self._lock = threading.Lock()
        self._households = {}
        self._members = {}
        self._consent = {}          # household_id -> [区间]
        self._residency = {}        # member_id -> [{household_id, start, end}]
        self._withdrawn = {}        # member_id -> {date, reason}
        # 分库：分析编号映射表独立存放，访问走专用方法。
        self._pseudo = {}           # analysis_id -> member_id
        self._pseudo_reverse = {}   # member_id -> analysis_id

    # ---- 家庭 ----------------------------------------------------------
    def create_household(self, actor, community_id, contact_name=None,
                         contact_phone=None, address=None):
        self._catalog.get_community(community_id)
        self._require_actor(actor, {"field", "admin"})
        with self._lock:
            household_id = new_id("HH", self._households)
            record = {
                "household_id": household_id,
                "community_id": community_id,
                "contact_name": contact_name,
                "contact_phone": contact_phone,
                "address": address,
            }
            self._households[household_id] = record
            self._consent[household_id] = []
        self._audit.append(actor, "household_created",
                           household_id=household_id, community_id=community_id)
        return {"household_id": household_id, "community_id": community_id}

    def get_household_identity(self, actor, household_id):
        """读取身份明文（姓名/联系方式/住址）：限伦理/管理员，全程审计。"""
        self._require_actor(actor, {"ethics", "admin"})
        record = self._households.get(household_id)
        if record is None:
            raise NotFound(f"家庭不存在：{household_id}")
        self._audit.append(actor, "identity_accessed", household_id=household_id)
        return dict(record)

    def get_household(self, household_id):
        record = self._households.get(household_id)
        if record is None:
            raise NotFound(f"家庭不存在：{household_id}")
        # 非明文视图。
        return {"household_id": household_id,
                "community_id": record["community_id"]}

    def _household_or_raise(self, household_id):
        record = self._households.get(household_id)
        if record is None:
            raise NotFound(f"家庭不存在：{household_id}")
        return record

    def list_households(self, community_id=None):
        """非明文家庭列表（编号与社区），供质量汇总与通知去重。"""
        rows = [{"household_id": hid, "community_id": rec["community_id"]}
                for hid, rec in self._households.items()]
        if community_id is not None:
            rows = [r for r in rows if r["community_id"] == community_id]
        return sorted(rows, key=lambda r: r["household_id"])

    # ---- 成员 ----------------------------------------------------------
    def add_member(self, actor, household_id, birth_date, sex=None,
                   name=None, move_in=None):
        self._require_actor(actor, {"field", "admin"})
        household = self._household_or_raise(household_id)
        birth = parse_date(birth_date)
        start = parse_date(move_in) if move_in else birth
        if start < birth:
            raise ValidationError("入住/加入日期不能早于出生日期")
        with self._lock:
            member_id = new_id("M", self._members)
            self._members[member_id] = {
                "member_id": member_id,
                "household_id": household_id,
                "birth_date": birth,
                "sex": sex,
                "name": name,
            }
            self._residency[member_id] = [{
                "household_id": household_id,
                "community_id": household["community_id"],
                "start": start,
                "end": None,
            }]
            analysis_id = f"A-{len(self._pseudo) + 1:06d}"
            self._pseudo[analysis_id] = member_id
            self._pseudo_reverse[member_id] = analysis_id
        self._audit.append(actor, "member_created",
                           household_id=household_id, analysis_id=analysis_id)
        return {"member_id": member_id, "analysis_id": analysis_id}

    # ---- 同意 ----------------------------------------------------------
    def grant_consent(self, actor, household_id, start_date, end_date=None,
                      document=None):
        self._require_actor(actor, {"field", "admin"})
        self._household_or_raise(household_id)
        start = parse_date(start_date)
        end = parse_date(end_date)
        if end is not None and end <= start:
            raise ValidationError("同意结束日必须晚于开始日")
        with self._lock:
            intervals = self._consent[household_id]
            for existing in intervals:
                e_start, e_end = existing["start"], existing["end"]
                ends = [d for d in (end, e_end) if d is not None]
                hi = min(ends) if ends else None
                if hi is None or max(start, e_start) < hi:
                    raise Conflict("该家庭存在重叠的同意区间，请先结束旧区间")
            intervals.append({"start": start, "end": end,
                              "document": document})
            intervals.sort(key=lambda item: item["start"])
        self._audit.append(actor, "consent_granted",
                           household_id=household_id,
                           start=iso(start), end=iso(end), document=document)
        return {"household_id": household_id,
                "start": iso(start), "end": iso(end)}

    def end_consent(self, actor, household_id, date, reason=None):
        """家庭整体撤回/同意到期：在真实日期截断开放同意区间。"""
        self._require_actor(actor, {"field", "admin"})
        self._household_or_raise(household_id)
        day = parse_date(date)
        with self._lock:
            open_intervals = [i for i in self._consent[household_id]
                              if i["end"] is None]
            if not open_intervals:
                raise Conflict("该家庭当前没有开放的同意区间")
            for interval in open_intervals:
                if day <= interval["start"]:
                    raise ValidationError("撤回日期不能早于同意开始日")
                interval["end"] = day
        self._audit.append(actor, "consent_ended",
                           household_id=household_id, date=iso(day), reason=reason)
        return {"household_id": household_id, "end": iso(day)}

    def withdraw_member(self, actor, analysis_id, date, reason=None):
        """成员个人退出：自真实日期起不再产生人时（身份不明文出现在日志）。"""
        self._require_actor(actor, {"field", "admin"})
        member_id = self._resolve(analysis_id)
        day = parse_date(date)
        with self._lock:
            if member_id in self._withdrawn:
                raise Conflict("该成员已登记退出")
            self._withdrawn[member_id] = {"date": day, "reason": reason}
        self._audit.append(actor, "member_withdrawn",
                           analysis_id=analysis_id, date=iso(day), reason=reason)
        return {"analysis_id": analysis_id, "withdrawn_on": iso(day)}

    # ---- 搬家 ----------------------------------------------------------
    def move_member(self, actor, analysis_id, to_household_id, date):
        """成员搬家：旧居住区间在搬迁日闭合，新家庭区间自同日开始。

        人时按真实日期截断，搬迁当日计入新家庭（半开区间约定）。
        """
        self._require_actor(actor, {"field", "admin"})
        member_id = self._resolve(analysis_id)
        destination = self._household_or_raise(to_household_id)
        day = parse_date(date)
        with self._lock:
            episodes = self._residency[member_id]
            current = next((e for e in episodes if e["end"] is None), None)
            if current is None:
                raise Conflict("该成员没有开放的居住区间，无法搬迁")
            if day <= current["start"]:
                raise ValidationError("搬迁日期必须晚于当前居住开始日")
            if current["household_id"] == to_household_id:
                raise ValidationError("成员已居住在该家庭")
            current["end"] = day
            episodes.append({
                "household_id": to_household_id,
                "community_id": destination["community_id"],
                "start": day,
                "end": None,
            })
        self._audit.append(actor, "member_moved",
                           analysis_id=analysis_id,
                           to_household_id=to_household_id, date=iso(day))
        return {"analysis_id": analysis_id,
                "household_id": to_household_id, "from_date": iso(day)}

    # ---- 编号分库 ------------------------------------------------------
    def analysis_id_of(self, member_id):
        if member_id not in self._pseudo_reverse:
            raise NotFound(f"成员不存在：{member_id}")
        return self._pseudo_reverse[member_id]

    def _resolve(self, analysis_id):
        member_id = self._pseudo.get(analysis_id)
        if member_id is None:
            raise NotFound(f"分析编号不存在：{analysis_id}")
        return member_id

    def resolve_analysis_id(self, actor, analysis_id):
        """把分析编号解析回成员真实身份：仅伦理/管理员，逐次审计。"""
        self._require_actor(actor, {"ethics", "admin"})
        member_id = self._resolve(analysis_id)
        self._audit.append(actor, "pseudo_resolved", analysis_id=analysis_id)
        return member_id

    def analysis_roster(self, actor):
        """分析侧花名册：只有分析编号、社区与当前年龄段，无任何身份字段。"""
        self._require_actor(actor, ANALYSIS_ROLES)
        from datetime import date
        today = date.today()
        rows = []
        for analysis_id, member_id in self._pseudo.items():
            member = self._members[member_id]
            episode = self._residency[member_id][-1]
            rows.append({
                "analysis_id": analysis_id,
                "community_id": episode["community_id"],
                "sex": member["sex"],
                "age_band": member_age_band(member["birth_date"], today),
            })
        rows.sort(key=lambda row: row["analysis_id"])
        return rows

    # ---- 供人时引擎使用的非明文接口 ------------------------------------
    def analysis_member_ids(self):
        return sorted(self._pseudo.keys())

    def birth_date_for_analysis(self, analysis_id):
        return self._members[self._resolve(analysis_id)]["birth_date"]

    def residency_episodes(self, analysis_id):
        """返回居住片段（含社区），供人时截断；不含身份字段。"""
        member_id = self._resolve(analysis_id)
        return [{
            "household_id": e["household_id"],
            "community_id": e["community_id"],
            "start": iso(e["start"]),
            "end": iso(e["end"]),
        } for e in self._residency[member_id]]

    def residency_episodes_raw(self, analysis_id):
        """内部使用：日期对象版本的居住片段。"""
        member_id = self._resolve(analysis_id)
        return [dict(e) for e in self._residency[member_id]]

    def consent_intervals(self, household_id):
        self._household_or_raise(household_id)
        return [{"start": iso(i["start"]), "end": iso(i["end"])}
                for i in self._consent[household_id]]

    def withdrawal(self, analysis_id):
        member_id = self._resolve(analysis_id)
        record = self._withdrawn.get(member_id)
        return None if record is None else {"date": iso(record["date"]),
                                            "reason": record["reason"]}

    def household_contact_for_notification(self, actor, household_id):
        """定向通知派发：现场可取联系方式，但系统不在此处暴露暴露数据。"""
        self._require_actor(actor, {"field", "ethics", "admin"})
        record = self._household_or_raise(household_id)
        return {
            "household_id": household_id,
            "community_id": record["community_id"],
            "contact_name": record["contact_name"],
            "contact_phone": record["contact_phone"],
        }

    def household_of_analysis(self, analysis_id):
        """成员当前所在家庭（通知路由用，不经过明文解析审计以外的暴露）。"""
        member_id = self._resolve(analysis_id)
        episodes = self._residency[member_id]
        current = next((e for e in episodes if e["end"] is None), episodes[-1])
        return current["household_id"], current["community_id"]

    # ---- 权限 ----------------------------------------------------------
    @staticmethod
    def _require_actor(actor, allowed):
        role = actor if isinstance(actor, str) else actor.get("role")
        if role not in allowed:
            raise PermissionDenied(
                f"角色 {role} 无权执行该操作", allowed=sorted(allowed))
