"""SQL 存储端到端集成测试（SQLite 内存库，无需 MySQL/Redis）。

验证：
- 建表/建案、首轮求解、检查点续跑、终态报告与导出
- SQL 存储的原子认领、幂等单元、事件去重、最优合并
- 与内存存储参考结果一致
"""
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

# 必须在导入 src.config.database 之前切换到 SQLite，避免触发 MySQL 驱动加载。
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("JWT_SECRET", "test-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import entities
from src.models.entities import SearchTask
from src.services.search_store import SqlSearchStore
from src.solver.runner import SearchRunner
from src.solver.solver_states import STATE_OPTIMAL, STATE_INFEASIBLE, STATE_WAITING
from src.solver.memory_store import InMemorySearchStore


SPEC = {
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


class SqlStoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        # 用临时文件库而非 :memory:，使多线程/多连接共享同一份表与数据。
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        self.db_path = self._tmp.name
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        with self.engine.begin() as conn:
            from sqlalchemy import text
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.execute(text("PRAGMA busy_timeout=30000"))
        entities.Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False, future=True)
        self.store = SqlSearchStore(self.Session, dialect_name="sqlite")

    def tearDown(self):
        self.engine.dispose()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _create_task(self, spec):
        with self.Session() as db:
            task = SearchTask(name="t", relation_id=1, status="queued",
                              spec=dict(spec), stats={})
            db.add(task)
            db.commit()
            return task.id

    def test_full_run_matches_memory_reference(self):
        # 内存参考
        mem = InMemorySearchStore()
        ref = SearchRunner.solve_sync(mem, 1, SPEC, "ref")
        ref_report = mem.get_report(1)

        task_id = self._create_task(SPEC)
        runner = SearchRunner(self.store)
        record = self.store.load_search(task_id)
        runner.initialize(task_id, self._pure(record["spec"]))
        final = runner.run_with_budget(task_id, "w1", 5.0)

        self.assertEqual(final["status"], STATE_OPTIMAL)
        report = self.store.get_report_safely(task_id)
        self.assertEqual(report["decomposition"], ref_report["decomposition"])
        self.assertEqual(report["duplication"], ref_report["duplication"])
        self.assertTrue(report["all_3nf"])
        self.assertTrue(report["dependency_preserved"])
        self.assertTrue(report["chase"]["lossless"])
        self.assertIn("checksum", report)
        # 事件落库且键唯一
        events = self.store.list_events(task_id)
        keys = [e["event_key"] for e in events]
        self.assertEqual(len(keys), len(set(keys)))

    def test_resume_after_short_budget(self):
        task_id = self._create_task(SPEC)
        runner = SearchRunner(self.store)
        runner.initialize(task_id, self._pure(self.store.load_search(task_id)["spec"]))
        saw_waiting = False
        for _ in range(300):
            rec = runner.run_with_budget(task_id, "w", 0.06)
            if rec["status"] == STATE_WAITING:
                saw_waiting = True
            if rec["status"] in (STATE_OPTIMAL, STATE_INFEASIBLE, "failed"):
                break
        self.assertTrue(saw_waiting)
        self.assertEqual(rec["status"], STATE_OPTIMAL)

    def test_atomic_claim_never_returns_same_unit_twice(self):
        """确定性验证原子认领：多个 worker 顺序认领，每个单元恰好被领一次。"""
        task_id = self._create_task(SPEC)
        runner = SearchRunner(self.store)
        runner.initialize(task_id, self._pure(self.store.load_search(task_id)["spec"]))
        claimed = []
        while True:
            unit = self.store.claim_unit(task_id, "w", lease_seconds=60)
            if unit is None:
                break
            claimed.append(unit["unit_key"])
        self.assertEqual(len(claimed), len(set(claimed)))  # 无重复认领
        # 全部被领走后无 pending；释放一个即可重新认领
        self.assertIsNone(self.store.claim_unit(task_id, "w2", lease_seconds=60))
        self.store.release_unit(task_id, claimed[0])
        again = self.store.claim_unit(task_id, "w2", lease_seconds=60)
        self.assertEqual(again["unit_key"], claimed[0])

    def test_lease_expiry_allows_takeover_without_counter_drift(self):
        """租约过期可被接管；running 计数不重复累加，迟到完成被忽略。"""
        task_id = self._create_task(SPEC)
        runner = SearchRunner(self.store)
        runner.initialize(task_id, self._pure(self.store.load_search(task_id)["spec"]))
        initial = self.store.load_search(task_id)["stats"]["units_pending"]
        unit = self.store.claim_unit(task_id, "w1", lease_seconds=0.0)
        after_claim = self.store.load_search(task_id)["stats"]
        self.assertEqual(after_claim["units_running"], 1)
        self.assertEqual(after_claim["units_pending"], initial - 1)
        # 租约立即过期，另一 worker 接管（running 计数不应漂移）
        time.sleep(0.01)
        takeover = self.store.claim_unit(task_id, "w2", lease_seconds=60)
        self.assertEqual(takeover["unit_key"], unit["unit_key"])
        mid = self.store.load_search(task_id)["stats"]
        self.assertEqual(mid["units_running"], 1)
        self.assertEqual(mid["units_pending"], initial - 1)
        # 旧租约迟到完成必须被忽略；单元仍为 running，计数未被重复扣减
        self.store.complete_unit(task_id, unit["unit_key"], "done", [], None,
                                 {"nodes": 1, "pruned": 0, "leaves": 1}, "w1-stale")
        from src.models.entities import SearchUnit
        from sqlalchemy import select
        with self.Session() as db:
            row = db.scalar(select(SearchUnit).where(
                SearchUnit.task_id == task_id,
                SearchUnit.unit_key == unit["unit_key"]))
            self.assertEqual(row.status, "running")
        self.assertEqual(self.store.load_search(task_id)["stats"]["units_running"], 1)

    def test_idempotent_unit_and_event_inserts(self):
        """检查点单元与事件按键幂等，重试/并发不重复发布。"""
        task_id = self._create_task(SPEC)
        payload = {"unit_key": "dup-1", "level_limit": 1,
                   "stack": [{"path": [], "covered": 0, "embedded": 0,
                              "has_key": False, "cost": 0}]}
        self.store.add_units(task_id, [payload])
        self.store.add_units(task_id, [payload])  # 重复提交
        self.store.append_event(task_id, "e1", "hello")
        self.store.append_event(task_id, "e1", "hello")  # 重复事件
        from src.models.entities import SearchUnit, SearchEvent
        from sqlalchemy import select, func
        with self.Session() as db:
            n_units = db.scalar(select(func.count()).select_from(SearchUnit).where(
                SearchUnit.task_id == task_id, SearchUnit.unit_key == "dup-1"))
            n_events = db.scalar(select(func.count()).select_from(SearchEvent).where(
                SearchEvent.task_id == task_id, SearchEvent.event_key == "e1"))
            self.assertEqual(n_units, 1)
            self.assertEqual(n_events, 1)

    def test_infeasible_capacity(self):
        bad = dict(SPEC, max_tables=1, max_attrs_per_table=2,
                   attributes=["A", "B", "C", "D", "E"],
                   dependencies=[{"lhs": ["A"], "rhs": ["B"]}])
        task_id = self._create_task(bad)
        runner = SearchRunner(self.store)
        rec = runner.initialize(task_id, self._pure(bad))
        self.assertEqual(rec["status"], STATE_INFEASIBLE)

    @staticmethod
    def _pure(spec):
        return {k: spec[k] for k in (
            "attributes", "dependencies", "max_tables",
            "max_attrs_per_table", "time_limit_seconds")}


# 给 SqlSearchStore 补一个测试用读取报告入口（生产通过 task.report 列读取）
def _get_report_safely(self, task_id):
    with self.Session() as db:
        return db.get(SearchTask, task_id).report


SqlSearchStore.get_report_safely = _get_report_safely


if __name__ == "__main__":
    unittest.main(verbosity=2)
