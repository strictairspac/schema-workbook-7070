"""求解器单元测试（纯标准库，无数据库/网络依赖）。

覆盖：
- theory：闭包、候选键、最小覆盖、3NF、无损、依赖保持
- 搜索与状态机：最优/无解、检查点续跑、并发只推进一份报告、幂等不重复发布
- 与独立暴力枚举的交叉验证
"""
import itertools
import os
import random
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.solver import theory
from src.solver.memory_store import InMemorySearchStore
from src.solver.runner import SearchRunner
from src.solver.search import Problem
from src.solver.solver_states import (
    STATE_OPTIMAL, STATE_INFEASIBLE, STATE_WAITING,
    UNIT_DONE, UNIT_CHECKPOINTED,
)


def build_problem(attrs, fds, k, m, limit=10.0):
    return Problem.build(attrs, fds, k, m, limit)


class TheoryTests(unittest.TestCase):
    def test_closure_basic(self):
        # A->B, B->C => A+ = ABC
        fds = [(0b001, 0b010), (0b010, 0b100)]
        self.assertEqual(theory.closure(0b001, fds), 0b111)
        self.assertEqual(theory.closure(0b100, fds), 0b100)

    def test_candidate_keys_classic(self):
        # AB->C, C->D, D->E；唯一候选键 AB
        names = ["A", "B", "C", "D", "E"]
        bit = {n: 1 << i for i, n in enumerate(names)}
        fds = [
            (bit["A"] | bit["B"], bit["C"]),
            (bit["C"], bit["D"]),
            (bit["D"], bit["E"]),
        ]
        keys = theory.candidate_keys(0b11111, fds)
        self.assertEqual(keys, [bit["A"] | bit["B"]])

    def test_candidate_keys_cyclic(self):
        # A<->B + 独立 C：候选键 AC、BC
        fds = [(0b001, 0b010), (0b010, 0b001)]
        keys = theory.candidate_keys(0b111, fds)
        self.assertEqual(sorted(keys), [0b101, 0b110])

    def test_minimal_cover_removes_redundancy(self):
        # A->B, A->C, B->C：A->C 冗余
        fds = [(0b001, 0b010), (0b001, 0b100), (0b010, 0b100)]
        cover = theory.minimal_cover(fds)
        self.assertNotIn((0b001, 0b100), cover)
        self.assertTrue(theory.equivalent_fds(fds, cover))

    def test_minimal_cover_no_empty_lhs(self):
        # 链 A->B->C->D->E：化简不得产生空左部
        fds = [(1, 2), (2, 4), (4, 8), (8, 16)]
        cover = theory.minimal_cover(fds)
        self.assertTrue(all(lhs != 0 for lhs, _ in cover))
        self.assertTrue(theory.equivalent_fds(fds, cover))

    def test_lossless_and_preservation_delegation(self):
        # 教科书合成结果对 AB->C,C->D,D->E 无损且保持依赖
        names = ["A", "B", "C", "D", "E"]
        fds = [(0b011, 0b100), (0b100, 0b1000), (0b1000, 0b10000)]
        rels = [0b111, 0b1100, 0b11000]
        chase = theory.lossless_chase(0b11111, rels, fds, names)
        self.assertTrue(chase["lossless"])
        preserved, _ = theory.dependency_preserved(rels, fds)
        self.assertTrue(preserved)

    def test_lossy_decomposition_detected(self):
        # 两表无共同超键属性时应判有损
        names = ["A", "B", "C"]
        fds = [(0b001, 0b010)]  # A->B
        rels = [0b001, 0b110]   # {A}, {B,C}
        chase = theory.lossless_chase(0b111, rels, fds, names)
        self.assertFalse(chase["lossless"])


