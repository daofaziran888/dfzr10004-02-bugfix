"""采集计划生命周期与下游联动的回归测试。

只依赖标准库（unittest + 线程），覆盖：
- 严格相邻流转：草稿不能直接暂停、暂停只作用于执行中、归档只作用于已暂停；
- 重复/并发启动只有一次有效推进，并发读取看到同一最终状态；
- 计划动作联动校验清单、结果回写冲突仲裁、归档不可重启；
- 列表/详情/概览共用同一状态读取口径，配额台账暂停不丢失。
"""
from __future__ import annotations

import threading
import unittest

from app.lifecycle import LifecycleConflict
from app.services.taskplan import TaskplanService
from app.services.verify import verify_service
from app.store import store


class LifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        store.reset()
        self.service = TaskplanService()

    def test_seed_normalization_and_read_path(self) -> None:
        rows, total = self.service.list_entries()
        self.assertEqual(total, 3)
        by_id = {row["id"]: row for row in rows}
        # 旧文案「运行中」归一为「执行中」，计划状态列与 status 同源，不再显示样本文案。
        self.assertEqual(by_id[1]["status"], "草稿")
        self.assertEqual(by_id[1]["计划状态"], "草稿")
        self.assertEqual(by_id[2]["status"], "执行中")
        self.assertEqual(by_id[3]["status"], "已暂停")
        # 可执行动作严格按当前阶段给出，列表与详情共用同一口径。
        self.assertEqual(by_id[1]["allowed_actions"], ["启动计划"])
        self.assertEqual(by_id[2]["allowed_actions"], ["暂停计划"])
        self.assertEqual(set(by_id[3]["allowed_actions"]), {"恢复执行", "归档计划"})
        detail = self.service.get_entry(2)
        self.assertEqual(detail["allowed_actions"], by_id[2]["allowed_actions"])

    def test_draft_cannot_pause_or_skip_stages(self) -> None:
        # TASK-0001 原本是草稿，点暂停必须被拒绝（原缺陷：直接进入暂停）。
        entry, message = self.service.run_action(1, "暂停计划")
        self.assertIsNone(entry)
        self.assertIn("草稿", message)
        # 跳级：草稿直接归档、待执行直接暂停都不受理。
        self.assertIsNone(self.service.run_action(1, "归档计划")[0])
        self.service.run_action(1, "启动计划")
        self.assertIsNone(self.service.run_action(1, "暂停计划")[0])
        # 退回：执行中再点启动不受理。
        self.service.run_action(1, "开始执行")
        self.assertIsNone(self.service.run_action(1, "启动计划")[0])

    def test_full_happy_path_with_stage_notes(self) -> None:
        for action, expected in [
            ("启动计划", "待执行"),
            ("开始执行", "执行中"),
            ("暂停计划", "已暂停"),
        ]:
            entry, message = self.service.run_action(1, action)
            self.assertEqual(entry["status"], expected)
            self.assertIn(expected, message)
        # 撤销暂停仅运营管理员可用，且只能回到执行中，不能回到更早阶段。
        denied, _ = self.service.run_action(1, "恢复执行")
        self.assertIsNone(denied)
        resumed, _ = self.service.run_action(1, "恢复执行", operator_role="运营管理员")
        self.assertEqual(resumed["status"], "执行中")
        self.service.run_action(1, "暂停计划")
        archived, _ = self.service.run_action(1, "归档计划")
        self.assertEqual(archived["status"], "已归档")

    def test_archived_plan_is_readonly(self) -> None:
        for action in ["启动计划", "开始执行", "暂停计划"]:
            self.service.run_action(1, action)
        self.service.run_action(1, "归档计划")
        # 历史已归档可查看，但任何动作都不受理，不能被重新启动。
        self.assertIsNotNone(self.service.get_entry(1))
        for action in ["启动计划", "开始执行", "恢复执行", "归档计划"]:
            entry, _ = self.service.run_action(
                1, action, operator_role="运营管理员"
            )
            self.assertIsNone(entry)

    def test_quota_ledger_never_lost_on_pause(self) -> None:
        for action in ["启动计划", "开始执行", "暂停计划"]:
            entry, _ = self.service.run_action(1, action)
        types = [record["type"] for record in entry["quota_records"]]
        # 暂停是追加「配额冻结」而不是覆盖/删除，历史记录完整保留。
        self.assertEqual(
            types, ["配额登记", "配额预留", "配额生效", "配额冻结"]
        )

    def test_duplicate_and_concurrent_start_advance_once(self) -> None:
        entry, _ = self.service.create_entry(
            {"计划编号": "TASK-0100", "目标站点": "X", "采集频率": "每日"}
        )
        plan_id = entry["id"]
        outcomes: list[bool] = []

        def fire() -> None:
            changed, _ = self.service.run_action(plan_id, "启动计划")
            outcomes.append(changed is not None)

        threads = [threading.Thread(target=fire) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(outcomes), 1)
        final = self.service.get_entry(plan_id)
        self.assertEqual(final["status"], "待执行")
        self.assertEqual(final["version"], 2)

    def test_concurrent_readers_see_same_final_state(self) -> None:
        seen: list[str] = []

        def read() -> None:
            seen.append(self.service.get_entry(2)["status"])

        threads = [threading.Thread(target=read) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(set(seen), {"执行中"})

    def test_optimistic_version_conflict(self) -> None:
        entry, _ = self.service.run_action(2, "暂停计划")
        stale = entry["version"] - 1
        with self.assertRaises(LifecycleConflict):
            self.service.run_action(2, "归档计划", expected_version=stale)

    def test_verify_checklist_linkage(self) -> None:
        # 启动即新增待处理项，后续阶段变化回写到同一项。
        self.service.run_action(1, "启动计划")
        tasks, total = verify_service.list_tasks(keyword="TASK-0001")
        self.assertEqual(total, 1)
        self.assertEqual(tasks[0]["status"], "待处理")
        self.service.run_action(1, "开始执行")
        self.service.run_action(1, "暂停计划")
        task = verify_service.list_tasks(keyword="TASK-0001")[0][0]
        self.assertEqual(task["status"], "待复核")
        # 待处理口径：未归档且尚无结论。
        pending, _ = verify_service.list_tasks(pending_only=True)
        self.assertTrue(any(item["计划编号"] == "TASK-0001" for item in pending))

    def test_result_conflict_arbitration(self) -> None:
        task = verify_service.list_tasks(keyword="TASK-0002")[0][0]
        ok, _ = verify_service.record_result(task["id"], passed=True, summary="v1", at=1000)
        self.assertIsNotNone(ok)
        # 更晚的未通过不能覆盖任何既有结论。
        rejected, message = verify_service.record_result(
            task["id"], passed=False, summary="late fail", at=2000
        )
        self.assertIsNone(rejected)
        self.assertIn("仲裁", message)
        # 更早的通过也不能覆盖；只有更晚且通过的版本生效。
        self.assertIsNone(
            verify_service.record_result(task["id"], passed=True, summary="old", at=500)[0]
        )
        winner, _ = verify_service.record_result(
            task["id"], passed=True, summary="latest pass", at=3000
        )
        self.assertEqual(winner["result_at"], 3000)

    def test_archived_checklist_rejects_rewrite_and_dispatch(self) -> None:
        for action in ["启动计划", "开始执行", "暂停计划", "归档计划"]:
            self.service.run_action(1, action)
        task = verify_service.list_tasks(keyword="TASK-0001")[0][0]
        self.assertEqual(task["status"], "已归档")
        self.assertIsNone(
            verify_service.record_result(task["id"], passed=True, summary="x", at=9999)[0]
        )
        # 已暂停/已归档计划不能再作为下发对象，校验入口共用该口径。
        dispatchable = {plan["id"] for plan in self.service.dispatchable_plans()}
        self.assertNotIn(1, dispatchable)
        self.assertEqual(
            {plan["id"] for plan in verify_service.dispatchable_plans()}, dispatchable
        )

    def test_overview_derives_from_state_machine(self) -> None:
        overview = store.overview()
        modules = {item["name"]: item for item in overview["modules"]}
        # 三条种子计划都未归档 => 待处理 3；仅已暂停一条计异常。
        self.assertEqual(modules["taskplan"]["pending"], 3)
        self.assertEqual(modules["taskplan"]["abnormal"], 1)
        # 校验规则的暂停是正常运营动作，不计异常。
        self.assertEqual(modules["verify"]["abnormal"], 0)

    def test_every_history_event_notes_current_stage(self) -> None:
        for action in ["启动计划", "开始执行", "暂停计划"]:
            self.service.run_action(1, action)
        view = self.service.lifecycle_view(1)
        self.assertEqual(view["current_stage"], "已暂停")
        self.assertEqual(view["stages"], ["草稿", "待执行", "执行中", "已暂停", "已归档"])
        for event in view["history"]:
            self.assertTrue(event["to_stage"])
            self.assertGreaterEqual(event["at"], 0)


if __name__ == "__main__":
    unittest.main()
