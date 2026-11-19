"""内存版 SolverStore：零依赖，供单元测试与本地无数据库演示使用。

语义与生产 SQL 版严格对齐：原子认领、幂等单元、按 (表数, 重复量)
合并最优解、事件去重、租约过期可重领。线程锁保证并发续跑正确性。
"""
from __future__ import annotations

import threading
import time
import uuid

from . import theory
from .orchestrator import SolverStore
from .search import Problem, solution_evidence
from .solver_states import UNIT_PENDING, UNIT_RUNNING, UNIT_DONE, UNIT_CHECKPOINTED


class InMemorySearchStore(SolverStore):
    def __init__(self):
        self._lock = threading.RLock()
        self.searches = {}
        self.units = {}        # (search_id, unit_key) -> dict
        self.events = {}       # search_id -> list[dict]
        self.heartbeats = {}   # search_id -> {worker_id: ts}

    # ---- 测试辅助 ------------------------------------------------------------

    def create_search(self, search_id, spec):
        with self._lock:
            self.searches[search_id] = {
                "id": search_id,
                "spec": spec,
                "status": "running",
                "precheck": None,
                "best_solution": None,
                "stats": {
                    "nodes": 0, "pruned": 0, "leaves": 0,
                    "units_pending": 0, "units_running": 0, "units_done": 0,
                    "exhausted_level": 0, "runs": 0,
                },
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            self.events[search_id] = []
            self.heartbeats[search_id] = {}
            return self.searches[search_id]

    def add_units(self, search_id, units):
        with self._lock:
            for payload in units:
                key = (search_id, payload["unit_key"])
                if key in self.units:
                    continue  # 幂等：重试不重复发布
                self.units[key] = {
                    "search_id": search_id,
                    "unit_key": payload["unit_key"],
                    "level_limit": payload["level_limit"],
                    "stack": payload["stack"],
                    "status": UNIT_PENDING,
                    "lease_owner": None,
                    "lease_expires": 0.0,
                    "attempts": 0,
                }
                self.searches[search_id]["stats"]["units_pending"] += 1
            self._touch(search_id)

    def set_precheck(self, search_id, precheck):
        with self._lock:
            self.searches[search_id]["precheck"] = precheck
            self._touch(search_id)

    def snapshot(self, search_id):
        with self._lock:
            record = dict(self.searches[search_id])
            record["stats"] = dict(record["stats"])
            return record

    # ---- SolverStore 接口 ----------------------------------------------------

    def load_search(self, search_id):
        with self._lock:
            return self.snapshot(search_id)

    def claim_unit(self, search_id, worker_id, lease_seconds):
        with self._lock:
            now = time.monotonic()
            for unit in self._ordered_units(search_id):
                if unit["status"] == UNIT_PENDING or (
                    unit["status"] == UNIT_RUNNING and unit["lease_expires"] <= now
                ):
                    was_pending = unit["status"] == UNIT_PENDING
                    unit["status"] = UNIT_RUNNING
                    unit["lease_owner"] = worker_id
                    unit["lease_expires"] = now + lease_seconds
                    unit["attempts"] += 1
                    stats = self.searches[search_id]["stats"]
                    if was_pending:
                        stats["units_pending"] -= 1
                    stats["units_running"] += 1
                    self._touch(search_id)
                    return {
                        "unit_key": unit["unit_key"],
                        "level_limit": unit["level_limit"],
                        "stack": unit["stack"],
                    }
            return None

    def release_unit(self, search_id, unit_key):
        with self._lock:
            unit = self.units[(search_id, unit_key)]
            if unit["status"] != UNIT_RUNNING:
                return
            unit["status"] = UNIT_PENDING
            unit["lease_owner"] = None
            unit["lease_expires"] = 0.0
            stats = self.searches[search_id]["stats"]
            stats["units_running"] -= 1
            stats["units_pending"] += 1
            self._touch(search_id)

    def complete_unit(self, search_id, unit_key, status, spawned, solution, stats, worker_id):
        with self._lock:
            unit = self.units[(search_id, unit_key)]
            # 幂等：旧租约的迟到完成必须丢弃，避免重复改状态/计数/发布。
            if unit["status"] in (UNIT_DONE, UNIT_CHECKPOINTED):
                return self.snapshot(search_id)
            if unit["status"] == UNIT_RUNNING and unit.get("lease_owner") is not None \
                    and unit["lease_owner"] != worker_id:
                return self.snapshot(search_id)  # 已被其他 worker 接管
            record = self.searches[search_id]
            record["stats"]["nodes"] += stats["nodes"]
            record["stats"]["pruned"] += stats["pruned"]
            record["stats"]["leaves"] += stats["leaves"]
            record["stats"]["units_running"] -= 1

            if status == "done":
                unit["status"] = UNIT_DONE
                record["stats"]["units_done"] += 1
                self.append_event(search_id, f"done:{unit_key}",
                                  f"工作单元 {unit_key} 已搜遍（worker {worker_id}）")
            else:
                unit["status"] = UNIT_CHECKPOINTED
                record["stats"]["units_done"] += 1
                self.append_event(search_id, f"checkpoint:{unit_key}",
                                  f"工作单元 {unit_key} 到时检查点，拆出 {len(spawned)} 个续跑单元")
            unit["lease_owner"] = None
            unit["lease_expires"] = 0.0
            self.add_units(search_id, spawned)

            if solution is not None:
                self._merge_best(search_id, solution, worker_id)
            self._touch(search_id)
            return self.snapshot(search_id)

    def heartbeat(self, search_id, worker_id):
        with self._lock:
            self.heartbeats[search_id][worker_id] = time.time()

    def set_exhausted_level(self, search_id, level):
        with self._lock:
            self.searches[search_id]["stats"]["exhausted_level"] = level
            self._touch(search_id)

    def append_event(self, search_id, event_key, message):
        with self._lock:
            events = self.events[search_id]
            if any(item["event_key"] == event_key for item in events):
                return  # 幂等：重试不重复发布
            events.append({"event_key": event_key, "message": message, "at": time.time()})

    def mark_run_started(self, search_id, worker_id):
        with self._lock:
            self.searches[search_id]["stats"]["runs"] += 1
            self.append_event(
                search_id,
                f"run:{self.searches[search_id]['stats']['runs']}:{worker_id}",
                f"worker {worker_id} 开始/续跑求解",
            )
            self._touch(search_id)

    def mark_status(self, search_id, status):
        with self._lock:
            self.searches[search_id]["status"] = status
            self._touch(search_id)

    def save_report(self, search_id, report):
        with self._lock:
            self.searches[search_id]["report"] = report
            self._touch(search_id)

    def get_report(self, search_id):
        with self._lock:
            return self.searches[search_id].get("report")

    def list_events(self, search_id):
        with self._lock:
            return list(self.events[search_id])

    # ---- 内部 ----------------------------------------------------------------

    def _merge_best(self, search_id, solution, worker_id):
        record = self.searches[search_id]
        current = record["best_solution"]
        candidate_tuple = (solution["table_count"], solution["duplication"])
        if current is None or candidate_tuple < (current["table_count"], current["duplication"]):
            record["best_solution"] = dict(solution)
            self.append_event(
                search_id,
                f"best:{candidate_tuple[0]}:{candidate_tuple[1]}:{uuid.uuid4().hex[:8]}",
                f"发现更优方案：{candidate_tuple[0]} 张表，重复存储 {candidate_tuple[1]} 个属性"
                f"（worker {worker_id}）",
            )

    def _ordered_units(self, search_id):
        return [self.units[k] for k in sorted(self.units) if k[0] == search_id]

    def _touch(self, search_id):
        self.searches[search_id]["updated_at"] = time.time()


def build_report(problem: Problem, solution):
    """把最优解与理论证据组装成对外报告 JSON 片段。"""
    evidence = solution_evidence(problem, solution)
    return {
        "candidate_keys": [theory._names_of(k, problem.names) for k in problem.keys],
        "minimal_cover": [
            {"lhs": theory._names_of(lhs, problem.names),
             "rhs": theory._names_of(rhs, problem.names)}
            for lhs, rhs in problem.cover
        ],
        **evidence,
    }
