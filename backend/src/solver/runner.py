"""高层求解流程：建案 -> 分层推进 -> 到时续跑 -> 终态与证据。

runner 不依赖具体数据库，只操作 SolverStore 接口，因此内存版与
SQL 版共用同一套状态机，单元测试可直接验证续跑/并发语义。
"""
from __future__ import annotations

import hashlib
import json
import time

from . import theory
from .orchestrator import Orchestrator, RunBudget
from .search import Problem
from .solver_states import (
    STATE_RUNNING, STATE_WAITING, STATE_OPTIMAL,
    STATE_INFEASIBLE, STATE_FAILED,
)


class SearchRunner:
    def __init__(self, store):
        self.store = store
        self.orchestrator = Orchestrator(store)

    def initialize(self, search_id, spec):
        """首次建案：冻结草案/上限，跑确定性预检并生成第 1 层单元。

        输入非法时落 failed 并向上抛出，由 worker/API 统一处理。
        """
        record = self.store.create_search(search_id, spec)
        try:
            problem = Problem.build(
                spec["attributes"], spec["dependencies"],
                spec["max_tables"], spec["max_attrs_per_table"],
                spec["time_limit_seconds"],
            )
        except ValueError as exc:
            self.store.mark_status(search_id, STATE_FAILED)
            self.store.append_event(search_id, "invalid-input", str(exc))
            raise
        plan = self.orchestrator.initial_plan(record)
        self.store.set_precheck(search_id, plan["precheck"])
        if not plan["precheck"]["feasible"]:
            for note in plan["precheck"]["notes"]:
                self.store.append_event(search_id, f"precheck:{note}", note)
            self.store.mark_status(search_id, STATE_INFEASIBLE)
            return self.store.load_search(search_id)
        self.store.add_units(search_id, plan["units"])
        self.store.append_event(
            search_id, "init",
            f"求解建案：表数上限 {spec['max_tables']}，每表上限 "
            f"{spec['max_attrs_per_table']}，时限 {spec['time_limit_seconds']}s；"
            f"候选表 {len(problem.candidates)} 张，首批单元 {len(plan['units'])} 个",
        )
        return self.store.load_search(search_id)

    def run_with_budget(self, search_id, worker_id, time_budget_seconds):
        """执行一轮（每次续跑都会调用）。time_budget_seconds 为这一轮的墙钟时限。"""
        record = self.store.load_search(search_id)
        if record["status"] in (STATE_OPTIMAL, STATE_INFEASIBLE, STATE_FAILED):
            return record  # 终态幂等：重复触发不重复执行/发布
        self.store.mark_run_started(search_id, worker_id)
        self.store.mark_status(search_id, STATE_RUNNING)

        start = time.monotonic()
        deadline = start + max(0.05, float(time_budget_seconds))
        # 同一轮内：跑完一层立即推进下一层（若仍有时间），直到到时或进入终态。
        while time.monotonic() < deadline:
            record, exhausted = self.orchestrator.run_once(
                search_id, RunBudget(deadline, worker_id))
            if exhausted:
                record = self._advance_levels(search_id, record)
            new_state = self.orchestrator.evaluate(record)
            if new_state != STATE_RUNNING:
                break
            if not exhausted:
                break  # 时限到且仍有未完成单元 -> 稍后落 waiting
        else:
            new_state = self.orchestrator.evaluate(record)

        if new_state == STATE_RUNNING:
            # 还有活但本轮时限已到 -> 待续跑，保留已验证的最好方案。
            new_state = STATE_WAITING
        self.store.mark_status(search_id, new_state)

        if new_state == STATE_OPTIMAL:
            self._finalize_optimal(search_id, record)
        elif new_state == STATE_INFEASIBLE:
            self.store.append_event(
                search_id, "infeasible",
                f"已搜遍 1..{record['spec']['max_tables']} 张表的全部合规组合，确认无解",
            )
        elif new_state == STATE_WAITING:
            best = record.get("best_solution")
            self.store.append_event(
                search_id, f"waiting:{record['stats']['runs']}",
                "本轮时限到，检查点已保存，可续跑；"
                + (f"当前已验证最好方案 {best['table_count']} 张表、重复 "
                   f"{best['duplication']} 个属性" if best else "暂未找到可行方案"),
            )
        return self.store.load_search(search_id)

    # ---- 内部 ----------------------------------------------------------------

    def _advance_levels(self, search_id, record):
        """表数层全部单元完成时推进：
        - 该层已找到可行解 -> 立即锁定最优（更少表数的层此前均已搜遍无解）；
        - 无解且还有更上层 -> 开下一层单元；
        - 已到上限仍无解 -> 留给 evaluate 判 infeasible。
        """
        while True:
            stats = record["stats"]
            open_units = stats["units_pending"] + stats["units_running"]
            if open_units != 0:
                return record
            level = stats["exhausted_level"] + 1
            best = record.get("best_solution")
            if best is not None:
                # 当前活跃层已清空且手里有解：该解即表数最少、重复最少的最优解。
                stats["exhausted_level"] = max(stats["exhausted_level"], best["table_count"])
                self.store.set_exhausted_level(search_id, stats["exhausted_level"])
                return record
            if level > record["spec"]["max_tables"]:
                stats["exhausted_level"] = record["spec"]["max_tables"]
                self.store.set_exhausted_level(search_id, stats["exhausted_level"])
                return record
            self.store.append_event(
                search_id, f"level-exhausted:{level}",
                f"{level} 张表的全部合规组合已排除" if level > 1 else "单表方案已排除",
            )
            stats["exhausted_level"] = level
            self.store.set_exhausted_level(search_id, level)
            next_level = level + 1
            if next_level <= record["spec"]["max_tables"]:
                units = self.orchestrator.next_level_units(record, next_level)
                if units:
                    self.store.add_units(search_id, units)
                    self.store.append_event(
                        search_id, f"level-open:{next_level}",
                        f"开启 {next_level} 张表层级，生成 {len(units)} 个工作单元",
                    )
            record = self.store.load_search(search_id)

    def _finalize_optimal(self, search_id, record):
        problem = Orchestrator._problem(record)
        best = record["best_solution"]
        report = {
            "spec": record["spec"],
            "candidate_keys": [theory._names_of(k, problem.names) for k in problem.keys],
            "minimal_cover": [
                {"lhs": theory._names_of(lhs, problem.names),
                 "rhs": theory._names_of(rhs, problem.names)}
                for lhs, rhs in problem.cover
            ],
            "precheck": record["precheck"],
            "search_stats": record["stats"],
        }
        from .search import solution_evidence
        report.update(solution_evidence(problem, best))
        report["optimality"] = {
            "proved": True,
            "reason": (
                f"1..{best['table_count'] - 1} 张表层级已全部搜遍无解；"
                f"在 {best['table_count']} 张表层级按重复存储量取到最小值 "
                f"{best['duplication']}"
            ),
        }
        report["checksum"] = hashlib.sha256(
            json.dumps(report, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        self.store.save_report(search_id, report)
        self.store.append_event(
            search_id, "optimal",
            f"已证明最优：{best['table_count']} 张表，重复存储 {best['duplication']} 个属性",
        )

    @staticmethod
    def solve_sync(store, search_id, spec, worker_id="sync"):
        """测试/CLI 便捷方法：建案并一轮跑到终态（时限由 spec 给定）。"""
        runner = SearchRunner(store)
        runner.initialize(search_id, spec)
        return runner.run_with_budget(search_id, worker_id, spec["time_limit_seconds"])
