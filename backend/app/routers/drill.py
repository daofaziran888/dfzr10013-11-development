"""多维下钻分析接口：维护下钻路径，覆盖开始下钻、完成分析、归档路径等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.drill import DrillService

router = APIRouter(prefix="/api/drill", tags=["多维下钻分析"])

service = DrillService()

LIST_FIELDS = ["路径编号", "起点指标", "下钻维度", "下钻层级", "筛选条件", "结果展示", "分析时间", "路径状态"]
STATUSES = ["已创建", "分析中", "已完成", "已归档"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按路径编号检索"),
    status: str | None = Query(default=None, description="已创建、分析中、已完成、已归档"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按路径编号与状态过滤多维下钻分析列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条下钻路径明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"下钻路径 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条下钻路径，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="下钻路径已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条下钻路径执行开始下钻、完成分析、归档路径；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/consume", response_model=ActionResult)
def consume_materialized(entry_id: int, payload: EntryPayload) -> ActionResult:
    """消费分析物化分区数据集：记录分区水位与指标定义，父级结果与叶级贡献同源返回。"""
    result, message = service.consume_materialized(entry_id, payload.values)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出多维下钻分析清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "drill", "total": total, "items": items}
