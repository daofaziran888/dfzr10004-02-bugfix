"""采集结果校验业务规则。

承担两类职责：
1. 校验规则自身的生命周期（与采集计划同一套状态机原语）；
2. 采集计划动作牵动的校验清单：计划启动即新增待处理项，计划阶段变化回写清单，
   校验结果按“时间戳更晚且校验通过”仲裁两个入口的冲突版本。
"""
from __future__ import annotations

from typing import Any

from app.lifecycle import (
    PLAN_STAGE_TO_TASK,
    TASK_ARCHIVED,
    TASK_FAILED,
    TASK_PASSED,
    TASK_PENDING,
    VERIFY_TASK_MODULE,
    IllegalTransition,
    LifecycleConflict,
    VERIFY_SPEC,
    apply_transition,
    is_abnormal,
    is_pending,
    now_ts,
)
from app.store import store

MODULE = "verify"
# 采集计划联动的校验任务清单，内部表（下划线前缀，不进运营概览）。
TASK_MODULE = VERIFY_TASK_MODULE
REQUIRED_FIELDS = ["规则编号", "校验对象", "校验类型"]


class VerifyService:
    # ---------- 校验规则自身的生命周期 ----------
    def _serialize_rule(self, entry: dict[str, Any]) -> dict[str, Any]:
        view = dict(entry)
        view["allowed_actions"] = VERIFY_SPEC.allowed_actions(str(entry.get("status")))
        view["pending"] = is_pending(entry, VERIFY_SPEC)
        view["abnormal"] = is_abnormal(entry, VERIFY_SPEC)
        return view

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.snapshot(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._serialize_rule(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find_snapshot(MODULE, entry_id)
        return self._serialize_rule(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.locked(MODULE):
            entry: dict[str, Any] = {"id": store.next_id(MODULE)}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = VERIFY_SPEC.stages[0]
            entry["version"] = 1
            entry["updated_at"] = now_ts()
            entry["history"] = [{
                "seq": 1,
                "action": "登记规则",
                "from_stage": None,
                "to_stage": VERIFY_SPEC.stages[0],
                "at": entry["updated_at"],
                "operator": str(values.get("operator") or "值班管理员"),
                "note": "规则登记，进入草稿",
                "version": 1,
            }]
            entry["pending"] = True
            entry["abnormal"] = False
            store.rows(MODULE).append(entry)
            return self._serialize_rule(entry), []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        expected_version: int | None = None,
        operator: str = "值班管理员",
        note: str | None = None,
        at: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        with store.locked(MODULE):
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"校验规则 {entry_id} 不存在或已归档"
            try:
                apply_transition(
                    entry,
                    VERIFY_SPEC,
                    action,
                    expected_version=expected_version,
                    operator=operator,
                    note=note,
                    at=at,
                )
            except LifecycleConflict as exc:
                raise exc
            except IllegalTransition as exc:
                return None, str(exc)
            return self._serialize_rule(entry), f"校验规则已{action}，当前阶段：{entry['status']}"

    # ---------- 采集计划联动的校验任务清单 ----------
    def sync_plan_event(self, plan: dict[str, Any], event: dict[str, Any]) -> None:
        """计划动作的下游钩子：新增待处理项，并把计划阶段回写到校验清单。

        必须在 taskplan 模块锁内被调用（锁可重入，这里再取内部表自己的锁）。
        计划从草稿首次启动时创建待处理项；后续每次流转更新同一项，不重复造数。
        """
        plan_id = int(plan["id"])
        new_status = PLAN_STAGE_TO_TASK.get(str(plan.get("status")))
        if new_status is None:
            return
        with store.locked(TASK_MODULE):
            table = store.rows(TASK_MODULE)
            task = next((row for row in table if int(row.get("plan_id", 0)) == plan_id), None)
            if task is None:
                task = {
                    "id": store.next_id(TASK_MODULE),
                    "plan_id": plan_id,
                    "计划编号": plan.get("计划编号"),
                    "目标站点": plan.get("目标站点"),
                    "status": TASK_PENDING,
                    "result_status": None,
                    "version": 1,
                    "updated_at": event["at"],
                    "history": [],
                }
                table.append(task)
            task["status"] = new_status
            task["version"] = int(task.get("version", 1)) + 1
            task["updated_at"] = event["at"]
            task.setdefault("history", []).append({
                "seq": len(task.get("history", [])) + 1,
                "action": f"计划{event['action']}",
                "from_stage": event["from_stage"],
                "to_stage": new_status,
                "at": event["at"],
                "operator": event["operator"],
                "note": f"随采集计划 {plan.get('计划编号')} 阶段回写",
                "version": task["version"],
            })

    def list_tasks(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        pending_only: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.snapshot(TASK_MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("计划编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if pending_only:
            # 待处理项：计划未归档且尚无校验结论。
            rows = [
                row
                for row in rows
                if row.get("status") != TASK_ARCHIVED and not row.get("result_status")
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        return store.find_snapshot(TASK_MODULE, task_id)

    def record_result(
        self,
        task_id: int,
        *,
        passed: bool,
        summary: str,
        operator: str = "值班管理员",
        expected_version: int | None = None,
        at: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """把校验结果回写到清单。

        两个入口结果冲突时，以时间戳更晚且校验通过的版本为准：
        - 已有结论后，未通过的任何回写都不能覆盖（无论时间戳早晚）；
        - 更晚的通过结果可以覆盖较早的通过结果；
        - 同版本（expected_version 过期）的并发提交按冲突拒绝；
        - 随计划归档的清单不再接收回写，历史归档只可查看。
        """
        with store.locked(TASK_MODULE):
            task = store.find(TASK_MODULE, task_id)
            if task is None:
                return None, f"校验任务 {task_id} 不存在"
            if expected_version is not None and int(expected_version) != int(task.get("version", 1)):
                raise LifecycleConflict(
                    f"校验任务已被其他入口更新（当前版本 {task.get('version')}），请刷新后重试"
                )
            if task.get("status") == TASK_ARCHIVED:
                return None, "关联采集计划已归档，校验清单结案，不能重新回写"

            ts = at or now_ts()
            existing = task.get("result_status")
            existing_at = task.get("result_at")
            if existing:
                if not passed:
                    return None, "冲突仲裁：既有版本已有校验结论，未通过结果不予覆盖"
                if existing_at is not None and ts <= int(existing_at):
                    return None, "冲突仲裁：已存在时间戳更晚或相同的通过版本，本次提交不生效"

            task["result_status"] = TASK_PASSED if passed else TASK_FAILED
            task["result_summary"] = summary
            task["result_at"] = ts
            task["result_operator"] = operator
            task["version"] = int(task.get("version", 1)) + 1
            task["updated_at"] = ts
            task.setdefault("history", []).append({
                "seq": len(task.get("history", [])) + 1,
                "action": "回写校验结果",
                "from_stage": existing,
                "to_stage": task["result_status"],
                "at": ts,
                "operator": operator,
                "note": summary,
                "version": task["version"],
            })
            return dict(task), "校验结果已回写" if passed else "校验结果已回写（未通过，待人工复核）"

    # ---------- 下发任务的统一入口 ----------
    def dispatchable_plans(self) -> list[dict[str, Any]]:
        """校验侧读取采集计划状态的唯一口径，已暂停/已归档计划不会再被下发。"""
        from app.services.taskplan import taskplan_service

        return taskplan_service.dispatchable_plans()


verify_service = VerifyService()
