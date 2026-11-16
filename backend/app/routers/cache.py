"""分析缓存管理接口：维护缓存策略，覆盖启用缓存、暂停缓存、关闭缓存等动作。

同前缀下挂了分析物化能力：给缓存对象定义聚合口径、消费事实增量与撤销更正事件、
读取可追溯的分区数据集与原始贡献明细、重建与一致性核对。
物化相关路径都声明在 `/{entry_id}` 之前，避免被路径参数抢占。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.cache import CacheMaterializeService, CacheService

router = APIRouter(prefix="/api/cache", tags=["分析缓存管理"])

service = CacheService()
materialize_service = CacheMaterializeService()

LIST_FIELDS = ["策略编号", "缓存对象", "缓存时间", "失效条件", "预加载", "容量上限", "命中统计", "策略状态"]
STATUSES = ["草稿", "已启用", "已暂停", "已关闭"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、已启用、已暂停、已关闭"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤分析缓存管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


# ---------------------------------------------------------------- 分析物化


@router.get("/materialize/objects")
def list_materialized_objects() -> dict[str, Any]:
    """列出已定义物化口径的缓存对象及其水位、分区与污染概况。"""
    return {"items": materialize_service.list_objects()}


@router.post("/materialize/define", response_model=ActionResult)
def define_materialization(payload: EntryPayload) -> ActionResult:
    """给缓存对象定义日期分区、分组维度与指标聚合含义；重复定义会作废旧结果。"""
    view, message = materialize_service.define(payload.values)
    if view is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=view)


@router.post("/materialize/{object_id}/events", response_model=ActionResult)
def ingest_events(object_id: str, payload: EntryPayload) -> ActionResult:
    """消费一批事实事件（增量、更正、删除）；整批校验通过才成套提交。"""
    events = payload.values.get("事件")
    if not isinstance(events, list):
        return ActionResult(ok=False, message="请在 values.事件 里给出事实事件数组")
    report, message = materialize_service.ingest(object_id, events)
    if report is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=report)


@router.get("/materialize/{object_id}/dataset")
def materialized_dataset(object_id: str) -> dict[str, Any]:
    """读取物化分区数据集：分区统计、分组指标与污染标记。"""
    view, error = materialize_service.dataset(object_id)
    if view is None:
        raise HTTPException(status_code=404, detail=error)
    return view


@router.get("/materialize/{object_id}/partitions/{partition_key}")
def read_materialized_partition(object_id: str, partition_key: str) -> dict[str, Any]:
    """单分区读取：受污染分区不作为命中结果返回，并说明原因。"""
    view, error = materialize_service.read_partition(object_id, partition_key)
    if view is None:
        raise HTTPException(status_code=404, detail=error)
    return view


@router.get("/materialize/{object_id}/partitions/{partition_key}/contributions")
def read_partition_contributions(object_id: str, partition_key: str) -> dict[str, Any]:
    """分区原始贡献明细：每条事实修订贡献了什么，可追溯、可核对。"""
    view, error = materialize_service.contributions(object_id, partition_key)
    if view is None:
        raise HTTPException(status_code=404, detail=error)
    return view


@router.post("/materialize/{object_id}/rebuild", response_model=ActionResult)
def rebuild_materialization(object_id: str, payload: EntryPayload) -> ActionResult:
    """重建物化结果：缺省按事件日志重放，也可传入给定事实集合；失败保留旧结果。"""
    facts = payload.values.get("事实")
    if facts is not None and not isinstance(facts, list):
        return ActionResult(ok=False, message="values.事实 必须是事实事件数组")
    report, message = materialize_service.rebuild(object_id, facts)
    if report is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=report)


@router.get("/materialize/{object_id}/verify")
def verify_materialization(object_id: str) -> dict[str, Any]:
    """一致性核对：增量物化结果应与同一事实集合的全量聚合一致。"""
    report, error = materialize_service.verify(object_id)
    if report is None:
        raise HTTPException(status_code=404, detail=error)
    return report


# ---------------------------------------------------------------- 缓存策略


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出分析缓存管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cache", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条缓存策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"缓存策略 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条缓存策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="缓存策略已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条缓存策略执行启用缓存、暂停缓存、关闭缓存；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
