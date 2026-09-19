"""研究目录：204 个地区、社区、匿名公共场所、年龄段与控烟干预版本。

目录数据不含任何家庭身份信息，可以被分析角色直接读取。
场所只保存“匿名编号 + 类型 + 所属地区/社区”，现场不得录入可定位名称。
"""

from study.errors import Conflict, NotFound, ValidationError
from study.util import iso, new_id, parse_date

REGION_COUNT = 204

AGE_BANDS = ("under_5", "5_17", "18_59", "60_plus")
AGE_BAND_LABELS = {
    "under_5": "5岁以下儿童",
    "5_17": "5-17岁",
    "18_59": "18-59岁成人",
    "60_plus": "60岁及以上",
}

# 场所类型只允许粗类别，避免通过小众场所反查家庭。
VENUE_TYPES = ("restaurant", "market", "playground", "clinic_waiting", "transit", "other")


class Catalog:
    def __init__(self):
        self._regions = {}
        self._communities = {}
        self._venues = {}
        self._interventions = {}
        self._community_intervention = {}
        self._bootstrap_regions()

    def _bootstrap_regions(self):
        for number in range(1, REGION_COUNT + 1):
            code = f"R{number:03d}"
            self._regions[code] = {"region_code": code, "name": f"地区{number:03d}"}

    # ---- 地区 ----------------------------------------------------------
    def list_regions(self):
        return list(self._regions.values())

    def get_region(self, code):
        region = self._regions.get(code)
        if region is None:
            raise NotFound(f"地区不存在：{code}")
        return dict(region)

    # ---- 社区 ----------------------------------------------------------
    def add_community(self, community_id, region_code, name=None):
        if community_id in self._communities:
            raise Conflict(f"社区编号已存在：{community_id}")
        self.get_region(region_code)
        record = {
            "community_id": community_id,
            "region_code": region_code,
            "name": name,
        }
        self._communities[community_id] = record
        return dict(record)

    def get_community(self, community_id):
        community = self._communities.get(community_id)
        if community is None:
            raise NotFound(f"社区不存在：{community_id}")
        return dict(community)

    def list_communities(self, region_code=None):
        items = self._communities.values()
        if region_code is not None:
            items = [c for c in items if c["region_code"] == region_code]
        return [dict(c) for c in items]

    # ---- 匿名公共场所 --------------------------------------------------
    def add_venue(self, venue_type, community_id, label=None):
        if venue_type not in VENUE_TYPES:
            raise ValidationError(f"未知场所类型：{venue_type}", allowed=VENUE_TYPES)
        self.get_community(community_id)
        venue_id = new_id("V", self._venues)
        record = {
            "venue_id": venue_id,
            "venue_type": venue_type,
            "community_id": community_id,
            # label 仅为现场布点备注（如“集市北门”），不回传社区反馈。
            "label": label,
        }
        self._venues[venue_id] = record
        return dict(record)

    def get_venue(self, venue_id):
        venue = self._venues.get(venue_id)
        if venue is None:
            raise NotFound(f"场所不存在：{venue_id}")
        return dict(venue)

    def public_venue_view(self, venue_id):
        """对外（社区反馈）视图：只保留粗类型，不含布点备注。"""
        venue = self.get_venue(venue_id)
        return {"venue_id": venue["venue_id"], "venue_type": venue["venue_type"]}

    def list_venues(self, community_id=None):
        items = self._venues.values()
        if community_id is not None:
            items = [v for v in items if v["community_id"] == community_id]
        return [dict(v) for v in items]

    # ---- 控烟干预版本 --------------------------------------------------
    def register_intervention(self, version, effective_date, description=None):
        """登记干预版本及其政策生效日（人时按该日截断）。"""
        effective = parse_date(effective_date)
        if version in self._interventions:
            raise Conflict(f"干预版本已存在：{version}")
        for existing in self._interventions.values():
            if existing["effective_date"] == iso(effective):
                raise Conflict(
                    f"生效日 {iso(effective)} 已登记干预版本 {existing['version']}"
                )
        record = {
            "version": version,
            "effective_date": iso(effective),
            "description": description,
        }
        self._interventions[version] = record
        return dict(record)

    def get_intervention(self, version):
        record = self._interventions.get(version)
        if record is None:
            raise NotFound(f"干预版本不存在：{version}")
        return dict(record)

    def list_interventions(self):
        return [dict(r) for r in self._interventions.values()]

    # ---- 社区干预版本分配 ----------------------------------------------
    def assign_intervention(self, community_id, version, phase=None):
        """把某控烟干预版本分配给社区；人时按版本生效日划分政策前后。"""
        self.get_community(community_id)
        self.get_intervention(version)
        self._community_intervention[community_id] = {
            "version": version, "phase": phase}
        return {"community_id": community_id, "version": version, "phase": phase}

    def intervention_for_community(self, community_id):
        record = self._community_intervention.get(community_id)
        if record is None:
            return None
        return dict(record)

    def policy_phase(self, community_id, day):
        """某日属于政策前还是政策后（生效日当天起计为政策后）。"""
        record = self.intervention_for_community(community_id)
        if record is None:
            return "no_policy"
        effective = parse_date(
            self.get_intervention(record["version"])["effective_date"])
        return "post" if day >= effective else "pre"
