"""受限分表搜索任务服务：建案、入队、续跑、查询与导出。

队列优先 Redis；Redis 不可用时降级为进程内 deque，并在响应中标记，
保证本地无 Redis 也能完整演示求解/续跑（生产仍是持久 Redis 队列）。
"""
from __future__ import annotations

import json
from collections import deque

from fastapi import HTTPException
from sqlalchemy import select

from src.config.database import Session, engine
from src.models.entities import Relation, Dependency, SearchTask, SearchEvent
from src.solver.runner import SearchRunner
from src.solver.solver_states import (
    STATE_QUEUED, STATE_WAITING, STATE_OPTIMAL, STATE_INFEASIBLE, STATE_FAILED,
    STATE_LABELS,
)
from src.services.common import save
from src.utils.serialization import record
from .search_store import SqlSearchStore

QUEUE_KEY = "schemaworkbook:search-queue"
CLASSIC_QUEUE_KEY = "schemaworkbook:queue"

try:  # pragma: no cover - 取决于运行环境
    from src.services.reports import queue as redis_queue
    _REDIS_IMPORTED = True
except Exception:  # pragma: no cover
    redis_queue = None
    _REDIS_IMPORTED = False

_memory_queue = deque()
# 熔断标志：Redis 连接失败后置 True，避免每个请求都等待连接超时；
# 进程重启或下次成功操作时复位。
_redis_dead = False


def queue_backend():
    if not _REDIS_IMPORTED or _redis_dead:
        return "memory"
    try:
        redis_queue.ping()
        return "redis"
    except Exception:
        return "memory"


def enqueue(task_id: int):
    if _REDIS_IMPORTED and not _redis_dead:
        try:
            redis_queue.rpush(QUEUE_KEY, str(task_id))
            return
        except Exception:  # pragma: no cover
            globals()["_redis_dead"] = True
    _memory_queue.append(str(task_id))


def dequeue(timeout=2):
    if _REDIS_IMPORTED and not _redis_dead:
        try:
            item = redis_queue.blpop(QUEUE_KEY, timeout=timeout)
            if item:
                return int(item[1])
            return None
        except Exception:  # pragma: no cover
            globals()["_redis_dead"] = True
    return int(_memory_queue.popleft()) if _memory_queue else None


# 经典分解队列复用同一套 Redis/内存弹性抽象（独立 key，互不串扰）。
_memory_classic_queue = deque()


def enqueue_classic(task_id: int):
    if _REDIS_IMPORTED and not _redis_dead:
        try:
            redis_queue.rpush(CLASSIC_QUEUE_KEY, str(task_id))
            return
        except Exception:  # pragma: no cover
            globals()["_redis_dead"] = True
    _memory_classic_queue.append(str(task_id))


def dequeue_classic(timeout=2):
    if _REDIS_IMPORTED and not _redis_dead:
        try:
            item = redis_queue.blpop(CLASSIC_QUEUE_KEY, timeout=timeout)
            if item:
                return int(item[1])
            return None
        except Exception:  # pragma: no cover
            globals()["_redis_dead"] = True
    return int(_memory_classic_queue.popleft()) if _memory_classic_queue else None


def store() -> SqlSearchStore:
    return SqlSearchStore(Session, dialect_name=engine.dialect.name)


# ---- 建案 -------------------------------------------------------------------

def create_task(db, data, user):
    relation = db.get(Relation, data.relation_id)
    if not relation:
        raise HTTPException(404, "关系不存在")
    deps = list(db.scalars(select(Dependency).where(
        Dependency.relation_id == relation.id)))
    spec = {
        "attributes": list(relation.attributes),
        "dependencies": [{"lhs": d.lhs, "rhs": d.rhs} for d in deps],
        "max_tables": data.max_tables,
        "max_attrs_per_table": data.max_attrs_per_table,
        "time_limit_seconds": data.time_limit_seconds,
    }
    snapshot = {"relation": record(relation),
                "dependencies": [{"lhs": d.lhs, "rhs": d.rhs} for d in deps]}
    # 冻结快照随 spec 一并持久化；后续草案/依赖修改不影响已建任务。
    task = SearchTask(
        name=data.name, relation_id=relation.id, status=STATE_QUEUED,
        spec={**spec, "snapshot": snapshot}, stats={},
    )
    saved = save(db, task, user, "受限分表建案 " + data.name)
    enqueue(task.id)
    return saved


def listing(db):
    rows = db.scalars(select(SearchTask).order_by(SearchTask.id.desc())).all()
    return [_view(row) for row in rows]


def detail(db, task_id):
    task = db.get(SearchTask, task_id)
    if not task:
        raise HTTPException(404, "搜索任务不存在")
    view = _view(task)
    with Session() as session:
        events = session.scalars(select(SearchEvent).where(
            SearchEvent.task_id == task_id).order_by(SearchEvent.id)).all()
        view["events"] = [{"event_key": e.event_key, "message": e.message,
                           "at": e.created_at.isoformat() if e.created_at else None}
                          for e in events]
    return view


def resume(db, task_id, user):
    task = db.get(SearchTask, task_id)
    if not task:
        raise HTTPException(404, "搜索任务不存在")
    if task.status in (STATE_OPTIMAL, STATE_INFEASIBLE, STATE_FAILED):
        return _view(task)  # 终态幂等
    enqueue(task.id)
    from src.models.entities import Audit
    db.add(Audit(actor=user.email, action=f"受限分表续跑 #{task_id}"))
    db.commit()
    return _view(task)


def export(db, task_id):
    task = db.get(SearchTask, task_id)
    if not task:
        raise HTTPException(404, "搜索任务不存在")
    if not task.report:
        raise HTTPException(409, "任务尚未得到终态报告，无可导出内容")
    return task.report


# ---- worker 入口 ------------------------------------------------------------

def execute_task(task_id: int, worker_id: str, budget_override: float = None):
    """worker 消费：建案（首次）或沿检查点续跑。

    budget_override 用于测试/手动续跑指定本轮预算；生产取 spec 中的时限。
    """
    sql_store = store()
    record = sql_store.load_search(task_id)
    spec = record["spec"]
    budget = float(budget_override) if budget_override else float(
        spec.get("time_limit_seconds", 5.0))
    runner = SearchRunner(sql_store)
    if record["status"] == STATE_QUEUED:
        runner.initialize(task_id, _pure_spec(spec))
    runner.run_with_budget(task_id, worker_id, budget)


def _pure_spec(spec):
    """去掉建案时附带的快照，只留求解器需要的字段。"""
    return {
        "attributes": spec["attributes"],
        "dependencies": spec["dependencies"],
        "max_tables": spec["max_tables"],
        "max_attrs_per_table": spec["max_attrs_per_table"],
        "time_limit_seconds": spec["time_limit_seconds"],
    }


def _view(task: SearchTask):
    stats = dict(task.stats or {})
    return {
        "id": task.id,
        "name": task.name,
        "relation_id": task.relation_id,
        "status": task.status,
        "status_label": STATE_LABELS.get(task.status, task.status),
        "spec": _pure_spec(task.spec) if task.spec else None,
        "precheck": task.precheck,
        "best_solution": _solution_view(task.best_solution),
        "report": task.report,
        "error": task.error,
        "stats": stats,
        "queue_backend": queue_backend(),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _solution_view(solution):
    if not solution:
        return None
    # solution 内部关系是位集，不直接对外；对外只暴露表数与重复量等标量。
    return {
        "table_count": solution["table_count"],
        "duplication": solution["duplication"],
        "total_stored": solution["total_stored"],
    }
