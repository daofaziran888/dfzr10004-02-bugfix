"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
所有状态推进都在模块锁内完成，读取统一走 snapshot 深拷贝，
让并发动作与并发读取看到的是同一份最终状态。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.lifecycle import (
    PLAN_STAGE_TO_TASK,
    SPEC_REGISTRY,
    VERIFY_TASK_MODULE,
    ensure_lifecycle_fields,
    is_abnormal,
    is_pending,
    normalize_stage,
)
from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._locks: dict[str, threading.RLock] = {}
        self._tables: dict[str, list[dict[str, Any]]] = {}
        for name, rows in SEED_ROWS.items():
            table: list[dict[str, Any]] = []
            for row in rows:
                entry = dict(row)
                spec = SPEC_REGISTRY.get(name)
                if spec is not None:
                    entry["status"] = normalize_stage(name, str(entry.get("status")))
                    ensure_lifecycle_fields(entry, name)
                    entry["pending"] = is_pending(entry, spec)
                    entry["abnormal"] = is_abnormal(entry, spec)
                table.append(entry)
            self._tables[name] = table
        self._seed_verify_tasks()

    def _seed_verify_tasks(self) -> None:
        """为已启动的历史计划补齐校验清单基线；草稿计划尚未启动，不产生待处理项。"""
        tasks: list[dict[str, Any]] = []
        next_id = 1
        for plan in self._tables.get("taskplan", []):
            stage = str(plan.get("status"))
            task_status = PLAN_STAGE_TO_TASK.get(stage)
            if task_status is None:
                continue
            ts = int(plan.get("updated_at") or 0)
            tasks.append({
                "id": next_id,
                "plan_id": int(plan["id"]),
                "计划编号": plan.get("计划编号"),
                "目标站点": plan.get("目标站点"),
                "status": task_status,
                "result_status": None,
                "version": 1,
                "updated_at": ts,
                "history": [{
                    "seq": 1,
                    "action": "清单初始化",
                    "from_stage": None,
                    "to_stage": task_status,
                    "at": ts,
                    "operator": "系统",
                    "note": f"随历史计划 {plan.get('计划编号')}（{stage}）建立校验清单",
                    "version": 1,
                }],
            })
            next_id += 1
        self._tables[VERIFY_TASK_MODULE] = tasks

    @contextmanager
    def locked(self, module: str) -> Iterator[threading.RLock]:
        """串行化同一模块的状态推进；可重入，允许下游钩子复用同一把锁。"""
        lock = self._locks.setdefault(module, threading.RLock())
        with lock:
            yield lock

    def reset(self) -> None:
        """恢复到种子初始状态，主要给测试做用例间隔离。"""
        self._locks = {}
        self._tables = {}
        for name, rows in SEED_ROWS.items():
            table: list[dict[str, Any]] = []
            for row in rows:
                entry = dict(row)
                spec = SPEC_REGISTRY.get(name)
                if spec is not None:
                    entry["status"] = normalize_stage(name, str(entry.get("status")))
                    ensure_lifecycle_fields(entry, name)
                    entry["pending"] = is_pending(entry, spec)
                    entry["abnormal"] = is_abnormal(entry, spec)
                table.append(entry)
            self._tables[name] = table
        self._seed_verify_tasks()

    def module_names(self) -> list[str]:
        # 以下划线开头的是模块间联动用的内部表（如校验任务清单），不进运营概览。
        return sorted(name for name in self._tables if not name.startswith("_"))

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        """仅供持锁的写路径使用：返回可变原对象。读路径请改用 snapshot/find_snapshot。"""
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def snapshot(self, module: str) -> list[dict[str, Any]]:
        """统一读取路径：深拷贝后再返回，任何入口都不会读到写一半的中间状态。"""
        with self.locked(module):
            return copy.deepcopy(self.rows(module))

    def find_snapshot(self, module: str, entry_id: int) -> dict[str, Any] | None:
        with self.locked(module):
            row = self.find(module, entry_id)
            return copy.deepcopy(row) if row is not None else None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            spec = SPEC_REGISTRY.get(name)
            with self.locked(name):
                rows = self.snapshot(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if is_pending(row, spec)),
                "abnormal": sum(1 for row in rows if is_abnormal(row, spec)),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
