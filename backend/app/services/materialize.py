"""分析物化引擎：消费事实增量与撤销更正事件，维护可追溯的聚合物化分区数据集。

设计约定：
- 缓存对象先登记物化定义（日期分区字段、分组维度、指标聚合含义），再消费事实事件；
- 事实事件携带事实编号、修订号、删除标记；同一事实的更正会在同一次提交里
  成套产生「旧分区撤回 + 新分区加入」两组变更，不会只刷新新键、两边同时保留金额；
- 计数、求和、去重人数分别维护：计数按存活贡献条数加减，求和按来源字段数值加减，
  去重人数按「成员 -> 贡献事实集合」维护，成员只在没有任何存活贡献者时消失；
- 重复修订（修订号不高于已应用修订）直接跳过，不重复应用；
- 原贡献不可得或修订断档时，把受影响分区标记为受污染，读取时不得当作命中结果；
- 重建在暂存区完整构建后再原子替换，失败时保留重建前最后一份可追溯结果。
"""
from __future__ import annotations

import copy
import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any

AGGREGATIONS = ("count", "sum", "distinct_count")
AGGREGATION_LABELS = {"count": "计数", "sum": "求和", "distinct_count": "去重人数"}


class MaterializeError(Exception):
    """物化定义或事实事件不合法；抛出时整批事件不会部分生效。"""


class RebuildError(MaterializeError):
    """重建输入不合法；引擎保留重建前最后一份可追溯结果。"""


@dataclass(frozen=True)
class MetricSpec:
    """单个指标的聚合含义：计数、求和或去重人数。"""

    name: str
    aggregation: str
    source_field: str | None = None

    def definition_view(self) -> dict[str, Any]:
        return {
            "名称": self.name,
            "聚合": self.aggregation,
            "聚合含义": AGGREGATION_LABELS[self.aggregation],
            "来源字段": self.source_field or "—",
        }


@dataclass(frozen=True)
class MaterializeSpec:
    """缓存对象的物化口径：日期分区字段、分组维度与指标集合。"""

    object_id: str
    date_field: str
    dimensions: tuple[str, ...]
    metrics: tuple[MetricSpec, ...]

    @property
    def sum_metrics(self) -> tuple[MetricSpec, ...]:
        return tuple(metric for metric in self.metrics if metric.aggregation == "sum")

    @property
    def distinct_metrics(self) -> tuple[MetricSpec, ...]:
        return tuple(metric for metric in self.metrics if metric.aggregation == "distinct_count")


def parse_spec(definition: dict[str, Any]) -> MaterializeSpec:
    """把接口入参解析成物化定义；不合法时抛出 MaterializeError，说明具体原因。"""
    object_id = str(definition.get("策略编号") or "").strip()
    if not object_id:
        raise MaterializeError("缺少策略编号，无法给缓存对象定义物化口径")
    date_field = str(definition.get("日期分区字段") or "").strip() or "业务日期"
    raw_dims = definition.get("分组维度") or []
    if isinstance(raw_dims, str):
        raw_dims = [part.strip() for part in raw_dims.split(",")]
    dimensions: list[str] = []
    for dim in raw_dims:
        name = str(dim).strip()
        if not name:
            continue
        if name in dimensions:
            raise MaterializeError(f"分组维度「{name}」重复定义")
        dimensions.append(name)
    raw_metrics = definition.get("指标") or []
    if not raw_metrics:
        raise MaterializeError("至少定义一个指标的聚合含义")
    metrics: list[MetricSpec] = []
    for item in raw_metrics:
        if not isinstance(item, dict):
            raise MaterializeError("指标定义必须是对象：名称、聚合、来源字段")
        name = str(item.get("名称") or "").strip()
        aggregation = str(item.get("聚合") or "").strip()
        source = str(item.get("来源字段") or "").strip() or None
        if not name:
            raise MaterializeError("指标名称不能为空")
        if aggregation not in AGGREGATIONS:
            raise MaterializeError(
                f"指标「{name}」的聚合方式「{aggregation}」不支持，可选：count（计数）、sum（求和）、distinct_count（去重人数）"
            )
        if aggregation in ("sum", "distinct_count") and not source:
            raise MaterializeError(f"指标「{name}」按{AGGREGATION_LABELS[aggregation]}聚合时必须给出来源字段")
        if any(metric.name == name for metric in metrics):
            raise MaterializeError(f"指标「{name}」重复定义")
        metrics.append(MetricSpec(name=name, aggregation=aggregation, source_field=source))
    return MaterializeSpec(object_id=object_id, date_field=date_field, dimensions=tuple(dimensions), metrics=tuple(metrics))


