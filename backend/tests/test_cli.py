"""CLI 状态文件持久化与续跑测试（子进程隔离，验证真正的落盘/加载往返）。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBLEM = {
    "attributes": ["A", "B", "C", "D", "E", "F", "G"],
    "dependencies": [
        {"lhs": ["A", "B"], "rhs": ["C"]},
        {"lhs": ["C"], "rhs": ["D"]},
        {"lhs": ["D"], "rhs": ["E"]},
        {"lhs": ["E"], "rhs": ["F"]},
        {"lhs": ["A", "F"], "rhs": ["G"]},
    ],
    "max_tables": 5, "max_attrs_per_table": 3, "time_limit_seconds": 0.01,
}


class CliPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.problem = os.path.join(self.tmp, "problem.json")
        self.state = os.path.join(self.tmp, "state.json")
        with open(self.problem, "w", encoding="utf-8") as fh:
            json.dump(PROBLEM, fh, ensure_ascii=False)
        self.env = {**os.environ, "PYTHONPATH": BACKEND}

    def _run(self, *args):
        proc = subprocess.run(
            [sys.executable, "-m", "src.solver.cli", *args],
            cwd=BACKEND, env=self.env, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_waiting_then_resume_reaches_optimal(self):
        out = self._run("solve", "--input", self.problem, "--state", self.state)
        self.assertIn("待续跑", out)
        # 检查点单元必须真正落盘，否则续跑无法恢复
        with open(self.state, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertGreater(len(data["units"]), 0)

        status = "待续跑"
        for _ in range(30):
            out = self._run("resume", "--state", self.state, "--budget", "0.08")
            first = out.splitlines()[0]
            if "已最优" in first or "无解" in first or "失败" in first:
                status = first
                break
        self.assertIn("已最优", status)
        # 终态报告含完整校验与校验和
        with open(self.state, encoding="utf-8") as fh:
            data = json.load(fh)
        report = data["report"]
        self.assertTrue(report["all_3nf"])
        self.assertTrue(report["dependency_preserved"])
        self.assertTrue(report["chase"]["lossless"])
        self.assertIn("checksum", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
