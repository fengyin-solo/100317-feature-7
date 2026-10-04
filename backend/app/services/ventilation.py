"""通风系统业务规则：风量判定、状态流转、欠风统计与阈值版本都收在这里。

判定档位只有三档，边界不允许重叠（比例恰等时归入更轻的一档）：

- 实测风量 < 额定风量 × 故障比例（默认 30%）→ 故障停机
- 额定风量 × 故障比例 ≤ 实测风量 < 额定风量 × 降频比例（默认 50%）→ 降频运行
- 实测风量 ≥ 额定风量 × 降频比例 → 正常

规则只有 ``classify`` 一份：值班员手动提交降频、运行时段自动判定两个入口
都经过它；两个入口判定打架时按 ``SEVERITY`` 取更严重的一档。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from app.store import store

MODULE = "ventilation"
LEDGER_MODULE = "ventilation_ledger"
LOG_MODULE = "ventilation_log"
THRESHOLD_MODULE = "ventilation_thresholds"
SEGMENT_MODULE = "ventilation_segments"

REQUIRED_FIELDS = ["设备编号", "设备类型", "额定风量"]
OPTIONAL_FIELDS = ["所属巷道", "上次检修"]

# 状态机里保留「已更换」作为设备退役终态；三档判定只管在役设备。
STATUS_NORMAL = "正常"
STATUS_DERATED = "降频运行"
STATUS_FAULT = "故障停机"
STATUS_REPLACED = "已更换"
JUDGED_STATUSES = [STATUS_NORMAL, STATUS_DERATED, STATUS_FAULT]
SEVERITY = {STATUS_NORMAL: 0, STATUS_DERATED: 1, STATUS_FAULT: 2}
ACTION_RULES = {"降频运行": STATUS_DERATED, "故障停机": STATUS_FAULT, "办理更换": STATUS_REPLACED}

# 默认阈值：阈值表为空时按这套初始化；改阈值只会追加新版本，不改老版本。
DEFAULT_FAULT_RATIO = 0.30
DEFAULT_DERATE_RATIO = 0.50
DEFAULT_RATED_FREQUENCY = 50.0
DEFAULT_EPOCH = "1970-01-01T00:00:00"

# 运行时段最长允许跨度，防止一次上报把一整年都算进欠风。
MAX_SEGMENT_MINUTES = 24 * 60


def _to_float(value: Any) -> float | None:
    """把表单里的字符串数值转成 float；空串、非数字一律按未提供处理。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_ts(value: Any) -> datetime | None:
    """解析 ``YYYY-mm-ddTHH:MM:SS`` 形式的时刻，非法输入返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def classify(rated_flow: float, measured_flow: float, fault_ratio: float, derate_ratio: float) -> str:
    """唯一的风量判定规则。所有入口都走这里，避免两处口径漂移。"""
    if rated_flow <= 0:
        # 没有可用的额定风量就无法判定，交回调用方按缺字段处理。
        return ""
    ratio = measured_flow / rated_flow
    if ratio < fault_ratio:
        return STATUS_FAULT
    if ratio < derate_ratio:
        return STATUS_DERATED
    return STATUS_NORMAL


class _KeyedLocks:
    """按设备编号取锁：同一台设备的并发动作串行化，不同设备互不阻塞。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[int, threading.Lock] = {}

    def get(self, key: int) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock


