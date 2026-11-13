"""共享生命周期状态机。

计划动作、列表取数、校验入口都通过这里的同一套常量与函数读取状态、推进状态，
避免各入口各自解释 status 造成口径漂移（草稿被直接挂起、列表显示旧文案、
概览待处理数漂移等问题都源于此前没有统一的状态读取路径）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


def now_ts() -> int:
    """毫秒级时间戳，全平台状态变更统一用它定序。"""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class IllegalTransition(Exception):
    """当前阶段不允许执行该动作：跳级、退回或终态再操作都在此拒绝。"""


class LifecycleConflict(Exception):
    """并发版本冲突：两个入口同时推进同一记录时只放行一个。"""


@dataclass(frozen=True)
class LifecycleSpec:
    """一条业务线的生命周期声明：阶段序列 + 每个动作允许的相邻流转。"""

    name: str
    stages: tuple[str, ...]
    # 动作 -> (唯一允许的来源阶段, 目标阶段)
    transitions: dict[str, tuple[str, str]]
    terminal_stages: frozenset[str]
    abnormal_stages: frozenset[str] = frozenset()

    def stage_index(self, stage: str) -> int:
        try:
            return self.stages.index(stage)
        except ValueError:
            return -1

    def allowed_actions(self, stage: str) -> list[str]:
        """该阶段真正可执行的动作，列表页与详情页的按钮都以此为准。"""
        return [
            action
            for action, (from_stage, _) in self.transitions.items()
            if from_stage == stage
        ]

    def target_stage(self, action: str, stage: str) -> str | None:
        """动作在当前阶段不受理时返回 None，杜绝跳级与退回。"""
        rule = self.transitions.get(action)
        if rule is None or rule[0] != stage:
            return None
        return rule[1]

    def is_terminal(self, stage: str) -> bool:
        return stage in self.terminal_stages


# 采集计划：草稿 -> 待执行 -> 执行中 -> 已暂停 -> 已归档
# 暂停只能作用于执行中；归档只能作用于已暂停；已暂停可由运营管理员撤销，
# 但撤销只能回到执行中，不能回到待执行/草稿等更早状态；已归档不可重启。
TASKPLAN_SPEC = LifecycleSpec(
    name="taskplan",
    stages=("草稿", "待执行", "执行中", "已暂停", "已归档"),
    transitions={
        "启动计划": ("草稿", "待执行"),
        "开始执行": ("待执行", "执行中"),
        "暂停计划": ("执行中", "已暂停"),
        "恢复执行": ("已暂停", "执行中"),
        "归档计划": ("已暂停", "已归档"),
    },
    terminal_stages=frozenset({"已归档"}),
    abnormal_stages=frozenset({"已暂停"}),
)

# 采集结果校验规则沿用四阶段，同样只允许相邻流转。
# 规则挂起属于正常运营动作，不计入概览异常量。
VERIFY_SPEC = LifecycleSpec(
    name="verify",
    stages=("草稿", "运行中", "已暂停", "已归档"),
    transitions={
        "启动校验": ("草稿", "运行中"),
        "暂停校验": ("运行中", "已暂停"),
        "恢复校验": ("已暂停", "运行中"),
        "归档规则": ("已暂停", "已归档"),
    },
    terminal_stages=frozenset({"已归档"}),
)

SPEC_REGISTRY: dict[str, LifecycleSpec] = {
    "taskplan": TASKPLAN_SPEC,
    "verify": VERIFY_SPEC,
}

# 历史样本里的旧状态文案 -> 规范阶段，仅用于一次性归一种子数据。
LEGACY_STAGE_ALIASES: dict[str, dict[str, str]] = {
    "taskplan": {"运行中": "执行中", "已完成": "已归档"},
    "verify": {"已完成": "已归档"},
}

# 采集计划联动的校验任务清单（store 内部表，下划线前缀，不进运营概览）。
VERIFY_TASK_MODULE = "_verify_task"

# 校验清单任务的统一状态口径。
TASK_PENDING = "待处理"
TASK_RUNNING = "校验中"
TASK_PAUSED = "待复核"
TASK_ARCHIVED = "已归档"
TASK_PASSED = "校验通过"
TASK_FAILED = "校验不通过"
TASK_STATUSES = (TASK_PENDING, TASK_RUNNING, TASK_PAUSED, TASK_ARCHIVED, TASK_PASSED, TASK_FAILED)

# 采集计划阶段 -> 校验清单状态。
PLAN_STAGE_TO_TASK = {
    "待执行": TASK_PENDING,
    "执行中": TASK_RUNNING,
    "已暂停": TASK_PAUSED,
    "已归档": TASK_ARCHIVED,
}


def normalize_stage(module: str, stage: str) -> str:
    return LEGACY_STAGE_ALIASES.get(module, {}).get(stage, stage)


def is_pending(entry: dict[str, Any], spec: LifecycleSpec | None) -> bool:
    """待处理口径统一到状态机：终态之外都算待处理，概览不再自己维护布尔值。"""
    if spec is None:
        return bool(entry.get("pending"))
    return entry.get("status") not in spec.terminal_stages


def is_abnormal(entry: dict[str, Any], spec: LifecycleSpec | None) -> bool:
    if spec is None:
        return bool(entry.get("abnormal"))
    return entry.get("status") in spec.abnormal_stages


def ensure_lifecycle_fields(entry: dict[str, Any], module: str) -> None:
    """给历史数据补齐版本号、时间戳与变更轨迹；已有字段只保留不覆盖。"""
    entry.setdefault("version", 1)
    entry.setdefault("updated_at", now_ts())
    entry.setdefault(
        "history",
        [
            {
                "seq": 1,
                "action": "数据初始化",
                "from_stage": None,
                "to_stage": entry.get("status"),
                "at": entry["updated_at"],
                "operator": "系统",
                "note": "历史样本数据归一",
                "version": 1,
            }
        ],
    )


def refresh_flags(entry: dict[str, Any], spec: LifecycleSpec) -> None:
    """把 pending/abnormal 派生标志回刷为状态机的统一口径。"""
    entry["pending"] = is_pending(entry, spec)
    entry["abnormal"] = is_abnormal(entry, spec)


def apply_transition(
    entry: dict[str, Any],
    spec: LifecycleSpec,
    action: str,
    *,
    expected_version: int | None = None,
    operator: str = "值班管理员",
    note: str | None = None,
    at: int | None = None,
    hook: Callable[[dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """在调用方持有模块锁的前提下推进一次状态。

    - 只受理 spec 中声明的相邻流转，跳级/退回/终态操作一律 IllegalTransition；
    - expected_version 与当前版本不一致时抛 LifecycleConflict，
      配合模块锁保证两个入口并发提交同一请求时只有一次有效推进；
    - 每次推进都生成带时间戳、操作人与当前阶段的 history 事件。
    """
    if action not in spec.transitions:
        raise IllegalTransition(f"动作「{action}」不属于{spec.name}可执行范围")

    if expected_version is not None and int(expected_version) != int(entry.get("version", 1)):
        raise LifecycleConflict(
            f"记录已被其他入口更新（当前版本 {entry.get('version')}），请刷新后重试"
        )

    current = str(entry.get("status"))
    target = spec.target_stage(action, current)
    if target is None:
        allowed = "、".join(spec.allowed_actions(current)) or "无可用动作"
        raise IllegalTransition(
            f"当前阶段为「{current}」，动作「{action}」不受理；该阶段可执行：{allowed}"
        )

    event = {
        "seq": len(entry.get("history", [])) + 1,
        "action": action,
        "from_stage": current,
        "to_stage": target,
        "at": at or now_ts(),
        "operator": operator or "值班管理员",
        "note": note or "",
        "version": int(entry.get("version", 1)) + 1,
    }
    entry["status"] = target
    entry["version"] = event["version"]
    entry["updated_at"] = event["at"]
    entry.setdefault("history", []).append(event)
    refresh_flags(entry, spec)
    if hook is not None:
        hook(entry, event)
    return event
