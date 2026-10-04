"""通风系统业务规则：设备档案、风量判定、阈值版本、欠风台账、运行日志。

设计约束（对应需求逐条落地）：
1. 每台通风机按额定风量定运行频率的合理范围；没有额定风量（或不是正数）一律不许保存；
2. 实测风量判定走 ventilation_rules 这唯一一份规则，人工动作入口与实测入口共用，
   判定打架取更严一档；
3. 同一台设备并发提交两次降频：按设备加锁 + 状态复查，只生效一次，后到的那次原样拒绝；
4. 降频/故障期间按设备运行时段累计欠风时长，回写到通风台账的欠风统计；
5. 阈值调整只产生新版本，已经归档的日子不重算；翻旧记录时按当时阈值快照还原；
6. 设备状态变化一律写运行日志。
"""
from __future__ import annotations

import threading
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from typing import Any, Callable

from app.services.ventilation_rules import (
    DEFAULT_DERATE_RATIO,
    DEFAULT_FAULT_RATIO,
    FREQUENCY_MAX,
    FREQUENCY_MIN,
    STATUS_DERATED,
    STATUS_FAULT,
    STATUS_NORMAL,
    STATUS_REPLACED,
    classify_by_ratio,
    stricter,
    validate_thresholds,
)
from app.store import store

MODULE = "ventilation"
THRESHOLD_TABLE = "ventilation_thresholds"
LEDGER_TABLE = "ventilation_ledger"
LOG_TABLE = "ventilation_logs"

REQUIRED_FIELDS = ["设备编号", "设备类型", "额定风量"]
# 人工动作 -> 目标严重档；办理更换走单独的行政流转
ACTION_SEVERITY = {
    "降频运行": STATUS_DERATED,
    "故障停机": STATUS_FAULT,
}
ADMIN_ACTIONS = {"办理更换": STATUS_REPLACED, "恢复正常": STATUS_NORMAL}

# 重复/并发降频被原样拒绝时的固定说法
DERATE_DUPLICATE_MESSAGE = "该通风机已处于降频运行，重复降频不予生效"


