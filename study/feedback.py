"""面向社区的反馈视图。

社区反馈员只能看到聚合结果，且任何去标识家庭数低于
FEEDBACK_MIN_HOUSEHOLDS 的单元格一律抑制：不返回点估计与区间，
家庭数只给分桶（<阈值 / 阈值+），从输出侧杜绝小单元格反演。

血缘、分析编号、设备序列、同意记录均不出现在该视图中。
"""

from .config import FEEDBACK_MIN_HOUSEHOLDS
from .errors import NotFoundError


class FeedbackService:
    def __init__(self, analysis_conn):
        self.conn = analysis_conn

    def version_feedback(self, version_id, min_households=FEEDBACK_MIN_HOUSEHOLDS):
        version = self.conn.execute(
            "SELECT version_id, title, period_start, period_end, region_count "
            "FROM estimate_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if version is None:
            raise NotFoundError(f"冻结版本不存在: {version_id}")
        rows = self.conn.execute(
            "SELECT * FROM estimate_region_results WHERE version_id=? ORDER BY region_code",
            (version_id,),
        ).fetchall()

        cells = []
        suppressed = 0
        for r in rows:
            cell = {
                "region_code": r["region_code"],
                "n_households_bucket": (
                    f"<{min_households}" if r["n_households"] < min_households
                    else f"{min_households}+"
                ),
                "n_person_days": r["n_person_days"],
                "quality": r["quality"],
            }
            if r["n_households"] < min_households or r["quality"] == "no_data":
                # 小单元格或无数据：抑制一切可反推暴露水平的数值
                cell["suppressed"] = True
                cell["suppress_reason"] = (
                    "small_cell" if r["n_households"] < min_households else "no_data"
                )
                suppressed += 1
            else:
                cell.update(suppressed=False, point=r["point"],
                            ci_low=r["ci_low"], ci_high=r["ci_high"])
            cells.append(cell)
        return {
            "version_id": version_id,
            "title": version["title"],
            "period": [version["period_start"], version["period_end"]],
            "min_households_per_cell": min_households,
            "region_count": len(cells),
            "cells_suppressed": suppressed,
            "cells_released": len(cells) - suppressed,
            "cells": cells,
        }
