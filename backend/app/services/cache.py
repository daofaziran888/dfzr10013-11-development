"""分析缓存管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.materialize import MaterializeError, RebuildError, registry
from app.store import store

MODULE = "cache"
REQUIRED_FIELDS = ["策略编号", "缓存对象", "缓存时间"]
STATUS_ORDER = ["草稿", "已启用", "已暂停", "已关闭"]
ACTION_RULES = {"启用缓存": "草稿", "暂停缓存": "已启用", "关闭缓存": "已暂停"}
NEGATIVE_ACTIONS = []


class CacheService:
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
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
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
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分析缓存管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"缓存策略已{action}"


class CacheMaterializeService:
    """分析物化：给缓存对象定义聚合口径，消费事实事件，输出可追溯的分区数据集。"""

    def _find_policy(self, object_id: str) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if str(row.get("策略编号") or "") == object_id:
                return row
        return None

    def _engine_or_error(self, object_id: str):
        engine = registry.engine(object_id)
        if engine is None:
            return None, f"缓存对象 {object_id or '（空）'} 尚未定义物化口径，先调用物化定义接口"
        return engine, ""

    def define(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        object_id = str(values.get("策略编号") or "").strip()
        policy = self._find_policy(object_id)
        if policy is None:
            return None, f"缓存策略 {object_id or '（空）'} 不存在，先登记缓存策略再定义物化口径"
        try:
            spec, replaced = registry.define(values)
        except MaterializeError as exc:
            return None, str(exc)
        view = {
            "策略编号": spec.object_id,
            "缓存对象": policy.get("缓存对象"),
            "日期分区字段": spec.date_field,
            "分组维度": list(spec.dimensions),
            "指标定义": [metric.definition_view() for metric in spec.metrics],
            "覆盖旧定义": replaced,
        }
        message = "物化口径已定义"
        if replaced:
            message += "；口径变更，旧物化结果已作废，请重新消费事实事件"
        return view, message

    def list_objects(self) -> list[dict[str, Any]]:
        objects = registry.list_objects()
        for item in objects:
            policy = self._find_policy(str(item.get("策略编号") or ""))
            item["缓存对象"] = policy.get("缓存对象") if policy else None
        return objects

    def ingest(self, object_id: str, events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
        engine, error = self._engine_or_error(object_id)
        if engine is None:
            return None, error
        try:
            report = engine.apply(events)
        except MaterializeError as exc:
            return None, f"事件批次未应用：{exc}"
        return report, "事件批次已成套应用"

    def dataset(self, object_id: str) -> tuple[dict[str, Any] | None, str]:
        engine, error = self._engine_or_error(object_id)
        if engine is None:
            return None, error
        return engine.snapshot().dataset_view(), ""

    def read_partition(self, object_id: str, partition_key: str) -> tuple[dict[str, Any] | None, str]:
        engine, error = self._engine_or_error(object_id)
        if engine is None:
            return None, error
        return engine.snapshot().read_partition(partition_key), ""

    def contributions(self, object_id: str, partition_key: str) -> tuple[dict[str, Any] | None, str]:
        engine, error = self._engine_or_error(object_id)
        if engine is None:
            return None, error
        return engine.snapshot().contributions_view(partition_key), ""

    def rebuild(self, object_id: str, facts: list[dict[str, Any]] | None) -> tuple[dict[str, Any] | None, str]:
        engine, error = self._engine_or_error(object_id)
        if engine is None:
            return None, error
        try:
            report = engine.rebuild(facts)
        except RebuildError as exc:
            return None, str(exc)
        return report, "物化结果已重建"

    def verify(self, object_id: str) -> tuple[dict[str, Any] | None, str]:
        engine, error = self._engine_or_error(object_id)
        if engine is None:
            return None, error
        return engine.verify(), ""


def _seed_demo_materialization() -> None:
    """给示例缓存策略准备一份物化数据集，启动后即可看到分区统计与贡献明细。"""
    service = CacheMaterializeService()
    definition = {
        "策略编号": "CACH-0001",
        "日期分区字段": "业务日期",
        "分组维度": ["渠道", "地区"],
        "指标": [
            {"名称": "订单数", "聚合": "count"},
            {"名称": "销售额", "聚合": "sum", "来源字段": "金额"},
            {"名称": "去重客户数", "聚合": "distinct_count", "来源字段": "客户编号"},
        ],
    }
    view, _ = service.define(definition)
    if view is None:
        return
    events = [
        {"事实编号": "F-1001", "修订号": 1, "业务日期": "2026-09-01", "维度": {"渠道": "线上", "地区": "华东"}, "指标值": {"金额": 120.0, "客户编号": "C-01"}},
        {"事实编号": "F-1002", "修订号": 1, "业务日期": "2026-09-01", "维度": {"渠道": "线上", "地区": "华东"}, "指标值": {"金额": 80.0, "客户编号": "C-02"}},
        {"事实编号": "F-1003", "修订号": 1, "业务日期": "2026-09-01", "维度": {"渠道": "线下", "地区": "华北"}, "指标值": {"金额": 200.0, "客户编号": "C-01"}},
        {"事实编号": "F-1004", "修订号": 1, "业务日期": "2026-09-02", "维度": {"渠道": "线上", "地区": "华北"}, "指标值": {"金额": 150.0, "客户编号": "C-03"}},
        {"事实编号": "F-1005", "修订号": 1, "业务日期": "2026-09-02", "维度": {"渠道": "线下", "地区": "华东"}, "指标值": {"金额": 90.0, "客户编号": "C-04"}},
        # 一笔跨日期更正：F-1002 从 09-01 改到 09-02，旧分区撤回、新分区加入
        {"事实编号": "F-1002", "修订号": 2, "业务日期": "2026-09-02", "维度": {"渠道": "线上", "地区": "华东"}, "指标值": {"金额": 80.0, "客户编号": "C-02"}},
    ]
    service.ingest("CACH-0001", events)


_seed_demo_materialization()