def _parse_number(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number


def _parse_window(value: Any) -> list[tuple[time, time]]:
    """解析运行时段，形如 [["08:00", "16:00"]]；空值表示全天运行。"""
    if not value:
        return [(time.min, time.max)]
    windows: list[tuple[time, time]] = []
    for start_text, end_text in value:
        windows.append((time.fromisoformat(start_text), time.fromisoformat(end_text)))
    return windows


def _intersect_minutes(start: datetime, end: datetime, windows: list[tuple[time, time]]) -> float:
    """计算 [start, end) 落在运行时段内的分钟数，跨天逐日切。"""
    if end <= start:
        return 0.0
    total = 0.0
    cursor = datetime.combine(start.date(), time.min)
    day_end = cursor + timedelta(days=1)
    while cursor < end:
        seg_start = max(start, cursor)
        seg_end = min(end, day_end)
        for win_start, win_end in windows:
            w_start = datetime.combine(cursor.date(), win_start)
            w_end = datetime.combine(cursor.date(), win_end)
            overlap = (min(seg_end, w_end) - max(seg_start, w_start)).total_seconds()
            if overlap > 0:
                total += overlap / 60
        cursor = day_end
        day_end = cursor + timedelta(days=1)
    return total


class VentilationService:
    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        # 按设备加锁：同一台设备的状态流转串行化，不同设备互不阻塞
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._clock = clock or datetime.now
        if not store.rows(THRESHOLD_TABLE):
            self._seed_thresholds()

    # ---------- 阈值版本 ----------

    def _seed_thresholds(self) -> None:
        now = self._clock()
        store.rows(THRESHOLD_TABLE).append(
            {
                "id": 1,
                "版本号": 1,
                "故障阈值": DEFAULT_FAULT_RATIO,
                "降频阈值": DEFAULT_DERATE_RATIO,
                "生效时间": now.isoformat(timespec="seconds"),
                "当前生效": True,
                "调整说明": "系统初始阈值：低于额定风量三成故障、低于五成降频",
            }
        )

    def current_thresholds(self) -> dict[str, Any]:
        rows = store.rows(THRESHOLD_TABLE)
        return next(row for row in rows if row.get("当前生效"))

    def list_thresholds(self) -> list[dict[str, Any]]:
        return store.rows(THRESHOLD_TABLE)

    def adjust_thresholds(
        self, fault_ratio: float, derate_ratio: float, reason: str
    ) -> tuple[dict[str, Any] | None, str]:
        """调整阈值：校验通过后新版本生效，旧版本保留快照，已归档台账不重算。"""
        error = validate_thresholds(fault_ratio, derate_ratio)
        if error:
            return None, error
        rows = store.rows(THRESHOLD_TABLE)
        with self._locks_guard:
            current = next(row for row in rows if row.get("当前生效"))
            current["当前生效"] = False
            version = {
                "id": max((int(row["id"]) for row in rows), default=0) + 1,
                "版本号": int(current["版本号"]) + 1,
                "故障阈值": fault_ratio,
                "降频阈值": derate_ratio,
                "生效时间": self._clock().isoformat(timespec="seconds"),
                "当前生效": True,
                "调整说明": reason or "人工调整判定阈值",
            }
            rows.append(version)
        self._write_log(None, "阈值调整", None, None, f"第 {version['版本号']} 版：故障<{fault_ratio:.0%}、降频<{derate_ratio:.0%}；历史台账不重算")
        return version, f"阈值已更新为第 {version['版本号']} 版，历史归档保持原口径"

    def _thresholds_at(self, moment: datetime) -> dict[str, Any]:
        """取某一时刻适用的阈值版本（按生效时间回溯）。"""
        versions = sorted(store.rows(THRESHOLD_TABLE), key=lambda row: row["生效时间"])
        chosen = versions[0]
        for row in versions:
            if datetime.fromisoformat(row["生效时间"]) <= moment:
                chosen = row
        return chosen

    # ---------- 设备档案 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("设备编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rated = _parse_number(values.get("额定风量"))
        if rated is None or rated <= 0:
            return None, ["额定风量必须是大于 0 的数字，没有额定风量的设备不许保存"]
        # 运行频率合理范围：在统一口径内允许按设备收窄
        lower = _parse_number(values.get("频率下限"))
        upper = _parse_number(values.get("频率上限"))
        lower = FREQUENCY_MIN if lower is None else lower
        upper = FREQUENCY_MAX if upper is None else upper
        if not (FREQUENCY_MIN <= lower <= upper <= FREQUENCY_MAX):
            return None, [f"运行频率合理范围必须落在 {FREQUENCY_MIN:g}~{FREQUENCY_MAX:g}Hz 内，且下限不大于上限"]
        rows = store.rows(MODULE)
        if any(str(row.get("设备编号")) == str(values.get("设备编号")).strip() for row in rows):
            return None, ["设备编号已存在，不能重复登记"]
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["额定风量"] = rated
        entry["频率下限"] = lower
        entry["频率上限"] = upper
        entry["运行频率"] = _parse_number(values.get("运行频率")) or lower
        for optional in ("电流值", "所属巷道", "上次检修"):
            if str(values.get(optional) or "").strip():
                entry[optional] = values[optional]
        entry["运行时段"] = values.get("运行时段") or [["00:00", "23:59"]]
        entry["实测风量"] = None
        entry["status"] = STATUS_NORMAL
        entry["pending"] = True
        entry["abnormal"] = False
        entry["异常起于"] = None
        rows.append(entry)
        self._write_log(entry["id"], "登记设备", None, STATUS_NORMAL, f"额定风量 {rated:g}，频率合理范围 {lower:g}~{upper:g}Hz")
        return entry, []

    def _lock_for(self, entry_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(entry_id, threading.Lock())

    # ---------- 状态流转（人工 / 实测两个入口共用） ----------

    def run_action(
        self, entry_id: int, action: str, *, measured_ratio: float | None = None
    ) -> tuple[dict[str, Any] | None, str, int]:
        """执行人工动作；若同时带实测比例，则实测判定与人工动作取严。

        返回 (设备, 说明, http状态)；重复/并发降频被拒时 http 状态为 409，调用方原样拒绝。
        """
        action = (action or "").strip()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"通风设备 {entry_id} 不存在或已归档", 404
        if action not in ACTION_SEVERITY and action not in ADMIN_ACTIONS:
            return None, f"动作「{action}」不属于通风系统可执行范围", 400

        lock = self._lock_for(entry_id)
        with lock:
            # 加锁后复查：两个并发降频只有先到的能穿过这道门
            if action == "降频运行" and entry["status"] == STATUS_DERATED:
                return None, DERATE_DUPLICATE_MESSAGE, 409

            before = entry["status"]
            if action in ADMIN_ACTIONS:
                target = ADMIN_ACTIONS[action]
            else:
                target = ACTION_SEVERITY[action]
                # 实测判定与人工动作结论打架时，取更严重的一档（规则只此一份）
                if measured_ratio is not None:
                    measured_status = classify_by_ratio(measured_ratio, self.current_thresholds())
                    target = stricter(target, measured_status)
                # 已处于更严重档位时，人工降级动作不允许把设备往回拉
                if before in (STATUS_DERATED, STATUS_FAULT) and stricter(before, target) != target:
                    return None, f"设备当前为「{before}」，比请求的「{target}」更严重，按取严规则不可回退", 409
                if before == STATUS_REPLACED:
                    return None, "已更换的设备不再参与降频/故障流转", 409
                if target == before:
                    # 故障状态再点降频：取严后仍是故障，不属于重复降频，明确告知
                    return None, f"按取严规则设备保持「{before}」，无需重复操作", 409

            self._apply_status(entry, target)
            self._write_log(
                entry_id,
                action,
                before,
                target,
                f"人工动作「{action}」"
                + (f"，实测比例 {measured_ratio:.0%} 取严" if measured_ratio is not None else ""),
            )
            return entry, f"通风设备已{action}（当前状态：{target}）", 200

    def judge_by_airflow(
        self, entry_id: int, measured_airflow: float, measured_at: datetime | None = None
    ) -> tuple[dict[str, Any] | None, str, int]:
        """实测风量入口：按唯一一份规则判档；与现状打架时同样取严。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"通风设备 {entry_id} 不存在或已归档", 404
        if measured_airflow is None or measured_airflow < 0:
            return None, "实测风量必须是不小于 0 的数字", 400
        moment = measured_at or self._clock()
        with self._lock_for(entry_id):
            if entry["status"] == STATUS_REPLACED:
                return None, "已更换的设备不再参与风量判定", 409
            thresholds = self._thresholds_at(moment)
            ratio = measured_airflow / float(entry["额定风量"])
            verdict = classify_by_ratio(ratio, thresholds)
            before = entry["status"]
            target = verdict if before not in (STATUS_DERATED, STATUS_FAULT) else stricter(before, verdict)
            entry["实测风量"] = measured_airflow
            entry["最近实测时刻"] = moment.isoformat(timespec="seconds")
            if target != before:
                self._apply_status(entry, target)
            self._write_log(
                entry_id,
                "实测风量上报",
                before,
                target,
                f"实测 {measured_airflow:g} / 额定 {float(entry['额定风量']):g} = {ratio:.0%}"
                f"，适用第 {thresholds['版本号']} 版阈值，判定「{verdict}」",
            )
            return entry, f"实测风量为额定的 {ratio:.0%}，判定：{target}", 200

    def _apply_status(self, entry: dict[str, Any], target: str) -> None:
        before = entry["status"]
        now = self._clock()
        # 离开异常档：把本轮异常时段按运行时段结清，并挂到当天台账（当天可能尚未归档）
        if before in (STATUS_DERATED, STATUS_FAULT) and target not in (STATUS_DERATED, STATUS_FAULT):
            if entry.get("异常起于"):
                start = datetime.fromisoformat(entry["异常起于"])
                self._accrue(entry, start, now)
            entry["异常起于"] = None
        # 进入异常档：记录起点
        if target in (STATUS_DERATED, STATUS_FAULT) and before not in (STATUS_DERATED, STATUS_FAULT):
            entry["异常起于"] = now.isoformat(timespec="seconds")
        entry["status"] = target
        entry["pending"] = target != STATUS_REPLACED
        entry["abnormal"] = target in (STATUS_DERATED, STATUS_FAULT)

    # ---------- 欠风台账 ----------

    def _accrue(self, entry: dict[str, Any], start: datetime, end: datetime) -> None:
        """把一段异常时间按运行时段拆分，累计到各自然日台账的欠风统计里。"""
        windows = _parse_window(entry.get("运行时段"))
        day = start.date()
        last_day = end.date()
        thresholds = self._thresholds_at(start)
        while day <= last_day:
            day_start = max(start, datetime.combine(day, time.min))
            day_end = min(end, datetime.combine(day + timedelta(days=1), time.min))
            minutes = _intersect_minutes(day_start, day_end, windows)
            if minutes > 0:
                ledger = self._ledger_row(entry, day, thresholds)
                ledger["欠风分钟数"] = round(float(ledger["欠风分钟数"]) + minutes, 1)
            day += timedelta(days=1)

    def _ledger_row(
        self, entry: dict[str, Any], day: date_cls, thresholds: dict[str, Any]
    ) -> dict[str, Any]:
        rows = store.rows(LEDGER_TABLE)
        key = (entry["id"], day.isoformat())
        for row in rows:
            if (row["设备id"], row["日期"]) == key:
                return row
        row = {
            "id": max((int(item["id"]) for item in rows), default=0) + 1,
            "设备id": entry["id"],
            "设备编号": entry["设备编号"],
            "日期": day.isoformat(),
            "欠风分钟数": 0.0,
            # 归档时钉死当时那套阈值：日后翻旧记录按它还原，阈值调整不重算
            "阈值版本号": thresholds["版本号"],
            "阈值快照": {"故障阈值": thresholds["故障阈值"], "降频阈值": thresholds["降频阈值"]},
            "已归档": False,
        }
        rows.append(row)
        return row

    def close_day(self, day: date_cls | None = None) -> tuple[list[dict[str, Any]], str]:
        """归档某日台账：先把仍在异常中的设备按运行时段结清到该日，再冻结，之后阈值变动不重算。"""
        day = day or (self._clock() - timedelta(minutes=1)).date()
        cutoff = datetime.combine(day + timedelta(days=1), time.min)
        archived: list[dict[str, Any]] = []
        for entry in store.rows(MODULE):
            if entry.get("异常起于"):
                start = datetime.fromisoformat(entry["异常起于"])
                if start < cutoff and entry["status"] in (STATUS_DERATED, STATUS_FAULT):
                    self._accrue(entry, start, cutoff)
                    # 跨天异常：结清旧账后从新一天零点重新计时
                    if start.date() < day:
                        entry["异常起于"] = cutoff.isoformat(timespec="seconds")
            for row in store.rows(LEDGER_TABLE):
                if row["设备id"] == entry["id"] and row["日期"] == day.isoformat():
                    row["已归档"] = True
                    archived.append(row)
        self._write_log(None, "台账归档", None, None, f"{day.isoformat()} 通风台账已冻结，共 {len(archived)} 台次；日后阈值调整不重算")
        return archived, f"{day.isoformat()} 已归档 {len(archived)} 台次，历史口径冻结"

    def list_ledger(
        self, *, device_id: int | None = None, day: str | None = None, include_archived: bool = True
    ) -> list[dict[str, Any]]:
        rows = store.rows(LEDGER_TABLE)
        if device_id is not None:
            rows = [row for row in rows if row["设备id"] == device_id]
        if day:
            rows = [row for row in rows if row["日期"] == day]
        if not include_archived:
            rows = [row for row in rows if not row["已归档"]]
        return sorted(rows, key=lambda row: (row["日期"], row["设备id"]))

    def replay_ledger(self, ledger_id: int, version_id: int | None = None) -> tuple[dict[str, Any] | None, str]:
        """翻旧记录：归档台账按钉死的当时阈值还原；欠风分钟数绝不重算。

        仅用于"如果用另一套阈值，这一天会被判成哪一档"的还原对照，不改写原统计。
        """
        ledger = next((row for row in store.rows(LEDGER_TABLE) if int(row["id"]) == ledger_id), None)
        if ledger is None:
            return None, f"台账记录 {ledger_id} 不存在"
        versions = {int(row["id"]): row for row in store.rows(THRESHOLD_TABLE)}
        snapshot_version = next(
            (row for row in versions.values() if int(row["版本号"]) == ledger["阈值版本号"]),
            None,
        )
        result = dict(ledger)
        result["还原说明"] = (
            f"按归档时第 {ledger['阈值版本号']} 版阈值还原"
            f"（故障<{ledger['阈值快照']['故障阈值']:.0%}、"
            f"降频<{ledger['阈值快照']['降频阈值']:.0%}），欠风 {ledger['欠风分钟数']:g} 分钟保持不变"
        )
        result["当时阈值"] = snapshot_version
        if version_id is not None:
            other = versions.get(version_id)
            if other is None:
                return None, f"阈值版本 {version_id} 不存在"
            result["对照版本"] = {
                "版本号": other["版本号"],
                "故障阈值": other["故障阈值"],
                "降频阈值": other["降频阈值"],
                "说明": "仅作判定档对照，台账欠风统计不重算、不改写",
            }
        return result, result["还原说明"]

    # ---------- 运行日志 ----------

    def _write_log(
        self, entry_id: int | None, action: str, before: str | None, after: str | None, detail: str
    ) -> None:
        rows = store.rows(LOG_TABLE)
        rows.append(
            {
                "id": max((int(row["id"]) for row in rows), default=0) + 1,
                "设备id": entry_id,
                "设备编号": store.find(MODULE, entry_id)["设备编号"] if entry_id else None,
                "动作": action,
                "前状态": before,
                "后状态": after,
                "发生时刻": self._clock().isoformat(timespec="seconds"),
                "详情": detail,
            }
        )

    def list_logs(self, device_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows(LOG_TABLE)
        if device_id is not None:
            rows = [row for row in rows if row["设备id"] == device_id]
        return sorted(rows, key=lambda row: int(row["id"]), reverse=True)
