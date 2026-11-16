"""分析物化引擎核对：跨日期更正、跨维度移动、重复删除、修订断档、去重人数减少等场景。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest

from app.services.materialize import (
    MaterializeEngine,
    MaterializeError,
    MaterializeRegistry,
    RebuildError,
    parse_spec,
)

SPEC_DEF = {
    "策略编号": "TEST-0001",
    "日期分区字段": "业务日期",
    "分组维度": ["渠道"],
    "指标": [
        {"名称": "订单数", "聚合": "count"},
        {"名称": "销售额", "聚合": "sum", "来源字段": "金额"},
        {"名称": "去重客户数", "聚合": "distinct_count", "来源字段": "客户编号"},
    ],
}


def make_engine() -> MaterializeEngine:
    return MaterializeEngine(parse_spec(SPEC_DEF))


def ev(
    fact_id: str,
    revision: int,
    date: str = "2026-09-01",
    channel: str = "线上",
    amount: float = 100.0,
    customer: str = "C-1",
    deleted: bool = False,
) -> dict:
    event = {
        "事实编号": fact_id,
        "修订号": revision,
        "业务日期": date,
        "维度": {"渠道": channel},
        "指标值": {"金额": amount, "客户编号": customer},
    }
    if deleted:
        event["删除标记"] = True
    return event


def partition_map(engine: MaterializeEngine) -> dict:
    view = engine.snapshot().dataset_view()
    return {item["分区键"]: item for item in view["分区"]}


def group_metrics(partition: dict, channel: str) -> dict | None:
    for group in partition["分组"]:
        if group["维度"].get("渠道") == channel:
            return group["指标"]
    return None


class CrossDateCorrectionTest(unittest.TestCase):
    """跨日期更正：旧分区减去原贡献，新分区加入新贡献，两边不能同时保留金额。"""

    def test_correction_moves_contribution_between_partitions(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, date="2026-09-01", amount=100.0)])
        report = engine.apply([ev("F-1", 2, date="2026-09-02", amount=100.0)])

        partitions = partition_map(engine)
        day1 = partitions["2026-09-01"]
        day2 = partitions["2026-09-02"]
        self.assertEqual(day1["合计"]["销售额"], 0.0, "旧分区必须减去原贡献")
        self.assertEqual(day1["合计"]["订单数"], 0)
        self.assertEqual(day2["合计"]["销售额"], 100.0, "新分区必须加入新贡献")
        self.assertEqual(day2["合计"]["订单数"], 1)
        total = day1["合计"]["销售额"] + day2["合计"]["销售额"]
        self.assertEqual(total, 100.0, "只刷新新键会让两边同时保留金额")

    def test_correction_change_set_is_atomic(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, date="2026-09-01")])
        report = engine.apply([ev("F-1", 2, date="2026-09-02")])

        changes = [item for item in report["变更集"] if item["事实编号"] == "F-1"]
        self.assertEqual(len(changes), 2, "一次更正必须成套产生撤回与加入两条变更")
        kinds = {item["变更"] for item in changes}
        self.assertEqual(kinds, {"撤回旧贡献", "加入新贡献"})
        seqs = {item["提交序号"] for item in changes}
        self.assertEqual(len(seqs), 1, "撤回与加入必须在同一次提交里成套可见")
        partitions = partition_map(engine)
        self.assertEqual(partitions["2026-09-01"]["水位"], seqs.pop())
        self.assertEqual(partitions["2026-09-02"]["水位"], partitions["2026-09-01"]["水位"])


class CrossDimensionMoveTest(unittest.TestCase):
    """跨维度移动：分组维度变化时旧分组减去、新分组加入。"""

    def test_dimension_move(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, channel="线上", amount=60.0), ev("F-2", 1, channel="线上", amount=40.0)])
        engine.apply([ev("F-1", 2, channel="线下", amount=60.0)])

        partitions = partition_map(engine)
        day1 = partitions["2026-09-01"]
        online = group_metrics(day1, "线上")
        offline = group_metrics(day1, "线下")
        self.assertEqual(online["销售额"], 40.0)
        self.assertEqual(online["订单数"], 1)
        self.assertEqual(offline["销售额"], 60.0)
        self.assertEqual(offline["订单数"], 1)
        self.assertEqual(day1["合计"]["销售额"], 100.0, "维度移动后分区合计不变")


class DuplicateHandlingTest(unittest.TestCase):
    """重复修订不重复应用；重复删除不重复扣减。"""

    def test_duplicate_revision_not_reapplied(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, amount=100.0)])
        report = engine.apply([ev("F-1", 1, amount=100.0), ev("F-1", 2, amount=150.0), ev("F-1", 2, amount=150.0)])

        self.assertEqual(report["跳过重复"], 2)
        partitions = partition_map(engine)
        metrics = group_metrics(partitions["2026-09-01"], "线上")
        self.assertEqual(metrics["订单数"], 1)
        self.assertEqual(metrics["销售额"], 150.0, "同一修订重复到达不得重复累加")

    def test_stale_revision_ignored(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 3, amount=300.0)])
        report = engine.apply([ev("F-1", 2, amount=200.0)])
        self.assertEqual(report["跳过重复"], 1)
        metrics = group_metrics(partition_map(engine)["2026-09-01"], "线上")
        self.assertEqual(metrics["销售额"], 300.0)

    def test_duplicate_delete_no_double_subtract(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, amount=100.0), ev("F-2", 1, amount=50.0)])
        engine.apply([ev("F-1", 2, deleted=True)])
        second = engine.apply([ev("F-1", 2, deleted=True)])  # 同修订重复删除
        third = engine.apply([ev("F-1", 3, deleted=True)])  # 已删除事实再次删除

        self.assertEqual(second["跳过重复"], 1)
        outcomes = [item for item in third["事件结果"] if item["事实编号"] == "F-1"]
        self.assertEqual(outcomes[0]["结果"], "重复删除忽略")
        metrics = group_metrics(partition_map(engine)["2026-09-01"], "线上")
        self.assertEqual(metrics["订单数"], 1)
        self.assertEqual(metrics["销售额"], 50.0, "重复删除不得重复扣减")
        self.assertEqual(partition_map(engine)["2026-09-01"]["状态"], "正常", "重复删除不应误标污染")


class RevisionGapTaintTest(unittest.TestCase):
    """修订断档与原贡献不可得：受影响分区必须标记，不得当作命中结果。"""

    def test_revision_gap_marks_both_partitions(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, date="2026-09-01", amount=100.0)])
        report = engine.apply([ev("F-1", 3, date="2026-09-02", amount=120.0)])  # 跳过修订 2

        tainted = {item["分区"] for item in report["受污染分区"]}
        self.assertEqual(tainted, {"2026-09-01", "2026-09-02"}, "断档影响新旧两个分区")
        snapshot = engine.snapshot()
        read_old = snapshot.read_partition("2026-09-01")
        read_new = snapshot.read_partition("2026-09-02")
        self.assertFalse(read_old["命中"], "受污染分区不得当作命中结果")
        self.assertFalse(read_new["命中"])
        self.assertIn("修订断档", read_new["原因"])
        # 数值仍然成套应用，只是被标记为不可靠
        self.assertEqual(read_new["受污染数据"]["合计"]["销售额"], 120.0)

    def test_unknown_fact_correction_taints(self) -> None:
        engine = make_engine()
        report = engine.apply([ev("F-9", 2, amount=80.0)])  # 从未接入修订 1

        self.assertEqual({item["分区"] for item in report["受污染分区"]}, {"2026-09-01"})
        read = engine.snapshot().read_partition("2026-09-01")
        self.assertFalse(read["命中"])
        self.assertIn("原贡献不可得", read["原因"])

    def test_delete_unknown_fact_taints_target_partition(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1)])
        report = engine.apply([ev("F-9", 4, date="2026-09-01", deleted=True)])

        self.assertEqual({item["分区"] for item in report["受污染分区"]}, {"2026-09-01"})
        read = engine.snapshot().read_partition("2026-09-01")
        self.assertFalse(read["命中"], "删除未知事实后分区汇总不可靠，不得命中")

    def test_delete_unknown_fact_without_date_rejected(self) -> None:
        engine = make_engine()
        with self.assertRaises(MaterializeError):
            engine.apply([{"事实编号": "F-9", "修订号": 4, "删除标记": True}])

    def test_untainted_partition_still_hits(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, date="2026-09-01"), ev("F-2", 1, date="2026-09-03")])
        engine.apply([ev("F-1", 3, date="2026-09-02")])  # 断档只影响 09-01 与 09-02
        read = engine.snapshot().read_partition("2026-09-03")
        self.assertTrue(read["命中"], "未受影响的分区应正常命中")


class DistinctCountTest(unittest.TestCase):
    """去重人数：按成员-贡献者集合维护，删除最后贡献者时才减少。"""

    def test_distinct_count_shared_member(self) -> None:
        engine = make_engine()
        engine.apply([
            ev("F-1", 1, customer="C-1"),
            ev("F-2", 1, customer="C-2"),
            ev("F-3", 1, customer="C-1"),
        ])
        metrics = group_metrics(partition_map(engine)["2026-09-01"], "线上")
        self.assertEqual(metrics["去重客户数"], 2)
        self.assertEqual(metrics["订单数"], 3)

        engine.apply([ev("F-1", 2, deleted=True)])
        metrics = group_metrics(partition_map(engine)["2026-09-01"], "线上")
        self.assertEqual(metrics["去重客户数"], 2, "F-3 仍贡献 C-1，去重人数不变")

        engine.apply([ev("F-3", 2, deleted=True)])
        metrics = group_metrics(partition_map(engine)["2026-09-01"], "线上")
        self.assertEqual(metrics["去重客户数"], 1, "最后一个 C-1 贡献者删除，去重人数减少")

        engine.apply([ev("F-2", 2, deleted=True)])
        metrics = group_metrics(partition_map(engine)["2026-09-01"], "线上")
        self.assertIsNone(metrics, "全部删除后分组清空")
        self.assertEqual(partition_map(engine)["2026-09-01"]["合计"]["去重客户数"], 0)

    def test_distinct_count_member_move(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, customer="C-1"), ev("F-2", 1, customer="C-2")])
        engine.apply([ev("F-2", 2, customer="C-1")])  # 更正后 F-2 改贡献 C-1
        metrics = group_metrics(partition_map(engine)["2026-09-01"], "线上")
        self.assertEqual(metrics["去重客户数"], 1, "C-2 失去最后贡献者后消失")

    def test_distinct_rollup_is_union_not_sum(self) -> None:
        engine = make_engine()
        engine.apply([
            ev("F-1", 1, date="2026-09-01", customer="C-1"),
            ev("F-2", 1, date="2026-09-02", customer="C-1"),
        ])
        view = engine.snapshot().dataset_view()
        totals = [p["合计"]["去重客户数"] for p in view["分区"]]
        self.assertEqual(totals, [1, 1], "同一客户跨分区各计一次")


class BatchAtomicityTest(unittest.TestCase):
    """任一条事件不合法，整批不生效。"""

    def test_invalid_event_rejects_whole_batch(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, amount=10.0)])
        with self.assertRaises(MaterializeError):
            engine.apply([
                ev("F-2", 1, amount=20.0),
                {"事实编号": "F-3", "修订号": 1, "业务日期": "2026-09-01", "维度": {"渠道": "线上"}, "指标值": {"金额": "abc", "客户编号": "C-9"}},
            ])
        partitions = partition_map(engine)
        self.assertEqual(partitions["2026-09-01"]["合计"]["销售额"], 10.0, "坏批次不得部分生效")
        self.assertEqual(partitions["2026-09-01"]["合计"]["订单数"], 1)


class VerifyConsistencyTest(unittest.TestCase):
    """物化结果应与相同事实集合的全量聚合一致。"""

    def test_incremental_matches_full_aggregation(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, date="2026-09-01", channel="线上", amount=100.0, customer="C-1")])
        engine.apply([ev("F-2", 1, date="2026-09-01", channel="线下", amount=50.0, customer="C-2")])
        engine.apply([ev("F-1", 2, date="2026-09-02", channel="线下", amount=120.0, customer="C-1")])  # 跨日期+跨维度
        engine.apply([ev("F-3", 1, date="2026-09-02", channel="线上", amount=30.0, customer="C-2")])
        engine.apply([ev("F-2", 2, deleted=True)])
        engine.apply([ev("F-3", 2, date="2026-09-02", channel="线上", amount=30.0, customer="C-1")])  # 去重成员变更

        report = engine.verify()
        self.assertTrue(report["一致"], f"增量物化与全量聚合不一致：{report['差异']}")
        self.assertEqual(report["差异"], [])
        self.assertEqual(report["存活事实数"], 2)

    def test_verify_after_gap_still_consistent_values(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, date="2026-09-01")])
        engine.apply([ev("F-1", 3, date="2026-09-02")])  # 断档 → 污染但数值成套应用
        report = engine.verify()
        self.assertTrue(report["一致"], "断档后数值仍应与全量聚合一致，污染是可靠性标记")
        self.assertEqual(report["受污染分区"], ["2026-09-01", "2026-09-02"])


class RebuildTest(unittest.TestCase):
    """重建：暂存完整构建后原子替换；失败保留上一份可追溯结果。"""

    def test_failed_rebuild_keeps_previous_result(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, amount=100.0), ev("F-2", 1, amount=50.0)])
        before = engine.snapshot().dataset_view()

        with self.assertRaises(RebuildError):
            engine.rebuild([{"事实编号": "F-9", "修订号": 1, "业务日期": "2026-09-01", "维度": {"渠道": "线上"}, "指标值": {"客户编号": "C-9"}}])
        after = engine.snapshot().dataset_view()
        self.assertEqual(before, after, "重建失败不得让半成品覆盖完整分区")

        with self.assertRaises(RebuildError):
            engine.rebuild([ev("F-8", 1), ev("F-8", 1)])  # 重复事实编号
        self.assertEqual(engine.snapshot().dataset_view(), after)

    def test_successful_rebuild_replaces_and_clears_taint(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, date="2026-09-01", amount=100.0)])
        engine.apply([ev("F-1", 3, date="2026-09-02", amount=120.0)])  # 断档 → 污染
        self.assertFalse(engine.snapshot().read_partition("2026-09-02")["命中"])

        report = engine.rebuild()  # 按事件日志重放
        self.assertEqual(report["受污染分区数"], 0)
        read = engine.snapshot().read_partition("2026-09-02")
        self.assertTrue(read["命中"], "完整重算后分区恢复可靠")
        self.assertEqual(read["合计"]["销售额"], 120.0)
        self.assertTrue(engine.verify()["一致"])

    def test_rebuild_from_given_fact_set(self) -> None:
        engine = make_engine()
        engine.apply([ev("F-1", 1, amount=100.0)])
        report = engine.rebuild([
            ev("F-2", 1, date="2026-09-03", amount=70.0, customer="C-2"),
            ev("F-3", 1, date="2026-09-03", amount=30.0, customer="C-2"),
        ])
        self.assertEqual(report["来源"], "给定事实集合")
        partitions = partition_map(engine)
        self.assertNotIn("2026-09-01", {k for k, v in partitions.items() if v["分组"]})
        metrics = group_metrics(partitions["2026-09-03"], "线上")
        self.assertEqual(metrics["销售额"], 100.0)
        self.assertEqual(metrics["去重客户数"], 1)
        self.assertTrue(engine.verify()["一致"])


class RegistryTest(unittest.TestCase):
    """口径定义与重定义：口径变更后旧物化结果作废。"""

    def test_define_apply_and_redefine(self) -> None:
        reg = MaterializeRegistry()
        spec, replaced = reg.define(SPEC_DEF)
        self.assertFalse(replaced)
        engine = reg.engine("TEST-0001")
        self.assertIsNotNone(engine)
        engine.apply([ev("F-1", 1)])
        self.assertEqual(reg.engine("TEST-0001").summary()["事实数"], 1)

        _, replaced = reg.define(SPEC_DEF)
        self.assertTrue(replaced)
        self.assertEqual(reg.engine("TEST-0001").summary()["事实数"], 0, "重定义后旧结果作废")

    def test_invalid_definition_rejected(self) -> None:
        reg = MaterializeRegistry()
        with self.assertRaises(MaterializeError):
            reg.define({"策略编号": "X-1", "指标": [{"名称": "销售额", "聚合": "sum"}]})  # 缺来源字段
        with self.assertRaises(MaterializeError):
            reg.define({"策略编号": "X-1", "指标": [{"名称": "订单数", "聚合": "avg"}]})  # 不支持的聚合


class DrillConsumeTest(unittest.TestCase):
    """多维下钻消费：记录分区水位与指标定义，叶级贡献和父级结果来自同一修订集合。"""

    def test_consume_records_receipt_and_same_origin(self) -> None:
        from app.services.drill import DrillService
        from app.services.materialize import registry

        registry.define({**SPEC_DEF, "策略编号": "TEST-DRILL"})
        engine = registry.engine("TEST-DRILL")
        engine.apply([
            ev("F-1", 1, date="2026-09-01", channel="线上", amount=100.0, customer="C-1"),
            ev("F-2", 1, date="2026-09-01", channel="线下", amount=50.0, customer="C-1"),
        ])
        engine.apply([ev("F-1", 2, date="2026-09-02", channel="线上", amount=100.0, customer="C-1")])

        service = DrillService()
        result, message = service.consume_materialized(1, {"策略编号": "TEST-DRILL"})
        self.assertIsNotNone(result, message)
        receipt = result["读取凭证"]
        self.assertEqual(receipt["水位"], engine.snapshot().watermark)
        self.assertEqual(receipt["修订集合"], {"F-1": 2, "F-2": 1}, "凭证必须记录同一修订集合")
        self.assertTrue(receipt["叶级与父级同源"])
        self.assertTrue(receipt["可靠"])
        self.assertEqual(len(receipt["指标定义"]), 3)
        self.assertIn("2026-09-01", receipt["分区水位"])
        self.assertTrue(result["命中"])
        self.assertEqual(len(result["父级结果"]), 2)
        self.assertEqual(len(result["叶级贡献"]), 2)

        # 再次读取生成新的凭证，历史记录留痕
        result2, _ = service.consume_materialized(1, {"策略编号": "TEST-DRILL", "分区键": "2026-09-02"})
        self.assertEqual(result2["读取凭证"]["读取编号"], "READ-1-2")
        self.assertEqual(len(result2["父级结果"]), 1)

    def test_consume_tainted_partition_not_hit(self) -> None:
        from app.services.drill import DrillService
        from app.services.materialize import registry

        registry.define({**SPEC_DEF, "策略编号": "TEST-DRILL-TAINT"})
        engine = registry.engine("TEST-DRILL-TAINT")
        engine.apply([ev("F-1", 1, date="2026-09-01")])
        engine.apply([ev("F-1", 3, date="2026-09-02")])  # 断档

        service = DrillService()
        result, message = service.consume_materialized(1, {"策略编号": "TEST-DRILL-TAINT", "分区键": "2026-09-02"})
        self.assertIsNotNone(result)
        self.assertFalse(result["命中"], "受污染分区不得作为命中结果")
        self.assertFalse(result["读取凭证"]["可靠"])
        self.assertEqual(result["读取凭证"]["受污染分区"], ["2026-09-02"])

    def test_consume_unknown_object_or_partition(self) -> None:
        from app.services.drill import DrillService

        service = DrillService()
        result, message = service.consume_materialized(1, {"策略编号": "NOPE-0000"})
        self.assertIsNone(result)
        result, message = service.consume_materialized(1, {"策略编号": "TEST-DRILL", "分区键": "2099-01-01"})
        self.assertIsNone(result)
        self.assertIn("不存在", message)


if __name__ == "__main__":
    unittest.main()
