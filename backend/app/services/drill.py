"""多维下钻分析业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.materialize import MaterializeSpec, registry
from app.store import store

MODULE = "drill"
REQUIRED_FIELDS = ["路径编号", "起点指标", "下钻维度"]
STATUS_ORDER = ["已创建", "分析中", "已完成", "已归档"]
ACTION_RULES = {"开始下钻": "已创建", "完成分析": "分析中", "归档路径": "已完成"}
NEGATIVE_ACTIONS = []


class DrillService:
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
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
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
            return None, f"下钻路径 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于多维下钻分析可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"下钻路径已{action}"

    def consume_materialized(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """跨模块读取分析物化分区数据集：父级结果与叶级贡献出自同一水位的同一修订集合。

        每次读取都会在下钻路径上留一份读取凭证，记录分区水位、指标定义与修订集合摘要，
        供事后核对「这次下钻看到的是哪一版物化结果」。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"下钻路径 {entry_id} 不存在或已归档"
        object_id = str(values.get("策略编号") or "").strip()
        engine = registry.engine(object_id)
        if engine is None:
            return None, f"缓存对象 {object_id or '（空）'} 尚未定义物化口径，请先在分析缓存管理里定义"
        partition_key = str(values.get("分区键") or "").strip() or None
        snapshot = engine.snapshot()
        selected, missing = snapshot.select_partitions([partition_key] if partition_key else None)
        if missing:
            return None, f"分区 {'、'.join(missing)} 在物化数据集里不存在"
        parent_rows = snapshot.group_rows(selected)
        leaf_rows = snapshot.contribution_rows(selected)
        consistent = _same_origin_check(engine.spec, parent_rows, leaf_rows)
        tainted = sorted(key for key, part in selected.items() if part.tainted)
        hit = not tainted
        records = entry.setdefault("物化读取记录", [])
        receipt = {
            "读取编号": f"READ-{entry_id}-{len(records) + 1}",
            "路径编号": entry.get("路径编号"),
            "策略编号": object_id,
            "分区范围": partition_key or "全部分区",
            "水位": snapshot.watermark,
            "分区水位": {key: part.watermark for key, part in sorted(selected.items())},
            "指标定义": snapshot.metric_definitions(),
            "修订集合": snapshot.revision_set(),
            "修订集合摘要": snapshot.revision_digest(),
            "叶级与父级同源": consistent,
            "可靠": hit,
            "受污染分区": tainted,
            "读取时间": datetime.now().isoformat(timespec="seconds"),
        }
        records.append(receipt)
        result = {
            "读取凭证": receipt,
            "父级结果": parent_rows,
            "叶级贡献": leaf_rows,
            "命中": hit,
        }
        if tainted:
            return result, f"物化分区数据集已读取，但分区 {'、'.join(tainted)} 受污染，未作为命中结果"
        return result, "物化分区数据集已读取，父级结果与叶级贡献同源"


def _same_origin_check(spec: MaterializeSpec, parent_rows: list[dict[str, Any]], leaf_rows: list[dict[str, Any]]) -> bool:
    """同源核对：用叶级贡献现算一遍父级指标，与父级结果逐格比对。"""
    counts: dict[tuple[str, str], int] = {}
    sums: dict[tuple[str, str], dict[str, float]] = {}
    members: dict[tuple[str, str], dict[str, set[str]]] = {}
    for row in leaf_rows:
        key = (row["分区键"], row["维度键"])
        counts[key] = counts.get(key, 0) + int(row["计数贡献"])
        sum_slot = sums.setdefault(key, {})
        for name, value in row["求和贡献"].items():
            sum_slot[name] = sum_slot.get(name, 0.0) + float(value)
        member_slot = members.setdefault(key, {})
        for name, keys in row["去重成员"].items():
            member_slot.setdefault(name, set()).update(keys)
    for row in parent_rows:
        key = (row["分区键"], row["维度键"])
        expected = row["指标"]
        for metric in spec.metrics:
            if metric.aggregation == "count":
                actual: Any = counts.get(key, 0)
            elif metric.aggregation == "sum":
                actual = round(sums.get(key, {}).get(metric.name, 0.0), 6)
            else:
                actual = len(members.get(key, {}).get(metric.name, set()))
            expected_value = expected[metric.name]
            if metric.aggregation == "sum":
                if abs(float(actual) - float(expected_value)) > 1e-6:
                    return False
            elif actual != expected_value:
                return False
    return True