@dataclass
class _Contribution:
    """一条事实修订对某个分区分组的原始贡献明细（可追溯、可撤回）。"""

    fact_id: str
    revision: int
    partition: str
    group: tuple[str, ...]
    dims: dict[str, str]
    sums: dict[str, float]
    members: dict[str, list[str]]


@dataclass
class _GroupState:
    """一个分区内某组维度取值的聚合状态：三类指标分开维护。"""

    count: int = 0
    sums: dict[str, float] = field(default_factory=dict)
    members: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    contributions: dict[str, _Contribution] = field(default_factory=dict)


@dataclass
class _PartitionState:
    """一个日期分区：分组聚合、分区水位与污染标记。"""

    groups: dict[tuple[str, ...], _GroupState] = field(default_factory=dict)
    watermark: int = 0
    tainted: bool = False
    taint_reasons: list[str] = field(default_factory=list)


@dataclass
class _FactRecord:
    """事实的最新已应用修订：删除后保留墓碑，供重复修订判定。"""

    revision: int
    deleted: bool
    partition: str | None = None
    group: tuple[str, ...] | None = None


def _group_label(group_key: tuple[str, ...]) -> str:
    return " / ".join(group_key) if group_key else "（全部）"


def _derive_contribution(spec: MaterializeSpec, event: dict[str, Any]) -> _Contribution:
    """从规范化事件推导出它对目标分区分组的贡献。"""
    measures = event["指标值"]
    sums = {metric.name: float(measures[metric.source_field]) for metric in spec.sum_metrics}
    members = {metric.name: [str(measures[metric.source_field])] for metric in spec.distinct_metrics}
    return _Contribution(
        fact_id=event["事实编号"],
        revision=event["修订号"],
        partition=event["分区键"],
        group=event["维度键"],
        dims=dict(event["维度"]),
        sums=sums,
        members=members,
    )


def _add_contribution(partitions: dict[str, _PartitionState], contribution: _Contribution) -> None:
    partition = partitions.setdefault(contribution.partition, _PartitionState())
    group = partition.groups.setdefault(contribution.group, _GroupState())
    group.count += 1
    for name, value in contribution.sums.items():
        group.sums[name] = group.sums.get(name, 0.0) + value
    for name, keys in contribution.members.items():
        bucket = group.members.setdefault(name, {})
        for key in keys:
            bucket.setdefault(key, set()).add(contribution.fact_id)
    group.contributions[contribution.fact_id] = contribution


def _remove_contribution(
    partitions: dict[str, _PartitionState], partition_key: str, group_key: tuple[str, ...], fact_id: str
) -> _Contribution | None:
    """撤回一条存活贡献：计数减一、求和回退、去重成员只在无其他贡献者时消失。"""
    partition = partitions.get(partition_key)
    if partition is None:
        return None
    group = partition.groups.get(group_key)
    if group is None:
        return None
    contribution = group.contributions.pop(fact_id, None)
    if contribution is None:
        return None
    group.count -= 1
    for name, value in contribution.sums.items():
        group.sums[name] = group.sums.get(name, 0.0) - value
    for name, keys in contribution.members.items():
        bucket = group.members.get(name, {})
        for key in keys:
            owners = bucket.get(key)
            if owners is None:
                continue
            owners.discard(fact_id)
            if not owners:
                bucket.pop(key, None)
    if group.count <= 0 and not group.contributions:
        partition.groups.pop(group_key, None)
    return contribution