class VentilationService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._device_locks = _KeyedLocks()

    # ---------- 阈值版本 ----------

    def _ensure_tables(self) -> None:
        for name in (THRESHOLD_MODULE, LEDGER_MODULE, LOG_MODULE, SEGMENT_MODULE):
            store.rows(name)
        versions = store.rows(THRESHOLD_MODULE)
        if not versions:
            versions.append({
                "id": 1,
                "version": 1,
                "故障比例": DEFAULT_FAULT_RATIO,
                "降频比例": DEFAULT_DERATE_RATIO,
                "生效时间": DEFAULT_EPOCH,
                "备注": "系统初始阈值",
            })

    def get_current_threshold(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_tables()
            return dict(store.rows(THRESHOLD_MODULE)[-1])

    def _threshold_at(self, moment: datetime) -> dict[str, Any]:
        """取某一时刻生效的阈值版本——翻旧记录就靠它还原当时口径。"""
        self._ensure_tables()
        stamp = moment.strftime("%Y-%m-%dT%H:%M:%S")
        chosen = store.rows(THRESHOLD_MODULE)[0]
        for version in store.rows(THRESHOLD_MODULE):
            if str(version["生效时间"]) <= stamp:
                chosen = version
        return dict(chosen)

    def update_threshold(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        fault_ratio = _to_float(values.get("故障比例"))
        derate_ratio = _to_float(values.get("降频比例"))
        if fault_ratio is None or derate_ratio is None:
            return None, "故障比例与降频比例都必须是 0 到 1 之间的数字"
        if not 0 < fault_ratio < derate_ratio <= 1:
            return None, "阈值必须满足 0 < 故障比例 < 降频比例 ≤ 1，两档边界不许重叠"
        with self._lock:
            self._ensure_tables()
            versions = store.rows(THRESHOLD_MODULE)
            current = versions[-1]
            if float(current["故障比例"]) == fault_ratio and float(current["降频比例"]) == derate_ratio:
                return None, "新阈值与当前阈值一致，无需调整"
            snapshot = {
                "id": max(int(row["id"]) for row in versions) + 1,
                "version": int(current["version"]) + 1,
                "故障比例": fault_ratio,
                "降频比例": derate_ratio,
                "生效时间": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "备注": str(values.get("备注") or "").strip(),
            }
            versions.append(snapshot)
            self._write_log(
                device_id=None,
                event="阈值调整",
                detail=(
                    f"故障比例 {float(current['故障比例']):.2f} → {fault_ratio:.2f}；"
                    f"降频比例 {float(current['降频比例']):.2f} → {derate_ratio:.2f}，"
                    f"新版本 v{int(snapshot['version'])} 即刻生效，已归档日期不重算"
                ),
            )
            return dict(snapshot), "阈值已调整并生成新版本，历史台账保持原口径"

    # ---------- 设备台账 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            self._ensure_tables()
            rows = store.rows(MODULE)
            if keyword:
                rows = [row for row in rows if keyword in str(row.get("设备编号", ""))]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            total = len(rows)
            start = max(page - 1, 0) * size
            return [self._decorate(dict(row)) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self._lock:
            self._ensure_tables()
            row = store.find(MODULE, entry_id)
            return self._decorate(dict(row)) if row else None

    def _decorate(self, entry: dict[str, Any]) -> dict[str, Any]:
        """给设备记录补上判定衍生字段：当前档位、实测占比、频率合理范围。"""
        threshold = self.get_current_threshold()
        rated_flow = _to_float(entry.get("额定风量"))
        rated_freq = _to_float(entry.get("额定频率")) or DEFAULT_RATED_FREQUENCY
        measured = _to_float(entry.get("实测风量"))
        entry["额定风量"] = rated_flow if rated_flow is not None else entry.get("额定风量")
        entry["额定频率"] = rated_freq
        entry["降频比例"] = float(threshold["降频比例"])
        entry["故障比例"] = float(threshold["故障比例"])
        entry["频率合理范围"] = (
            f"≥{rated_freq * float(threshold['降频比例']):.1f}Hz 正常；"
            f"{rated_freq * float(threshold['故障比例']):.1f}–"
            f"{rated_freq * float(threshold['降频比例']):.1f}Hz 降频；"
            f"<{rated_freq * float(threshold['故障比例']):.1f}Hz 故障"
        )
        if rated_flow is not None and measured is not None and rated_flow > 0:
            entry["风量占比"] = round(measured / rated_flow, 4)
            entry["当前判定档位"] = classify(
                rated_flow, measured,
                float(threshold["故障比例"]), float(threshold["降频比例"]),
            )
        else:
            entry["风量占比"] = None
            entry["当前判定档位"] = None
        entry.setdefault("欠风累计分钟", 0)
        return entry

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rated_flow = _to_float(values.get("额定风量"))
        if rated_flow is None or rated_flow <= 0:
            return None, ["额定风量必须是大于 0 的数字，没有额定风量的设备不允许保存"]
        rated_freq = _to_float(values.get("额定频率"))
        if rated_freq is None:
            rated_freq = DEFAULT_RATED_FREQUENCY
        if rated_freq <= 0:
            return None, ["额定频率必须是大于 0 的数字"]
        with self._lock:
            self._ensure_tables()
            rows = store.rows(MODULE)
            code = str(values.get("设备编号") or "").strip()
            if any(str(row.get("设备编号")) == code for row in rows):
                return None, [f"设备编号 {code} 已存在"]
            entry: dict[str, Any] = {
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "设备编号": code,
                "设备类型": str(values.get("设备类型") or "").strip(),
                "额定风量": rated_flow,
                "额定频率": rated_freq,
                "所属巷道": str(values.get("所属巷道") or "").strip(),
                "上次检修": str(values.get("上次检修") or "").strip(),
                "实测风量": None,
                "运行频率": rated_freq,
                "欠风累计分钟": 0,
                "降频开始时间": None,
                "status": STATUS_NORMAL,
                "pending": True,
                "abnormal": False,
            }
            rows.append(entry)
            self._write_log(entry["id"], "设备登记", f"登记通风设备 {code}，额定风量 {rated_flow:g}")
            return self._decorate(dict(entry)), []

    # ---------- 手动动作与自动判定 ----------

    def run_action(self, entry_id: int, action: str, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于通风系统可执行范围"
        target = ACTION_RULES[action]
        with self._device_locks.get(entry_id):
            with self._lock:
                self._ensure_tables()
                entry = store.find(MODULE, entry_id)
                if entry is None:
                    return None, f"通风设备 {entry_id} 不存在或已归档"

                if target == STATUS_REPLACED:
                    return self._apply_status(entry, STATUS_REPLACED, "值班员办理更换")

                # 降频/故障都先走同一份判定规则；实测风量缺失时才退化为人工指定档。
                threshold = self.get_current_threshold()
                measured = _to_float(values.get("实测风量", entry.get("实测风量")))
                rated_flow = _to_float(entry.get("额定风量"))
                judged = ""
                if measured is not None and rated_flow is not None:
                    judged = classify(
                        rated_flow, measured,
                        float(threshold["故障比例"]), float(threshold["降频比例"]),
                    )

                # 并发保护：已经在降频（或更严重的故障）时，重复降频原样拒绝。
                if entry["status"] in (STATUS_DERATED, STATUS_FAULT):
                    return None, (
                        f"设备已处于{entry['status']}，本次降频请求拒绝（同一设备并发降频只生效一次）"
                    )

                # 判定打架取更严重的一档：人工意图 target 与规则判定 judged 取严。
                if judged:
                    final = judged if SEVERITY[judged] >= SEVERITY[target] else target
                    reason = f"人工请求{target}；实测风量 {measured:g}（占额定 {measured / rated_flow:.0%}）规则判定为{judged}，按取严规则执行{final}"
                else:
                    final = target
                    reason = f"人工请求{target}；暂无可用实测风量，按人工指令执行"
                return self._apply_status(entry, final, reason, measured_flow=measured)

    def report_reading(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """上报一次实时测点读数；自动判定入口，同样走 classify，不另写规则。"""
        measured = _to_float(values.get("实测风量"))
        if measured is None or measured < 0:
            return None, "实测风量必须是不小于 0 的数字"
        moment = _parse_ts(values.get("时刻")) or datetime.now()
        with self._lock:
            self._ensure_tables()
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"通风设备 {entry_id} 不存在或已归档"
            if entry["status"] == STATUS_REPLACED:
                return None, "设备已更换，不再接收测点读数"
            rated_flow = _to_float(entry.get("额定风量"))
            rated_freq = _to_float(entry.get("额定频率")) or DEFAULT_RATED_FREQUENCY
            threshold = self._threshold_at(moment)
            judged = classify(
                rated_flow, measured,
                float(threshold["故障比例"]), float(threshold["降频比例"]),
            )
            entry["实测风量"] = measured
            entry["运行频率"] = round(rated_freq * measured / rated_flow, 2)
            message = f"读数已记录，当前判定档位：{judged}"
            if judged != entry["status"]:
                # 读数驱动只允许按规则档位变化，取严逻辑天然体现在唯一规则里。
                self._apply_status(
                    entry, judged,
                    f"自动判定：实测风量 {measured:g}（占额定 {measured / rated_flow:.0%}）→ {judged}",
                    measured_flow=measured,
                )
                message = f"读数已记录，设备状态由原状态变更为{judged}"
            return self._decorate(dict(entry)), message

    def _apply_status(
        self,
        entry: dict[str, Any],
        target: str,
        reason: str,
        *,
        measured_flow: float | None = None,
    ) -> tuple[dict[str, Any], str]:
        previous = str(entry.get("status"))
        entry["status"] = target
        entry["设备状态"] = target
        entry["pending"] = target != STATUS_REPLACED
        entry["abnormal"] = target in (STATUS_DERATED, STATUS_FAULT)
        if target == STATUS_DERATED:
            entry["降频开始时间"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        else:
            # 欠风时长统一由运行时段累计；离开降频档后清掉降频段起点。
            entry["降频开始时间"] = None
        if measured_flow is not None:
            entry["实测风量"] = measured_flow
        self._write_log(
            int(entry["id"]), "状态变更",
            f"{previous} → {target}。{reason}",
        )
        return self._decorate(dict(entry)), f"通风设备已变更为{target}"

    # ---------- 运行时段与欠风统计 ----------

    def report_segment(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """上报一段运行时段：按当时阈值判定档位，欠风时长按日拆分回写台账。"""
        start = _parse_ts(values.get("开始时间"))
        end = _parse_ts(values.get("结束时间"))
        measured = _to_float(values.get("实测风量"))
        if start is None or end is None:
            return None, "开始时间与结束时间格式应为 YYYY-MM-DD HH:MM:SS"
        if end <= start:
            return None, "结束时间必须晚于开始时间"
        duration_minutes = (end - start).total_seconds() / 60
        if duration_minutes > MAX_SEGMENT_MINUTES:
            return None, f"单个运行时段不能超过 {MAX_SEGMENT_MINUTES} 分钟"
        if measured is None or measured < 0:
            return None, "实测风量必须是不小于 0 的数字"
        with self._lock:
            self._ensure_tables()
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"通风设备 {entry_id} 不存在或已归档"
            rated_flow = _to_float(entry.get("额定风量"))
            # 阈值取时段中点生效的那版：调阈值不回头重算，只影响之后的时段。
            midpoint = start + (end - start) / 2
            threshold = self._threshold_at(midpoint)
            judged = classify(
                rated_flow, measured,
                float(threshold["故障比例"]), float(threshold["降频比例"]),
            )
            deficit_minutes = duration_minutes if judged in (STATUS_DERATED, STATUS_FAULT) else 0

            segments = store.rows(SEGMENT_MODULE)
            segment = {
                "id": max((int(row.get("id", 0)) for row in segments), default=0) + 1,
                "设备id": entry_id,
                "设备编号": entry.get("设备编号"),
                "开始时间": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "结束时间": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "实测风量": measured,
                "判定档位": judged,
                "欠风分钟": deficit_minutes,
                "阈值版本": int(threshold["version"]),
            }
            segments.append(segment)

            archived_days = self._accumulate_ledger(entry, start, end, judged, deficit_minutes, int(threshold["version"]))
            if archived_days:
                return None, f"以下日期已归档，阈值调整后不再重算，请改报其他日期：{'、'.join(archived_days)}"
            if deficit_minutes:
                self._write_log(
                    entry_id, "欠风统计",
                    f"{segment['开始时间']} ~ {segment['结束时间']} 判定为{judged}，"
                    f"欠风 {deficit_minutes:.0f} 分钟已回写通风台账（阈值 v{int(threshold['version'])}）",
                )
            return segment, (
                f"运行时段已记录，判定档位：{judged}，欠风 {deficit_minutes:.0f} 分钟"
                if deficit_minutes else "运行时段已记录，风量正常，无欠风时长"
            )

    def _accumulate_ledger(
        self,
        entry: dict[str, Any],
        start: datetime,
        end: datetime,
        judged: str,
        deficit_minutes: float,
        version: int,
    ) -> list[str]:
        """把欠风分钟按自然日拆分累加到通风台账；已归档日期拒绝写入。"""
        if deficit_minutes <= 0:
            return []
        ledger = store.rows(LEDGER_MODULE)
        # 欠风分钟按自然日切分，跨天时段分别计入对应日期。
        from datetime import timedelta
        pieces: dict[str, float] = {}
        cursor = start
        while cursor < end:
            next_day = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0)
            day_end = min(end, next_day)
            day = cursor.strftime("%Y-%m-%d")
            pieces[day] = pieces.get(day, 0.0) + (day_end - cursor).total_seconds() / 60
            cursor = day_end

        archived: list[str] = []
        for day, minutes in pieces.items():
            existing = next(
                (row for row in ledger
                 if int(row.get("设备id", 0)) == int(entry["id"]) and row.get("日期") == day),
                None,
            )
            if existing and existing.get("已归档"):
                archived.append(day)
                continue
            if existing is None:
                existing = {
                    "id": max((int(row.get("id", 0)) for row in ledger), default=0) + 1,
                    "设备id": int(entry["id"]),
                    "设备编号": entry.get("设备编号"),
                    "日期": day,
                    "降频分钟": 0.0,
                    "故障分钟": 0.0,
                    "欠风分钟": 0.0,
                    "阈值版本": version,
                    "已归档": False,
                }
                ledger.append(existing)
            if judged == STATUS_DERATED:
                existing["降频分钟"] = round(float(existing["降频分钟"]) + minutes, 1)
            else:
                existing["故障分钟"] = round(float(existing["故障分钟"]) + minutes, 1)
            existing["欠风分钟"] = round(float(existing["欠风分钟"]) + minutes, 1)
            existing["阈值版本"] = version

        if not archived:
            total = sum(
                float(row["欠风分钟"]) for row in ledger if int(row.get("设备id", 0)) == int(entry["id"])
            )
            entry["欠风累计分钟"] = round(total, 1)
        return archived

    def list_ledger(
        self,
        *,
        entry_id: int | None = None,
        day: str | None = None,
        archived: bool | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_tables()
            rows = store.rows(LEDGER_MODULE)
            if entry_id is not None:
                rows = [row for row in rows if int(row.get("设备id", 0)) == entry_id]
            if day:
                rows = [row for row in rows if row.get("日期") == day]
            if archived is not None:
                rows = [row for row in rows if bool(row.get("已归档")) == archived]
            return [dict(row) for row in sorted(rows, key=lambda r: (str(r.get("日期")), int(r.get("设备id", 0))))]

    def archive_day(self, day: str) -> tuple[int, str]:
        """归档某一天：冻结台账，之后阈值再调也不给这天重算。"""
        parsed = _parse_ts(day)
        if parsed is None:
            return 0, "归档日期格式应为 YYYY-MM-DD"
        day = parsed.strftime("%Y-%m-%d")
        with self._lock:
            self._ensure_tables()
            count = 0
            for row in store.rows(LEDGER_MODULE):
                if row.get("日期") == day and not row.get("已归档"):
                    row["已归档"] = True
                    count += 1
            if count:
                self._write_log(None, "台账归档", f"{day} 通风台账已归档，共冻结 {count} 条欠风统计，日后阈值调整不再重算")
            return count, (f"{day} 已归档 {count} 条台账" if count else f"{day} 没有可归档的台账记录")

    # ---------- 运行日志与历史还原 ----------

    def list_logs(self, *, entry_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            self._ensure_tables()
            rows = store.rows(LOG_MODULE)
            if entry_id is not None:
                rows = [row for row in rows if row.get("设备id") == entry_id]
            rows = sorted(rows, key=lambda r: int(r.get("id", 0)), reverse=True)
            return [dict(row) for row in rows[:limit]]

    def _write_log(self, device_id: int | None, event: str, detail: str) -> dict[str, Any]:
        logs = store.rows(LOG_MODULE)
        log = {
            "id": max((int(row.get("id", 0)) for row in logs), default=0) + 1,
            "设备id": device_id,
            "事件": event,
            "详情": detail,
            "时间": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        logs.append(log)
        return log

    def reconstruct(self, entry_id: int, moment: Any, measured_flow: Any) -> tuple[dict[str, Any] | None, str]:
        """按指定时刻生效的阈值还原旧记录当时的判定档位。"""
        parsed_moment = _parse_ts(moment)
        if parsed_moment is None:
            return None, "时刻格式应为 YYYY-MM-DD HH:MM:SS"
        measured = _to_float(measured_flow)
        if measured is None or measured < 0:
            return None, "实测风量必须是不小于 0 的数字"
        with self._lock:
            self._ensure_tables()
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"通风设备 {entry_id} 不存在或已归档"
            rated_flow = _to_float(entry.get("额定风量"))
            rated_freq = _to_float(entry.get("额定频率")) or DEFAULT_RATED_FREQUENCY
            threshold = self._threshold_at(parsed_moment)
            judged = classify(
                rated_flow, measured,
                float(threshold["故障比例"]), float(threshold["降频比例"]),
            )
            return {
                "设备编号": entry.get("设备编号"),
                "还原时刻": parsed_moment.strftime("%Y-%m-%dT%H:%M:%S"),
                "适用阈值版本": int(threshold["version"]),
                "当时故障比例": float(threshold["故障比例"]),
                "当时降频比例": float(threshold["降频比例"]),
                "实测风量": measured,
                "额定风量": rated_flow,
                "风量占比": round(measured / rated_flow, 4),
                "当时判定档位": judged,
                "当时运行频率": round(rated_freq * measured / rated_flow, 2),
            }, "已按该时刻生效的阈值还原判定结果"
