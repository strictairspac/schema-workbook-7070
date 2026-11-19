"""求解编排：分层穷举、工作单元认领、检查点续跑与状态机。

状态机：
  running   尚有单元在跑或待认领
  waiting   本次时限耗尽且仍有未完成单元 -> 待续跑（保留已验证最好方案）
  optimal   搜遍当前必要层级并找到可行解 -> 已最优（含最优性证据）
  infeasible 全部层级搜遍仍无解 -> 无解
  failed    输入不合法等确定性失败

并发约定（见 services/store.py 的事务实现）：
  - claim_unit 在单个事务内原子领取 pending 单元并写租约；
  - 单元以 unit_key 幂等，checkpoint 产生的新单元重复提交会被忽略；
  - 全局最优在同一事务内按 (表数, 重复量) 比较更新，事件日志去重；
  - 并发续跑只推进同一份报告，重试不会重复发布方案/事件。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import search
from .search import Problem
from .solver_states import (
    STATE_RUNNING, STATE_OPTIMAL, STATE_INFEASIBLE,
)

# 单元租约：worker 崩溃/被抢占后，租约过期即可由其他续跑重新认领。
UNIT_LEASE_SECONDS = 15.0
# 认领后若剩余工作时间不足该阈值，立即归还单元，避免半成品租约。
MIN_WORK_SECONDS = 0.05


class SolverStore:
    """存储抽象；生产实现见 services/search_store.py（MySQL/SQLite 均可）。"""

    def load_search(self, search_id):
        raise NotImplementedError

    def claim_unit(self, search_id, worker_id, lease_seconds):
        """原子领取一个 pending（或租约过期的 running）单元。返回单元 dict 或 None。"""
        raise NotImplementedError

    def release_unit(self, search_id, unit_key):
        """把认领后来不及执行的单元归还为 pending（不增加尝试次数统计）。"""
        raise NotImplementedError

    def complete_unit(self, search_id, unit_key, status, spawned, solution, stats, worker_id):
        """单元完成/检查点的原子提交：
        - 旧单元置 done/checkpointed；
        - spawned 按 unit_key 幂等插入；
        - solution 按字典序合并全局最优；
        - 写入去重后的事件与累计统计。
        返回合并后的搜索记录 dict。
        """
        raise NotImplementedError

    def heartbeat(self, search_id, worker_id):
        raise NotImplementedError

    def set_exhausted_level(self, search_id, level):
        raise NotImplementedError

    def append_event(self, search_id, event_key, message):
        raise NotImplementedError


@dataclass
class RunBudget:
    deadline: float
    worker_id: str


class Orchestrator:
    def __init__(self, store: SolverStore):
        self.store = store

    # ---- 入口 ----------------------------------------------------------------

    def run_once(self, search_id, budget: RunBudget):
        """一个 worker 的一轮执行：不断领取单元直到到时或无可领单元。

        返回 (record, exhausted)：exhausted=True 表示当前已无任何可领取
        （pending 或租约过期）的单元，调用方应推进层级或判定终态。
        """
        record = self.store.load_search(search_id)
        self.store.heartbeat(search_id, budget.worker_id)
        exhausted = False
        while time.monotonic() < budget.deadline:
            # 租约时长固定且与本轮预算解耦：worker 崩溃/被抢占后，单元在
            # UNIT_LEASE_SECONDS 后可被重新认领，不会随长预算被长时间挂起。
            unit = self.store.claim_unit(
                search_id, budget.worker_id, lease_seconds=UNIT_LEASE_SECONDS)
            if unit is None:
                exhausted = True
                break
            problem = self._problem(record)
            remaining = budget.deadline - time.monotonic()
            if remaining < MIN_WORK_SECONDS:
                # 来不及安全执行：立即释放认领，保持可续跑，避免租约挂死。
                self.store.release_unit(search_id, unit["unit_key"])
                break
            stats = {"nodes": 0, "pruned": 0, "leaves": 0}
            status, solution, spawned = search.run_unit(
                problem, unit, deadline=budget.deadline,
                best_so_far=self._best_tuple(record), stats=stats,
            )
            record = self.store.complete_unit(
                search_id, unit["unit_key"], status, spawned,
                solution, stats, budget.worker_id,
            )
        else:
            exhausted = False  # 时限到，仍可能有单元可续跑
        return self.store.load_search(search_id), exhausted

    # ---- 纯函数：层级推进与终态判定 ------------------------------------------

    @staticmethod
    def _problem(record) -> Problem:
        spec = record["spec"]
        return Problem.build(
            spec["attributes"], spec["dependencies"],
            spec["max_tables"], spec["max_attrs_per_table"],
            spec["time_limit_seconds"],
        )

    @staticmethod
    def _best_tuple(record):
        best = record.get("best_solution")
        if not best:
            return None
        return (best["table_count"], best["duplication"])

    @staticmethod
    def initial_plan(record):
        """根据冻结草案与上限生成首批工作单元与预检结论（确定性）。"""
        problem = Orchestrator._problem(record)
        if not problem.precheck["feasible"]:
            return {"precheck": problem.precheck, "units": []}
        units = search.initial_units(problem, level_limit=1)
        return {"precheck": problem.precheck, "units": units}

    @staticmethod
    def next_level_units(record, level):
        """上一层级搜遍且无解时，为下一层级生成单元。"""
        problem = Orchestrator._problem(record)
        if level > problem.max_tables:
            return []
        return search.initial_units(problem, level_limit=level)

    @staticmethod
    def evaluate(record):
        """根据单元/层级统计与最优解，决定搜索终态。纯函数，便于单测。

        - 预检不可行 -> infeasible
        - 存在最优解且当前活跃层级 >= 最优表数 -> optimal（更少表数的层均已搜遍）
        - 全部 1..K 层搜遍且无解 -> infeasible
        - 还有未完成单元 -> running（由调用方按是否到时改 waiting）
        """
        if record.get("precheck", {}).get("feasible") is False:
            return STATE_INFEASIBLE
        best = record.get("best_solution")
        open_units = record["stats"]["units_pending"] + record["stats"]["units_running"]
        exhausted_level = record["stats"].get("exhausted_level", 0)
        if best is not None and exhausted_level >= best["table_count"] and open_units == 0:
            return STATE_OPTIMAL
        if exhausted_level >= record["spec"]["max_tables"] and open_units == 0 and best is None:
            return STATE_INFEASIBLE
        return STATE_RUNNING