def _final_states(journal: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """从事件日志取每条事实的最终修订（全量聚合的输入口径）。"""
    final: dict[str, dict[str, Any]] = {}
    for event in journal:
        fact_id = event["事实编号"]
        if fact_id not in final or event["修订号"] > final[fact_id]["修订号"]:
            final[fact_id] = event
    return final


def _full_aggregate(
    spec: MaterializeSpec, final_events: list[dict[str, Any]]
) -> tuple[dict[str, _PartitionState], dict[str, _FactRecord]]:
    """对同一事实集合做全量聚合：核对与重建共用的独立计算路径。"""
    partitions: dict[str, _PartitionState] = {}
    records: dict[str, _FactRecord] = {}
    for event in final_events:
        fact_id = event["事实编号"]
        if event["删除标记"]:
            records[fact_id] = _FactRecord(revision=event["修订号"], deleted=True)
            continue
        _add_contribution(partitions, _derive_contribution(spec, event))
        records[fact_id] = _FactRecord(
            revision=event["修订号"], deleted=False, partition=event["分区键"], group=event["维度键"]
        )
    return partitions, records


def _diff_state(
    actual: dict[str, _PartitionState], expected: dict[str, _PartitionState]
) -> list[str]:
    """把增量物化结果与全量聚合结果逐分区逐组对比，返回可读的差异列表。"""
    diffs: list[str] = []
    actual_keys = {key for key, part in actual.items() if part.groups}
    expected_keys = {key for key, part in expected.items() if part.groups}
    for key in sorted(actual_keys - expected_keys):
        diffs.append(f"分区 {key}：物化结果多出分组，全量聚合中不存在")
    for key in sorted(expected_keys - actual_keys):
        diffs.append(f"分区 {key}：全量聚合存在分组，物化结果缺失")
    for key in sorted(actual_keys & expected_keys):
        actual_groups = actual[key].groups
        expected_groups = expected[key].groups
        for group_key in sorted(set(actual_groups) - set(expected_groups)):
            diffs.append(f"分区 {key} 分组 {_group_label(group_key)}：物化结果多出")
        for group_key in sorted(set(expected_groups) - set(actual_groups)):
            diffs.append(f"分区 {key} 分组 {_group_label(group_key)}：物化结果缺失")
        for group_key in sorted(set(actual_groups) & set(expected_groups)):
            label = f"分区 {key} 分组 {_group_label(group_key)}"
            actual_group = actual_groups[group_key]
            expected_group = expected_groups[group_key]
            if actual_group.count != expected_group.count:
                diffs.append(f"{label}：计数 {actual_group.count} ≠ 全量 {expected_group.count}")
            for name in sorted(set(actual_group.sums) | set(expected_group.sums)):
                actual_value = actual_group.sums.get(name, 0.0)
                expected_value = expected_group.sums.get(name, 0.0)
                if abs(actual_value - expected_value) > 1e-6:
                    diffs.append(f"{label}：求和 {name} {actual_value} ≠ 全量 {expected_value}")
            for name in sorted(set(actual_group.members) | set(expected_group.members)):
                actual_members = set(actual_group.members.get(name, {}))
                expected_members = set(expected_group.members.get(name, {}))
                if actual_members != expected_members:
                    diffs.append(f"{label}：去重成员 {name} 不一致 {sorted(actual_members)} ≠ 全量 {sorted(expected_members)}")
            if set(actual_group.contributions) != set(expected_group.contributions):
                diffs.append(f"{label}：贡献事实集合不一致")
    return diffs


class MaterializeEngine:
    """单个缓存对象的物化引擎：事件应用、快照读取、一致性核对与重建。"""

    def __init__(self, spec: MaterializeSpec) -> None:
        self.spec = spec
        self._lock = threading.RLock()
        self._partitions: dict[str, _PartitionState] = {}
        self._facts: dict[str, _FactRecord] = {}
        self._journal: list[dict[str, Any]] = []
        self._watermark = 0

    # ------------------------------------------------------------------ 事件应用

    def apply(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """消费一批事实事件：整批先校验，再在暂存副本上按序预演，全部合法才一次性替换正式状态。"""
        if not isinstance(events, list) or not events:
            raise MaterializeError("事件批次为空，没有可消费的事实事件")
        with self._lock:
            normalized = [self._normalize_event(event, index) for index, event in enumerate(events)]
            # 在暂存副本上按序规划并提交：批内后的事件能看到批内前的事件，
            # 任一条失败时正式状态不受影响，成功时整批变更成套可见
            partitions = copy.deepcopy(self._partitions)
            facts = dict(self._facts)
            watermark = self._watermark
            journal: list[dict[str, Any]] = []
            change_set: list[dict[str, Any]] = []
            outcomes: list[dict[str, Any]] = []
            tainted: dict[str, list[str]] = {}
            applied = 0
            skipped = 0
            for event in normalized:
                plan = self._plan_event(event, facts)
                outcomes.append(plan["outcome"])
                if plan["skip"]:
                    skipped += 1
                    continue
                applied += 1
                watermark += 1
                seq = watermark
                touched: set[str] = set()
                if plan["retract"] is not None:
                    old_partition, old_group = plan["retract"]
                    _remove_contribution(partitions, old_partition, old_group, event["事实编号"])
                    touched.add(old_partition)
                    change_set.append({
                        "提交序号": seq,
                        "分区": old_partition,
                        "维度键": _group_label(old_group),
                        "变更": "撤回旧贡献",
                        "事实编号": event["事实编号"],
                        "修订号": plan["old_revision"],
                    })
                if plan["contribution"] is not None:
                    contribution = plan["contribution"]
                    _add_contribution(partitions, contribution)
                    touched.add(contribution.partition)
                    change_set.append({
                        "提交序号": seq,
                        "分区": contribution.partition,
                        "维度键": _group_label(contribution.group),
                        "变更": "加入新贡献",
                        "事实编号": contribution.fact_id,
                        "修订号": contribution.revision,
                    })
                for partition_key, reason in plan["taints"]:
                    partition = partitions.setdefault(partition_key, _PartitionState())
                    partition.tainted = True
                    if reason not in partition.taint_reasons:
                        partition.taint_reasons.append(reason)
                    tainted.setdefault(partition_key, [])
                    if reason not in tainted[partition_key]:
                        tainted[partition_key].append(reason)
                    touched.add(partition_key)
                for partition_key in touched:
                    partitions[partition_key].watermark = seq
                facts[event["事实编号"]] = plan["record"]
                journal.append(event)
            # 整批预演成功，一次性替换正式状态
            self._partitions = partitions
            self._facts = facts
            self._watermark = watermark
            self._journal.extend(journal)
            return {
                "对象": self.spec.object_id,
                "水位": self._watermark,
                "接收": len(events),
                "应用": applied,
                "跳过重复": skipped,
                "变更集": change_set,
                "受污染分区": [{"分区": key, "原因": reasons} for key, reasons in sorted(tainted.items())],
                "事件结果": outcomes,
            }

    def _normalize_event(self, event: dict[str, Any], index: int) -> dict[str, Any]:
        label = f"第 {index + 1} 条事件"
        if not isinstance(event, dict):
            raise MaterializeError(f"{label}不是有效的事实事件")
        fact_id = str(event.get("事实编号") or "").strip()
        if not fact_id:
            raise MaterializeError(f"{label}缺少事实编号")
        revision = event.get("修订号")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise MaterializeError(f"{label}（事实 {fact_id}）修订号必须是不小于 1 的整数")
        deleted = bool(event.get("删除标记") or False)
        partition = str(event.get(self.spec.date_field) or event.get("业务日期") or "").strip()
        raw_dims = event.get("维度") or {}
        dims: dict[str, str] = {}
        measures = event.get("指标值") or {}
        if not deleted:
            if not partition:
                raise MaterializeError(f"{label}（事实 {fact_id}）缺少{self.spec.date_field}，无法定位日期分区")
            if not isinstance(raw_dims, dict):
                raise MaterializeError(f"{label}（事实 {fact_id}）维度必须是对象")
            for dim in self.spec.dimensions:
                value = raw_dims.get(dim)
                if value is None or str(value).strip() == "":
                    raise MaterializeError(f"{label}（事实 {fact_id}）缺少分组维度「{dim}」")
                dims[dim] = str(value)
            if not isinstance(measures, dict):
                raise MaterializeError(f"{label}（事实 {fact_id}）指标值必须是对象")
            for metric in self.spec.sum_metrics:
                value = measures.get(metric.source_field)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise MaterializeError(
                        f"{label}（事实 {fact_id}）指标「{metric.name}」来源字段「{metric.source_field}」不是数值"
                    )
            for metric in self.spec.distinct_metrics:
                value = measures.get(metric.source_field)
                if value is None or str(value).strip() == "":
                    raise MaterializeError(
                        f"{label}（事实 {fact_id}）指标「{metric.name}」来源字段「{metric.source_field}」为空，无法统计去重人数"
                    )
        return {
            "事实编号": fact_id,
            "修订号": revision,
            "删除标记": deleted,
            "分区键": partition or None,
            "维度": dims,
            "维度键": tuple(dims[dim] for dim in self.spec.dimensions) if not deleted else tuple(),
            "指标值": dict(measures) if isinstance(measures, dict) else {},
        }

    def _plan_event(self, event: dict[str, Any], facts: dict[str, _FactRecord]) -> dict[str, Any]:
        """为单条事件规划变更集：撤回旧贡献、加入新贡献、污染标记，规划本身不产生副作用。"""
        fact_id = event["事实编号"]
        revision = event["修订号"]
        deleted = event["删除标记"]
        record = facts.get(fact_id)
        outcome = {"事实编号": fact_id, "修订号": revision, "结果": "应用", "说明": ""}
        if record is not None and revision <= record.revision:
            outcome["结果"] = "重复跳过"
            outcome["说明"] = f"事实已应用修订 {record.revision}，修订 {revision} 不重复应用"
            return {"skip": True, "outcome": outcome, "retract": None, "old_revision": None,
                    "contribution": None, "taints": [], "record": record}
        retract: tuple[str, tuple[str, ...]] | None = None
        old_revision: int | None = None
        if record is not None and not record.deleted and record.partition is not None:
            retract = (record.partition, record.group if record.group is not None else tuple())
            old_revision = record.revision
        taints: list[tuple[str, str]] = []
        if record is None and revision > 1:
            # 原贡献不可得：历史修订从未接入，没有可撤回的旧贡献
            if deleted and not event["分区键"]:
                raise MaterializeError(
                    f"事实 {fact_id} 删除时原贡献不可得，且事件未携带{self.spec.date_field}，无法定位受影响分区"
                )
            taints.append((event["分区键"], f"事实 {fact_id} 从修订 {revision} 接入，原贡献不可得（历史修订未接入）"))
        elif record is not None and revision > record.revision + 1:
            # 修订断档：中间修订丢失，旧位置与新位置都标记为受污染
            reason = f"事实 {fact_id} 修订断档：已应用 {record.revision}，收到 {revision}"
            if retract is not None:
                taints.append((retract[0], reason))
            if not deleted:
                taints.append((event["分区键"], reason))
            elif event["分区键"] and (retract is None or event["分区键"] != retract[0]):
                taints.append((event["分区键"], reason))
        contribution = _derive_contribution(self.spec, event) if not deleted else None
        if record is None and revision == 1 and not deleted:
            outcome["说明"] = "新增事实"
        elif record is not None and record.deleted and deleted:
            outcome["结果"] = "重复删除忽略"
            outcome["说明"] = "事实已删除，重复删除不改变聚合结果"
        elif deleted:
            outcome["说明"] = "删除已应用，旧贡献已撤回"
        elif retract is not None:
            outcome["说明"] = "更正已成套应用：旧分区撤回、新分区加入"
        else:
            outcome["说明"] = "更正已应用"
        new_record = _FactRecord(
            revision=revision,
            deleted=deleted,
            partition=None if deleted else event["分区键"],
            group=None if deleted else event["维度键"],
        )
        return {"skip": False, "outcome": outcome, "retract": retract, "old_revision": old_revision,
                "contribution": contribution, "taints": taints, "record": new_record}

    # ------------------------------------------------------------------ 快照读取

    def snapshot(self) -> "DatasetSnapshot":
        """取当前水位的不可变快照：分区统计与原始贡献明细都出自同一修订集合。"""
        with self._lock:
            return DatasetSnapshot(
                spec=self.spec,
                watermark=self._watermark,
                partitions=copy.deepcopy(self._partitions),
                facts=copy.deepcopy(self._facts),
            )

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return {
                "策略编号": self.spec.object_id,
                "日期分区字段": self.spec.date_field,
                "分组维度": list(self.spec.dimensions),
                "指标定义": [metric.definition_view() for metric in self.spec.metrics],
                "水位": self._watermark,
                "分区数": len(self._partitions),
                "受污染分区数": sum(1 for part in self._partitions.values() if part.tainted),
                "事实数": len(self._facts),
            }

    # ------------------------------------------------------------------ 核对与重建

    def verify(self) -> dict[str, Any]:
        """把增量物化结果与同一事实集合的全量聚合对比，验证两者一致。"""
        with self._lock:
            final = _final_states(self._journal)
            expected_partitions, expected_facts = _full_aggregate(self.spec, list(final.values()))
            diffs = _diff_state(self._partitions, expected_partitions)
            return {
                "对象": self.spec.object_id,
                "一致": not diffs,
                "差异": diffs,
                "核对口径": "同一事实集合最终修订的全量聚合",
                "核对事实数": len(expected_facts),
                "存活事实数": sum(1 for item in expected_facts.values() if not item.deleted),
                "受污染分区": sorted(key for key, part in self._partitions.items() if part.tainted),
                "水位": self._watermark,
            }

    def rebuild(self, facts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """重建物化结果：在暂存区完整构建后才原子替换；失败时保留上一份可追溯结果。"""
        with self._lock:
            if facts is None:
                source = list(_final_states(self._journal).values())
                origin = "事件日志"
            else:
                source = self._normalize_rebuild_facts(facts)
                origin = "给定事实集合"
            partitions, records = _full_aggregate(self.spec, source)
            # 走到这里说明暂存区已完整构建，替换是整份的，半成品不会覆盖完整分区
            self._watermark += 1
            for partition in partitions.values():
                partition.watermark = self._watermark
            self._partitions = partitions
            self._facts = records
            self._journal = [dict(event) for event in source]
            return {
                "对象": self.spec.object_id,
                "来源": origin,
                "水位": self._watermark,
                "事实数": len(records),
                "存活事实数": sum(1 for item in records.values() if not item.deleted),
                "分区数": len(partitions),
                "受污染分区数": 0,
                "说明": "重建完成，结果已整体替换，污染标记已随完整重算清除",
            }

    def _normalize_rebuild_facts(self, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(facts, list) or not facts:
            raise RebuildError("重建事实集合为空，已保留上一份可追溯结果")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, item in enumerate(facts):
            try:
                event = self._normalize_event(item, index)
            except MaterializeError as exc:
                raise RebuildError(f"重建失败：{exc}；已保留上一份可追溯结果") from exc
            fact_id = event["事实编号"]
            if fact_id in seen:
                raise RebuildError(f"重建失败：事实 {fact_id} 在重建集合中重复；已保留上一份可追溯结果")
            seen.add(fact_id)
            normalized.append(event)
        return normalized


class DatasetSnapshot:
    """某一水位下的不可变数据集视图：分区统计与原始贡献明细同源。"""

    def __init__(
        self,
        spec: MaterializeSpec,
        watermark: int,
        partitions: dict[str, _PartitionState],
        facts: dict[str, _FactRecord],
    ) -> None:
        self.spec = spec
        self.watermark = watermark
        self.partitions = partitions
        self.facts = facts

    # ------------------------------------------------------------------ 基础视图

    def metric_definitions(self) -> list[dict[str, Any]]:
        return [metric.definition_view() for metric in self.spec.metrics]

    def revision_set(self) -> dict[str, int]:
        return {fact_id: record.revision for fact_id, record in self.facts.items()}

    def revision_digest(self) -> str:
        payload = ";".join(f"{fact_id}#{revision}" for fact_id, revision in sorted(self.revision_set().items()))
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

    def select_partitions(self, partition_keys: list[str] | None = None) -> tuple[dict[str, _PartitionState], list[str]]:
        """按分区键选择分区；返回（选中的分区，缺失的分区键）。"""
        if partition_keys is None:
            return dict(self.partitions), []
        selected: dict[str, _PartitionState] = {}
        missing: list[str] = []
        for key in partition_keys:
            partition = self.partitions.get(key)
            if partition is None:
                missing.append(key)
            else:
                selected[key] = partition
        return selected, missing

    def dataset_view(self) -> dict[str, Any]:
        return {
            "策略编号": self.spec.object_id,
            "日期分区字段": self.spec.date_field,
            "分组维度": list(self.spec.dimensions),
            "指标定义": self.metric_definitions(),
            "水位": self.watermark,
            "修订集合摘要": self.revision_digest(),
            "分区": [self._partition_view(key, part) for key, part in sorted(self.partitions.items())],
        }

    def read_partition(self, partition_key: str) -> dict[str, Any]:
        """单分区读取：受污染分区不得当作命中结果返回。"""
        partition = self.partitions.get(partition_key)
        if partition is None:
            return {"命中": False, "分区键": partition_key, "原因": f"分区 {partition_key} 不存在"}
        view = self._partition_view(partition_key, partition)
        if partition.tainted:
            return {
                "命中": False,
                "分区键": partition_key,
                "原因": "；".join(partition.taint_reasons),
                "受污染数据": view,
            }
        return {"命中": True, **view}

    def contributions_view(self, partition_key: str) -> dict[str, Any]:
        """分区原始贡献明细：可追溯每条事实修订贡献了什么。"""
        partition = self.partitions.get(partition_key)
        if partition is None:
            return {"命中": False, "分区键": partition_key, "原因": f"分区 {partition_key} 不存在", "贡献": []}
        rows = self._contribution_rows({partition_key: partition})
        result: dict[str, Any] = {
            "分区键": partition_key,
            "水位": partition.watermark,
            "贡献数": len(rows),
            "贡献": rows,
        }
        if partition.tainted:
            result["命中"] = False
            result["原因"] = "；".join(partition.taint_reasons)
        else:
            result["命中"] = True
        return result

    # ------------------------------------------------------------------ 下钻消费视图

    def group_rows(self, partitions: dict[str, _PartitionState]) -> list[dict[str, Any]]:
        """父级结果：所选分区内各维度分组的聚合行。"""
        rows: list[dict[str, Any]] = []
        for key in sorted(partitions):
            partition = partitions[key]
            for group_key, group in sorted(partition.groups.items()):
                rows.append({
                    "分区键": key,
                    "维度": dict(zip(self.spec.dimensions, group_key)),
                    "维度键": _group_label(group_key),
                    "指标": self._metrics_of(group),
                    "贡献数": len(group.contributions),
                    "状态": "受污染" if partition.tainted else "正常",
                    "水位": partition.watermark,
                })
        return rows

    def contribution_rows(self, partitions: dict[str, _PartitionState]) -> list[dict[str, Any]]:
        """叶级贡献：所选分区内每条事实修订的原始贡献明细。"""
        return self._contribution_rows(partitions)

    # ------------------------------------------------------------------ 内部组装

    def _partition_view(self, key: str, partition: _PartitionState) -> dict[str, Any]:
        return {
            "分区键": key,
            "状态": "受污染" if partition.tainted else "正常",
            "污染原因": list(partition.taint_reasons),
            "水位": partition.watermark,
            "分组数": len(partition.groups),
            "贡献数": sum(len(group.contributions) for group in partition.groups.values()),
            "合计": self._rollup([partition]),
            "分组": [self._group_view(group_key, group) for group_key, group in sorted(partition.groups.items())],
        }

    def _group_view(self, group_key: tuple[str, ...], group: _GroupState) -> dict[str, Any]:
        return {
            "维度": dict(zip(self.spec.dimensions, group_key)),
            "维度键": _group_label(group_key),
            "指标": self._metrics_of(group),
            "贡献数": len(group.contributions),
        }

    def _metrics_of(self, group: _GroupState) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for metric in self.spec.metrics:
            if metric.aggregation == "count":
                output[metric.name] = group.count
            elif metric.aggregation == "sum":
                output[metric.name] = round(group.sums.get(metric.name, 0.0), 6)
            else:
                output[metric.name] = len(group.members.get(metric.name, {}))
        return output

    def _rollup(self, partitions: list[_PartitionState]) -> dict[str, Any]:
        """跨分组合计：计数与求和累加，去重人数按成员并集统计（不能简单相加）。"""
        count = 0
        sums: dict[str, float] = {}
        members: dict[str, set[str]] = {}
        for partition in partitions:
            for group in partition.groups.values():
                count += group.count
                for name, value in group.sums.items():
                    sums[name] = sums.get(name, 0.0) + value
                for name, bucket in group.members.items():
                    members.setdefault(name, set()).update(bucket.keys())
        output: dict[str, Any] = {}
        for metric in self.spec.metrics:
            if metric.aggregation == "count":
                output[metric.name] = count
            elif metric.aggregation == "sum":
                output[metric.name] = round(sums.get(metric.name, 0.0), 6)
            else:
                output[metric.name] = len(members.get(metric.name, set()))
        return output

    def _contribution_rows(self, partitions: dict[str, _PartitionState]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key in sorted(partitions):
            partition = partitions[key]
            for group_key, group in sorted(partition.groups.items()):
                for contribution in sorted(group.contributions.values(), key=lambda item: item.fact_id):
                    rows.append({
                        "分区键": key,
                        "事实编号": contribution.fact_id,
                        "修订号": contribution.revision,
                        "维度": dict(contribution.dims),
                        "维度键": _group_label(group_key),
                        "计数贡献": 1,
                        "求和贡献": {name: round(value, 6) for name, value in contribution.sums.items()},
                        "去重成员": {name: sorted(keys) for name, keys in contribution.members.items()},
                    })
        return rows


class MaterializeRegistry:
    """按策略编号管理各缓存对象的物化引擎。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._engines: dict[str, MaterializeEngine] = {}

    def define(self, definition: dict[str, Any]) -> tuple[MaterializeSpec, bool]:
        """登记或整体替换物化口径；口径变更后旧物化结果作废，需重新消费事件。"""
        spec = parse_spec(definition)
        with self._lock:
            replaced = spec.object_id in self._engines
            self._engines[spec.object_id] = MaterializeEngine(spec)
        return spec, replaced

    def engine(self, object_id: str) -> MaterializeEngine | None:
        with self._lock:
            return self._engines.get(object_id)

    def list_objects(self) -> list[dict[str, Any]]:
        with self._lock:
            engines = list(self._engines.values())
        return [engine.summary() for engine in engines]


registry = MaterializeRegistry()
