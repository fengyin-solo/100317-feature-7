"""通风系统接口：维护通风设备，覆盖风量判定、降频/故障流转、欠风台账与阈值版本。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.ventilation import MODULE, VentilationService

router = APIRouter(prefix="/api/ventilation", tags=["通风系统"])

service = VentilationService()

LIST_FIELDS = ["设备编号", "设备类型", "额定风量", "额定频率", "实测风量", "运行频率", "所属巷道", "设备状态"]
STATUSES = ["正常", "降频运行", "故障停机", "已更换"]


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


@router.get("/thresholds", response_model=dict)
def get_thresholds() -> dict[str, Any]:
    """读取当前生效的风量阈值与频率合理范围口径。"""
    return service.get_current_threshold()


@router.put("/thresholds", response_model=ActionResult)
def update_thresholds(payload: EntryPayload) -> ActionResult:
    """调整判定阈值：只追加新版本，已归档台账不重算。"""
    snapshot, message = service.update_threshold(payload.values)
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.get("/ledger", response_model=list[dict])
def list_ledger(
    entry_id: int | None = Query(default=None, description="按设备过滤"),
    day: str | None = Query(default=None, description="按日期过滤，YYYY-MM-DD"),
    archived: bool | None = Query(default=None, description="是否只看已归档"),
) -> list[dict]:
    """通风台账欠风统计：按设备、日期、归档状态过滤。"""
    return service.list_ledger(entry_id=entry_id, day=day, archived=archived)


@router.post("/ledger/archive", response_model=ActionResult)
def archive_ledger(payload: EntryPayload) -> ActionResult:
    """归档某一天的台账：冻结欠风统计，之后阈值调整不再重算该日。"""
    day = str(payload.values.get("日期") or "").strip()
    count, message = service.archive_day(day)
    return ActionResult(ok=count > 0 or "没有可归档" not in message, message=message)


@router.get("/logs", response_model=list[dict])
def list_logs(
    entry_id: int | None = Query(default=None, description="按设备过滤"),
    limit: int = Query(default=100, le=500),
) -> list[dict]:
    """通风运行日志：设备状态变化、欠风回写、阈值调整都在这里。"""
    return service.list_logs(entry_id=entry_id, limit=limit)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出通风系统清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": MODULE, "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条通风设备明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"通风设备 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条通风设备；没有额定风量（或额定风量非正数）不允许保存。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message="；".join(missing) if len(missing) == 1 and "必须" in missing[0] else f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="通风设备已登记", entry=entry)


@router.post("/{entry_id}/readings", response_model=ActionResult)
def report_reading(entry_id: int, payload: EntryPayload) -> ActionResult:
    """上报实测风量：自动走判定规则，需要降频/停机时直接改设备状态。"""
    entry, message = service.report_reading(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/segments", response_model=ActionResult)
def report_segment(entry_id: int, payload: EntryPayload) -> ActionResult:
    """上报一段运行时段：按当时阈值判档，欠风时长回写通风台账。"""
    entry, message = service.report_segment(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/{entry_id}/reconstruct", response_model=ActionResult)
def reconstruct(
    entry_id: int,
    moment: str = Query(description="要还原到的时刻，YYYY-MM-DD HH:MM:SS"),
    measured_flow: float = Query(description="当时的实测风量"),
) -> ActionResult:
    """翻旧记录：按指定时刻生效的那套阈值还原当时的判定档位。"""
    entry, message = service.reconstruct(entry_id, moment, measured_flow)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条通风设备执行降频运行、故障停机、办理更换。

    降频/故障与实测风量同时提交时走同一份判定规则，打架取更严重的一档；
    设备已处于降频或故障时，重复降频原样拒绝。
    """
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
