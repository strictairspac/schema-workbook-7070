"""FastAPI 端到端测试：SQLite + 内存队列，覆盖受限搜索的完整 HTTP 流程。"""
import os
import sys
import tempfile
import unittest

# 在导入应用前切换数据库到文件型 SQLite，并关闭 Redis 依赖。
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP.name}"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["SEED_DEMO"] = "false"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from src.main import app
from src.config.database import Session
from src.models.entities import User, Relation, Dependency
from src.utils.security import password_hash
from src.services import searches as search_service


class SearchApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 上下文管理器触发 lifespan，执行 metadata.create_all 建表。
        cls._cm = TestClient(app)
        cls.client = cls._cm.__enter__()
        with Session() as db:
            for role, email in (("admin", "admin@nanlu.test"),
                                ("operator", "operator@nanlu.test"),
                                ("viewer", "viewer@nanlu.test")):
                if not db.query(User).filter_by(email=email).first():
                    db.add(User(email=email, password=password_hash("Demo2026!"), role=role))
            rel = Relation(code="T-API", title="接口测试",
                           attributes=["A", "B", "C", "D", "E"])
            db.add(rel)
            db.flush()
            for lhs, rhs in [
                (["A", "B"], ["C"]), (["C"], ["D"]), (["D"], ["E"]),
            ]:
                db.add(Dependency(relation_id=rel.id, lhs=lhs, rhs=rhs))
            db.commit()
            cls.relation_id = rel.id

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)

    def _token(self, email):
        res = self.client.post("/auth/login", json={"email": email, "password": "Demo2026!"})
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["token"]

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}

    def test_full_search_lifecycle(self):
        token = self._token("operator@nanlu.test")
        # 建案
        res = self.client.post("/searches", headers=self._auth(token), json={
            "name": "api-受限求解", "relation_id": self.relation_id,
            "max_tables": 3, "max_attrs_per_table": 3, "time_limit_seconds": 5.0,
        })
        self.assertEqual(res.status_code, 201, res.text)
        task_id = res.json()["id"]

        # 同步驱动 worker 消费队列（内存队列降级）
        self._drain_queue()

        detail = self.client.get(f"/searches/{task_id}", headers=self._auth(token)).json()
        self.assertIn(detail["status"], ("optimal", "infeasible", "waiting", "running"))
        # 该实例应为最优
        self.assertEqual(detail["status"], "optimal")
        self.assertEqual(detail["status_label"], "已最优")
        self.assertTrue(detail["report"]["all_3nf"])
        self.assertTrue(detail["report"]["dependency_preserved"])
        self.assertTrue(detail["report"]["chase"]["lossless"])
        self.assertEqual(detail["report"]["table_count"], 3)
        self.assertIn("checksum", detail["report"])
        self.assertGreater(len(detail["events"]), 0)

        # 列表
        listing = self.client.get("/searches", headers=self._auth(token)).json()
        self.assertTrue(any(item["id"] == task_id for item in listing))

        # 导出
        export = self.client.get(f"/searches/{task_id}/export",
                                 headers=self._auth(token))
        self.assertEqual(export.status_code, 200)
        self.assertIn("decomposition", export.json())

        # 终态续跑幂等
        before = len(detail["events"])
        resume = self.client.post(f"/searches/{task_id}/resume",
                                  headers=self._auth(token))
        self.assertEqual(resume.status_code, 202)
        again = self.client.get(f"/searches/{task_id}",
                                headers=self._auth(token)).json()
        self.assertEqual(len(again["events"]), before)

    def test_viewer_cannot_create(self):
        token = self._token("viewer@nanlu.test")
        res = self.client.post("/searches", headers=self._auth(token), json={
            "name": "viewer-禁止", "relation_id": self.relation_id,
            "max_tables": 2, "max_attrs_per_table": 3, "time_limit_seconds": 5.0,
        })
        self.assertEqual(res.status_code, 403)

    def test_validation_rejects_bad_limits(self):
        token = self._token("admin@nanlu.test")
        res = self.client.post("/searches", headers=self._auth(token), json={
            "name": "越界", "relation_id": self.relation_id,
            "max_tables": 0, "max_attrs_per_table": 3, "time_limit_seconds": 5.0,
        })
        self.assertEqual(res.status_code, 422)
        res2 = self.client.post("/searches", headers=self._auth(token), json={
            "name": "越界时限", "relation_id": self.relation_id,
            "max_tables": 2, "max_attrs_per_table": 3, "time_limit_seconds": 0,
        })
        self.assertEqual(res2.status_code, 422)

    def test_waiting_then_resume_reaches_optimal(self):
        token = self._token("operator@nanlu.test")
        res = self.client.post("/searches", headers=self._auth(token), json={
            "name": "续跑流程", "relation_id": self.relation_id,
            "max_tables": 3, "max_attrs_per_table": 3, "time_limit_seconds": 0.15,
        })
        task_id = res.json()["id"]
        # 多轮消费，模拟到时后续跑；用更大预算覆盖，验证 waiting->optimal 收敛。
        for _ in range(30):
            self._drain_queue(budget=1.0)
            detail = self.client.get(f"/searches/{task_id}",
                                     headers=self._auth(token)).json()
            if detail["status"] in ("optimal", "infeasible", "failed"):
                break
        self.assertEqual(detail["status"], "optimal")

    def test_classic_report_worker_consumption(self):
        """经典分解任务经弹性队列入队后，worker 逻辑能消费并产出报告。"""
        token = self._token("operator@nanlu.test")
        res = self.client.post("/reports", headers=self._auth(token), json={
            "name": "经典队列消费", "relation_id": self.relation_id,
        })
        self.assertEqual(res.status_code, 201, res.text)
        report_id = res.json()["id"]
        # 模拟 worker：从经典队列弹出并执行分析
        from src.services import searches as search_service
        from src.services import reports as report_service
        from src.models.entities import Report
        popped = search_service.dequeue_classic(timeout=0)
        self.assertEqual(popped, report_id)
        with Session() as db:
            from src.services.normalization import analyze
            obj = db.get(Report, report_id)
            snapshot = obj.input_snapshot
            obj.status = "running"
            db.commit()
            result = analyze(snapshot["relation"]["attributes"], snapshot["dependencies"])
            obj.result = result
            obj.status = "completed"
            db.commit()
        detail = self.client.get(f"/reports/{report_id}",
                                 headers=self._auth(token)).json()
        self.assertEqual(detail["status"], "completed")
        self.assertTrue(detail["result"]["all_3nf"])
        self.assertTrue(detail["result"]["chase"]["lossless"])

    def _drain_queue(self, budget=None):
        while True:
            task_id = search_service.dequeue(timeout=0)
            if task_id is None:
                return
            search_service.execute_task(task_id, "test-worker", budget_override=budget)


if __name__ == "__main__":
    unittest.main(verbosity=2)
