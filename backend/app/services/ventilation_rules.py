"""通风机风量判定规则：全平台唯一一份，两个入口（人工动作、实测判定）共用。

规则口径：
- 实测风量 / 额定风量 的比例落在不同区间，得到不同严重档；
- 两档边界不重叠：比例 < 故障阈值 -> 故障停机，落在 [故障阈值, 降频阈值) -> 降频运行，
  >= 降频阈值 -> 正常；
- 判定打架（同一次提交里人工动作与实测判定结论不一致）时，按严重度取更严的一档。

阈值本身支持调整（见 VentilationService.adjust_thresholds），但本模块只负责
"给定一组阈值如何判定"，不持有当前生效阈值，保证规则永远只有这一份实现。
"""
from __future__ import annotations

from typing import Any

# 状态序列与严重度：数字越大越严重，取严时直接比较即可
STATUS_NORMAL = "正常"
STATUS_DERATED = "降频运行"
STATUS_FAULT = "故障停机"
STATUS_REPLACED = "已更换"

SEVERITY_ORDER = [STATUS_NORMAL, STATUS_DERATED, STATUS_FAULT]
# 已更换是行政流转状态，不参与风量严重度比较
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITY_ORDER)}

# 默认阈值：五成降频、三成故障
DEFAULT_DERATE_RATIO = 0.5
DEFAULT_FAULT_RATIO = 0.3

# 每台通风机运行频率合理范围的统一口径（Hz）。设备可在此口径内收窄，不能超出。
FREQUENCY_MIN = 30.0
FREQUENCY_MAX = 50.0


def stricter(first: str, second: str) -> str:
    """两个判定结论打架时，返回更严重的一档。"""
    if first not in SEVERITY_RANK or second not in SEVERITY_RANK:
        raise ValueError("取严比较只接受正常/降频运行/故障停机三档")
    return first if SEVERITY_RANK[first] >= SEVERITY_RANK[second] else second


def classify_by_ratio(ratio: float, thresholds: dict[str, Any]) -> str:
    """按 实测/额定 比例与阈值判档，两档边界不重叠（左闭右开）。"""
    fault_ratio = float(thresholds["故障阈值"])
    derate_ratio = float(thresholds["降频阈值"])
    if ratio < fault_ratio:
        return STATUS_FAULT
    if ratio < derate_ratio:
        return STATUS_DERATED
    return STATUS_NORMAL


def validate_thresholds(fault_ratio: float, derate_ratio: float) -> str | None:
    """阈值校验：必须在 (0,1) 内，且故障档边界必须严于（小于）降频档，保证两档不重叠。"""
    if not 0 < fault_ratio < 1 or not 0 < derate_ratio < 1:
        return "故障阈值与降频阈值都必须落在 0 到 1 之间"
    if fault_ratio >= derate_ratio:
        return "故障阈值必须严格小于降频阈值，两档边界不许重叠"
    return None
