"""通风系统接口：维护通风设备，覆盖实测风量判定、降频运行、故障停机、办理更换等动作。

降频重复提交（含并发）按业务语义返回 409，调用方原样拒绝、不做任何状态改动。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.ventilation import VentilationService

router = APIRouter(prefix="/api/ventilation", tags=["通风系统"])

service = VentilationService()

LIST_FIELDS = ["设备编号", "设备类型", "额定风量", "运行频率", "实测风量", "所属巷道", "上次检修", "设备状态"]
STATUSES = ["正常", "降频运行", "故障停机", "已更换"]
CREATE_FIELDS = [
    "设备编号",
    "设备类型",
    "额定风量",
    "运行频率",
    "频率下限",
    "频率上限",
    "电流值",
    "所属巷道",
    "上次检修",
    "运行时段",
]


class AirflowReport(BaseModel):
    """实测风量上报。"""

    measured_airflow: float
    measured_at: str | None = None


class ThresholdPayload(BaseModel):
    """阈值调整。"""

    fault_ratio: float
    derate_ratio: float
    reason: str | None = None


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按设备编号检索"),
    status: str | None = Query(default=None, description="正常、降频运行、故障停机、已更换"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按设备编号与状态过滤通风系统列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/thresholds")
def list_thresholds() -> dict[str, Any]:
    """查看阈值版本链：当前版与历史版都在，旧记录靠版本号回溯。"""
    return {"current": service.current_thresholds(), "versions": service.list_thresholds()}


@router.put("/thresholds", response_model=ActionResult)
def adjust_thresholds(payload: ThresholdPayload) -> ActionResult:
    """调整判定阈值：新版本生效，已归档台账不重算；两档边界重叠会被拒绝。"""
    version, message = service.adjust_thresholds(payload.fault_ratio, payload.derate_ratio, payload.reason or "")
    if version is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=version)


@router.get("/ledger")
def list_ledger(
    device_id: int | None = Query(default=None, description="按设备过滤"),
    day: str | None = Query(default=None, description="按日期过滤 YYYY-MM-DD"),
    include_archived: bool = True,
) -> dict[str, Any]:
    """通风台账欠风统计：每行带归档时钉死的阈值版本，翻旧账按当时口径还原。"""
    items = service.list_ledger(device_id=device_id, day=day, include_archived=include_archived)
    return {"total": len(items), "items": items}


@router.post("/ledger/close", response_model=ActionResult)
def close_ledger_day(day: str | None = None) -> ActionResult:
    """归档某天台账：结清当日欠风分钟数并冻结，之后阈值调整不再影响这一天。"""
    target_day = date.fromisoformat(day) if day else None
    items, message = service.close_day(target_day)
    return ActionResult(ok=True, message=message, entry={"items": items})


@router.get("/ledger/{ledger_id}/replay", response_model=ActionResult)
def replay_ledger(ledger_id: int, version_id: int | None = None) -> ActionResult:
    """按归档时那套阈值还原旧台账；可传 version_id 仅做判定档对照，统计不重算。"""
    result, message = service.replay_ledger(ledger_id, version_id)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/logs")
def list_logs(device_id: int | None = Query(default=None, description="按设备过滤")) -> dict[str, Any]:
    """运行日志：设备状态变化、阈值调整、台账归档都落在这里。"""
    items = service.list_logs(device_id)
    return {"total": len(items), "items": items}


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出通风系统清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "ventilation", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条通风设备明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"通风设备 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条通风设备；没有额定风量（或不是正数）一律不许保存。"""
    values = {key: value for key, value in payload.values.items() if key in CREATE_FIELDS}
    entry, errors = service.create_entry(values)
    if errors:
        return ActionResult(ok=False, message="；".join(errors))
    return ActionResult(ok=True, message="通风设备已登记", entry=entry)


@router.post("/{entry_id}/airflow", response_model=ActionResult)
def report_airflow(entry_id: int, payload: AirflowReport) -> ActionResult:
    """实测风量上报入口：与人工动作共用同一份判定规则，取更严的一档。"""
    measured_at = None
    if payload.measured_at:
        from datetime import datetime

        try:
            measured_at = datetime.fromisoformat(payload.measured_at)
        except ValueError:
            return ActionResult(ok=False, message="measured_at 需为 ISO 时间格式")
    entry, message, status = service.judge_by_airflow(entry_id, payload.measured_airflow, measured_at)
    if entry is None:
        raise HTTPException(status_code=status, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条通风设备执行降频运行、故障停机、办理更换；不允许的动作会被拦下并说明原因。

    同一台设备并发两次降频：先到的生效，后到的原样 409 拒绝。
    若同时提交 measured_airflow，则人工动作与实测判定取严。
    """
    action = str(payload.values.get("action") or "").strip()
    measured_ratio: float | None = None
    if payload.values.get("measured_airflow") is not None:
        try:
            measured_airflow = float(payload.values["measured_airflow"])
        except (TypeError, ValueError):
            return ActionResult(ok=False, message="measured_airflow 必须是数字")
        entry = service.get_entry(entry_id)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"通风设备 {entry_id} 不存在或已归档")
        measured_ratio = measured_airflow / float(entry["额定风量"])
    entry, message, status = service.run_action(entry_id, action, measured_ratio=measured_ratio)
    if entry is None:
        # 服务层已区分 404（设备不存在）/409（重复降频、取严回退等冲突）/400（动作非法）
        raise HTTPException(status_code=status, detail=message)
    return ActionResult(ok=True, message=message, entry=entry)