class OptimalAndInfeasibleTests(unittest.TestCase):
    def solve(self, spec):
        store = InMemorySearchStore()
        record = SearchRunner.solve_sync(store, 1, spec, "test")
        return store, record

    def test_classic_synthesis_optimal(self):
        spec = {
            "attributes": ["A", "B", "C", "D", "E"],
            "dependencies": [
                {"lhs": ["A", "B"], "rhs": ["C"]},
                {"lhs": ["C"], "rhs": ["D"]},
                {"lhs": ["D"], "rhs": ["E"]},
            ],
            "max_tables": 3, "max_attrs_per_table": 3, "time_limit_seconds": 10.0,
        }
        store, record = self.solve(spec)
        self.assertEqual(record["status"], STATE_OPTIMAL)
        report = store.get_report(1)
        self.assertEqual(report["table_count"], 3)
        self.assertTrue(report["all_3nf"])
        self.assertTrue(report["dependency_preserved"])
        self.assertTrue(report["chase"]["lossless"])
        self.assertEqual(len(report["decomposition"]), 3)

    def test_no_dependencies_single_table(self):
        spec = {
            "attributes": ["批号", "容器", "记录人"], "dependencies": [],
            "max_tables": 2, "max_attrs_per_table": 3, "time_limit_seconds": 5.0,
        }
        store, record = self.solve(spec)
        self.assertEqual(record["status"], STATE_OPTIMAL)
        self.assertEqual(store.get_report(1)["table_count"], 1)

    def test_capacity_infeasible(self):
        spec = {
            "attributes": ["A", "B", "C", "D", "E"],
            "dependencies": [{"lhs": ["A"], "rhs": ["B"]}],
            "max_tables": 2, "max_attrs_per_table": 2, "time_limit_seconds": 5.0,
        }
        _, record = self.solve(spec)
        self.assertEqual(record["status"], STATE_INFEASIBLE)

    def test_chain_tight_limit_infeasible_by_search(self):
        spec = {
            "attributes": ["A", "B", "C", "D", "E"],
            "dependencies": [
                {"lhs": ["A"], "rhs": ["B"]}, {"lhs": ["B"], "rhs": ["C"]},
                {"lhs": ["C"], "rhs": ["D"]}, {"lhs": ["D"], "rhs": ["E"]},
            ],
            "max_tables": 3, "max_attrs_per_table": 2, "time_limit_seconds": 10.0,
        }
        _, record = self.solve(spec)
        self.assertEqual(record["status"], STATE_INFEASIBLE)
        self.assertGreater(record["stats"]["nodes"], 0)  # 由精确搜索而非预检得出

    def test_indirect_preservation_feasible(self):
        # X1->X0, X0->X1X2，M=2：X0->X2 经 X1 间接保持，必须判可行（回归用例）
        spec = {
            "attributes": ["X0", "X1", "X2"],
            "dependencies": [
                {"lhs": ["X1"], "rhs": ["X0"]},
                {"lhs": ["X0"], "rhs": ["X1", "X2"]},
            ],
            "max_tables": 2, "max_attrs_per_table": 2, "time_limit_seconds": 5.0,
        }
        store, record = self.solve(spec)
        self.assertEqual(record["status"], STATE_OPTIMAL)
        report = store.get_report(1)
        self.assertEqual((report["table_count"], report["duplication"]), (2, 1))

    def test_invalid_input_fails(self):
        spec = {
            "attributes": [f"A{i}" for i in range(11)], "dependencies": [],
            "max_tables": 1, "max_attrs_per_table": 1, "time_limit_seconds": 5.0,
        }
        store = InMemorySearchStore()
        runner = SearchRunner(store)
        with self.assertRaises(ValueError):
            runner.initialize(1, spec)
        self.assertEqual(store.load_search(1)["status"], "failed")


class CheckpointResumeTests(unittest.TestCase):
    def setUp(self):
        self.spec = {
            "attributes": ["A", "B", "C", "D", "E", "F", "G"],
            "dependencies": [
                {"lhs": ["A", "B"], "rhs": ["C"]},
                {"lhs": ["C"], "rhs": ["D"]},
                {"lhs": ["D"], "rhs": ["E"]},
                {"lhs": ["E"], "rhs": ["F"]},
                {"lhs": ["A", "F"], "rhs": ["G"]},
            ],
            "max_tables": 5, "max_attrs_per_table": 3, "time_limit_seconds": 5.0,
        }
        ref_store = InMemorySearchStore()
        self.ref_record = SearchRunner.solve_sync(ref_store, 1, self.spec, "ref")
        self.ref_report = ref_store.get_report(1)

    def test_resume_reaches_same_optimum(self):
        store = InMemorySearchStore()
        runner = SearchRunner(store)
        runner.initialize(1, self.spec)
        saw_waiting = False
        for _ in range(500):
            record = runner.run_with_budget(1, "w", 0.06)
            if record["status"] == STATE_WAITING:
                saw_waiting = True
            if record["status"] in (STATE_OPTIMAL, STATE_INFEASIBLE, "failed"):
                break
        self.assertTrue(saw_waiting)
        self.assertEqual(record["status"], STATE_OPTIMAL)
        report = store.get_report(1)
        self.assertEqual(report["decomposition"], self.ref_report["decomposition"])
        self.assertEqual(report["duplication"], self.ref_report["duplication"])

    def test_terminal_retry_is_idempotent(self):
        store = InMemorySearchStore()
        runner = SearchRunner(store)
        runner.initialize(1, self.spec)
        record = runner.run_with_budget(1, "w", 5.0)
        self.assertEqual(record["status"], STATE_OPTIMAL)
        before = len(store.list_events(1))
        again = runner.run_with_budget(1, "retry", 1.0)
        after = len(store.list_events(1))
        self.assertEqual(again["status"], STATE_OPTIMAL)
        self.assertEqual(before, after)  # 重试不重复发布
        keys = [e["event_key"] for e in store.list_events(1)]
        self.assertEqual(len(keys), len(set(keys)))


