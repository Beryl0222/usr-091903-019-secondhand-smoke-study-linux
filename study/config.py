"""研究范围常量与运行期配置。

所有"覆盖范围"以 REGION_COUNT 为准；分析端只能在已登记地区内产出结果。
"""

import os

REGION_COUNT = 204

# 社区反馈中任一单元格涉及的去标识家庭数下限：低于该值抑制，
# 避免社区侧通过小单元格反推出具体家庭。
FEEDBACK_MIN_HOUSEHOLDS = 5

# 定向通知去标识阈值（通知由现场协调员在身份库内落地，不经过社区反馈）。
NOTIFY_MIN_HOUSEHOLDS = 1

# 随访症状阈值（演示用规则，真实方案应在冻结方法中固化）。
SYMPTOM_ALERT_MIN = 2


def env_int(name, default):
    raw = os.environ.get(name)
    return int(raw) if raw else default
