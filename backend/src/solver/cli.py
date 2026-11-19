"""受限 3NF 求解的零依赖命令行入口（便于本地验证与离线演示）。

用法：
  python -m src.solver.cli solve --input problem.json --state state.json
  python -m src.solver.cli resume --state state.json [--budget 2.0]

problem.json:
  {"attributes": [...], "dependencies": [{"lhs": [...], "rhs": [...]}],
   "max_tables": 3, "max_attrs_per_table": 3, "time_limit_seconds": 1.0}

state.json 保存内存存储的完整快照；每次 resume 沿原草案、原上限与
已落库检查点继续，已排除组合不会重复执行。
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid

from .memory_store import InMemorySearchStore
from .runner import SearchRunner
from .solver_states import STATE_LABELS


def _persist(store, path, search_id):
    snapshot = store.snapshot(search_id)
    prefix = f"{search_id}::"
    payload = {
        "search": snapshot,
        # _jsonable 已把 (sid, unit_key) 展平为 "sid::unit_key" 字符串。
        "units": {key: value for key, value in _jsonable(store.units).items()
                  if key.startswith(prefix)},
        "events": store.list_events(search_id),
        "report": store.get_report(search_id),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=_default)


def _jsonable(units):
    out = {}
    for (sid, key), value in units.items():
        out[f"{sid}::{key}"] = dict(value)
    return out


def _default(value):
    if isinstance(value, (set, tuple)):
        return list(value)
    return str(value)


def _load(store, path):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    search_id = payload["search"]["id"]
    store.searches[search_id] = payload["search"]
    for composite, unit in payload.get("units", {}).items():
        sid, key = composite.split("::", 1)
        # 内存存储的单元键是 (search_id, unit_key) 元组，sid 类型需与建案一致。
        unit["status"] = "pending" if unit["status"] == "running" else unit["status"]
        unit["lease_owner"] = None
        unit["lease_expires"] = 0.0
        store.units[(int(sid), key)] = unit
    store.events[search_id] = payload.get("events", [])
    store.heartbeats[search_id] = {}
    if payload.get("report"):
        store.searches[search_id]["report"] = payload["report"]
    return search_id


def solve(args):
    with open(args.input, encoding="utf-8") as handle:
        spec = json.load(handle)
    store = InMemorySearchStore()
    search_id = 1
    runner = SearchRunner(store)
    runner.initialize(search_id, spec)
    record = runner.run_with_budget(search_id, f"cli-{uuid.uuid4().hex[:6]}",
                                    spec["time_limit_seconds"])
    _persist(store, args.state, search_id)
    _print_summary(store, search_id, record)


def resume(args):
    store = InMemorySearchStore()
    search_id = _load(store, args.state)
    spec = store.snapshot(search_id)["spec"]
    budget = args.budget if args.budget is not None else spec["time_limit_seconds"]
    runner = SearchRunner(store)
    record = runner.run_with_budget(search_id, f"cli-{uuid.uuid4().hex[:6]}", budget)
    _persist(store, args.state, search_id)
    _print_summary(store, search_id, record)


def _print_summary(store, search_id, record):
    print(f"状态: {record['status']}（{STATE_LABELS.get(record['status'], record['status'])}）")
    print(f"统计: {json.dumps(record['stats'], ensure_ascii=False)}")
    best = record.get("best_solution")
    if best:
        print(f"当前最好: {best['table_count']} 张表, 重复存储 {best['duplication']} 个属性")
    report = store.get_report(search_id)
    if report:
        print("最优分解:")
        for index, relation in enumerate(report["decomposition"], 1):
            print(f"  R{index}({', '.join(relation)})")
        print(f"校验: 3NF={report['all_3nf']} 依赖保持={report['dependency_preserved']} "
              f"无损={report['chase']['lossless']}")
    if record["status"] in ("waiting",):
        print("提示: 使用 resume 子命令沿检查点续跑。", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description="受限 3NF 分表求解器")
    sub = parser.add_subparsers(dest="command", required=True)

    p_solve = sub.add_parser("solve", help="按输入 JSON 建案并求解一轮")
    p_solve.add_argument("--input", required=True)
    p_solve.add_argument("--state", required=True)
    p_solve.set_defaults(func=solve)

    p_resume = sub.add_parser("resume", help="沿状态文件续跑")
    p_resume.add_argument("--state", required=True)
    p_resume.add_argument("--budget", type=float, default=None)
    p_resume.set_defaults(func=resume)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