class ConcurrencyTests(unittest.TestCase):
    def test_parallel_workers_single_report(self):
        spec = {
            "attributes": ["A", "B", "C", "D", "E", "F", "G"],
            "dependencies": [
                {"lhs": ["A", "B"], "rhs": ["C"]},
                {"lhs": ["C"], "rhs": ["D"]},
                {"lhs": ["D"], "rhs": ["E"]},
                {"lhs": ["E"], "rhs": ["F"]},
                {"lhs": ["A", "F"], "rhs": ["G"]},
            ],
            "max_tables": 5, "max_attrs_per_table": 3, "time_limit_seconds": 5.0,
        }
        store = InMemorySearchStore()
        runner = SearchRunner(store)
        runner.initialize(1, spec)
        stop = threading.Event()
        errors = []

        def worker(wid):
            try:
                while not stop.is_set():
                    rec = runner.run_with_budget(1, f"worker-{wid}", 0.07)
                    if rec["status"] in (STATE_OPTIMAL, STATE_INFEASIBLE, "failed"):
                        return
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if store.load_search(1)["status"] in (STATE_OPTIMAL, STATE_INFEASIBLE, "failed"):
                break
            time.sleep(0.05)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.assertFalse(errors)
        self.assertEqual(store.load_search(1)["status"], STATE_OPTIMAL)
        # 无租约泄漏、无重复事件
        self.assertFalse(any(u["status"] == "running" for u in store.units.values()))
        terminal = [u for u in store.units.values()
                    if u["status"] in (UNIT_DONE, UNIT_CHECKPOINTED)]
        self.assertEqual(len(terminal), len(store.units))
        keys = [e["event_key"] for e in store.list_events(1)]
        self.assertEqual(len(keys), len(set(keys)))


class BruteForceCrossCheckTests(unittest.TestCase):
    """与独立暴力枚举交叉验证（小实例，全枚举子集族）。"""

    @staticmethod
    def brute(names, fds, max_tables, max_attrs):
        bit = {a: 1 << i for i, a in enumerate(names)}
        full = (1 << len(names)) - 1
        norm = []
        for dep in fds:
            lhs = 0
            for a in dep["lhs"]:
                lhs |= bit[a]
            for a in dep["rhs"]:
                rhs = bit[a]
                if not (lhs & rhs):
                    norm.append((lhs, rhs))
        norm = list(dict.fromkeys(norm))
        subsets = []
        for size in range(1, max_attrs + 1):
            for combo in itertools.combinations(range(len(names)), size):
                rel = 0
                for i in combo:
                    rel |= 1 << i
                subsets.append(rel)
        best = None
        for k in range(1, max_tables + 1):
            for combo in itertools.combinations_with_replacement(range(len(subsets)), k):
                rels = [subsets[i] for i in combo]
                union = 0
                for rel in rels:
                    union |= rel
                if union != full:
                    continue
                if not all(theory.is_3nf_relation(r, norm)[0] for r in rels):
                    continue
                if not theory.dependency_preserved(rels, norm)[0]:
                    continue
                if not theory.lossless_chase(full, rels, norm, names)["lossless"]:
                    continue
                dup = sum(r.bit_count() for r in rels) - len(names)
                if best is None or (k, dup) < best:
                    best = (k, dup)
            if best is not None:
                return best
        return None

    def test_random_instances_match_brute_force(self):
        rng = random.Random(20261118)
        for _ in range(25):
            n = rng.randint(3, 6)
            names = [f"X{i}" for i in range(n)]
            fds = []
            for _ in range(rng.randint(0, 5)):
                lhs = rng.sample(names, rng.randint(1, max(1, n - 1)))
                remaining = [a for a in names if a not in lhs]
                if remaining:
                    fds.append({"lhs": lhs,
                                "rhs": rng.sample(remaining, rng.randint(1, min(2, len(remaining))))})
            uniq = {(tuple(sorted(d["lhs"])), tuple(sorted(d["rhs"]))) for d in fds}
            fds = [{"lhs": list(l), "rhs": list(r)} for l, r in uniq]
            k, m = rng.randint(1, 4), rng.randint(2, n)
            expected = self.brute(names, fds, k, m)
            spec = {"attributes": names, "dependencies": fds, "max_tables": k,
                    "max_attrs_per_table": m, "time_limit_seconds": 10.0}
            store = InMemorySearchStore()
            record = SearchRunner.solve_sync(store, 1, spec, "verify")
            if expected is None:
                self.assertEqual(record["status"], STATE_INFEASIBLE)
            else:
                self.assertEqual(record["status"], STATE_OPTIMAL)
                report = store.get_report(1)
                self.assertEqual((report["table_count"], report["duplication"]), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
