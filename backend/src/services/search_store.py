"""基于 SQLAlchemy 的 SolverStore 生产实现。

并发正确性靠数据库事务保证：
- claim_unit：行锁（SELECT ... FOR UPDATE，SQLite 退化为库级写锁）下挑选
  pending 或租约过期的 running 单元，并在同一事务内改 running，杜绝双领；
- 单元 (task_id, unit_key) 唯一：检查点续跑单元重复提交被 IntegrityError 吞掉；
- 事件 (task_id, event_key) 唯一：重试/并发不重复发布；
- 全局最优在同一事务按 (表数, 重复量) 字典序比较更新。

MySQL 8 与 SQLite 均可运行（JSON 列在 SQLite 上退化为 TEXT）。
"""
from __future__ import annotations

import time

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from src.models.entities import SearchTask, SearchUnit, SearchEvent
from src.solver.orchestrator import SolverStore
from src.solver.solver_states import (
    UNIT_PENDING, UNIT_RUNNING, UNIT_DONE, UNIT_CHECKPOINTED,
)


class SqlSearchStore(SolverStore):
    def __init__(self, session_factory, dialect_name="mysql"):
        # session_factory：返回上下文管理器会话（与 config.database.Session 同形）
        self.Session = session_factory
        self.dialect_name = dialect_name

    # ---- 基础读写 ------------------------------------------------------------

    def load_search(self, search_id):
        with self.Session() as db:
            task = db.get(SearchTask, search_id)
            if task is None:
                raise KeyError(f"搜索任务 {search_id} 不存在")
            return self._task_record(task)

    def create_search(self, search_id, spec):
        """幂等建案：任务通常已由服务层创建（带名称/关系引用）。

        若已存在则直接返回当前记录（续跑场景）；若不存在则用最小字段补建，
        保证 runner 接口在两种存储上同形。
        """
        with self.Session() as db:
            task = db.get(SearchTask, search_id)
            if task is not None:
                return self._task_record(task)
            task = SearchTask(
                id=search_id, name=f"search-{search_id}", relation_id=0,
                status="queued", spec=spec, stats={},
            )
            db.add(task)
            db.commit()
            return self._task_record(task)

    def heartbeat(self, search_id, worker_id):
        # 租约本身就是活性证明，心跳无需额外落库；保留接口以兼容编排器。
        return None

    def set_exhausted_level(self, search_id, level):
        with self.Session() as db:
            task = db.get(SearchTask, search_id)
            stats = dict(task.stats or {})
            stats["exhausted_level"] = level
            task.stats = stats
            db.commit()

    # ---- 单元认领/完成 -------------------------------------------------------

    def claim_unit(self, search_id, worker_id, lease_seconds):
        now = time.monotonic()
        with self.Session() as db:
            unit = self._lock_claimable(db, search_id, now)
            if unit is None:
                return None
            was_pending = unit.status == UNIT_PENDING
            unit.status = UNIT_RUNNING
            unit.lease_owner = worker_id
            unit.lease_expires = now + lease_seconds
            unit.attempts = (unit.attempts or 0) + 1
            if was_pending:
                # 仅 pending->running 改变计数；running(过期重领)->running 是
                # 同一逻辑单元的接管，不能再次累加 units_running，否则计数只增不减。
                self._adjust_stat(db, search_id, "units_pending", -1)
                self._adjust_stat(db, search_id, "units_running", +1)
            db.commit()
            return {
                "unit_key": unit.unit_key,
                "level_limit": unit.level_limit,
                "stack": unit.stack,
            }

    def _lock_claimable(self, db, search_id, now):
        stmt = select(SearchUnit).where(SearchUnit.task_id == search_id)
        if self.dialect_name == "sqlite":
            units = db.scalars(stmt.order_by(SearchUnit.id)).all()
        else:
            units = db.scalars(stmt.order_by(SearchUnit.id).with_for_update()).all()
        for unit in units:
            if unit.status == UNIT_PENDING:
                return unit
            if unit.status == UNIT_RUNNING and (unit.lease_expires or 0) <= now:
                return unit
        return None

    def release_unit(self, search_id, unit_key):
        with self.Session() as db:
            unit = db.scalar(select(SearchUnit).where(
                SearchUnit.task_id == search_id, SearchUnit.unit_key == unit_key))
            if unit is None or unit.status != UNIT_RUNNING:
                db.rollback()
                return
            unit.status = UNIT_PENDING
            unit.lease_owner = None
            unit.lease_expires = None
            self._adjust_stat(db, search_id, "units_running", -1)
            self._adjust_stat(db, search_id, "units_pending", +1)
            db.commit()

    def complete_unit(self, search_id, unit_key, status, spawned, solution, stats, worker_id):
        with self.Session() as db:
            unit = db.scalar(select(SearchUnit).where(
                SearchUnit.task_id == search_id, SearchUnit.unit_key == unit_key))
            if unit is None:
                db.rollback()
                return self.load_search(search_id)
            # 幂等：租约过期后单元可能已被接管者终态化或仍在其名下运行。
            # 仅当单元仍处于 running 且认领者就是当前 worker 时才接受完成，
            # 否则视为旧租约的迟到结果丢弃，杜绝重复改状态/计数/发布。
            if unit.status in (UNIT_DONE, UNIT_CHECKPOINTED):
                db.rollback()
                return self.load_search(search_id)
            if unit.status == UNIT_RUNNING and unit.lease_owner is not None \
                    and unit.lease_owner != worker_id:
                db.rollback()
                return self.load_search(search_id)
            for key in ("nodes", "pruned", "leaves"):
                self._adjust_stat(db, search_id, key, stats.get(key, 0))
            self._adjust_stat(db, search_id, "units_running", -1)

            if status == "done":
                unit.status = UNIT_DONE
                self._adjust_stat(db, search_id, "units_done", +1)
                self._safe_event(db, search_id, f"done:{unit_key}",
                                 f"工作单元 {unit_key} 已搜遍（worker {worker_id}）")
            else:
                unit.status = UNIT_CHECKPOINTED
                self._adjust_stat(db, search_id, "units_done", +1)
                self._safe_event(db, search_id, f"checkpoint:{unit_key}",
                                 f"工作单元 {unit_key} 到时检查点，拆出 {len(spawned)} 个续跑单元")
            unit.lease_owner = None
            unit.lease_expires = None

            for payload in spawned:
                self._insert_unit_if_absent(db, search_id, payload)

            if solution is not None:
                self._merge_best(db, search_id, solution, worker_id)
            db.commit()
        return self.load_search(search_id)

    # ---- 事件与报告 ----------------------------------------------------------

    def append_event(self, search_id, event_key, message):
        with self.Session() as db:
            self._safe_event(db, search_id, event_key, message)
            db.commit()

    def mark_status(self, search_id, status):
        with self.Session() as db:
            task = db.get(SearchTask, search_id)
            task.status = status
            db.commit()

    def mark_run_started(self, search_id, worker_id):
        with self.Session() as db:
            self._adjust_stat(db, search_id, "runs", +1)
            runs = (db.get(SearchTask, search_id).stats or {}).get("runs", 0)
            self._safe_event(db, search_id, f"run:{runs}:{worker_id}",
                             f"worker {worker_id} 开始/续跑求解")
            db.commit()

    def save_report(self, search_id, report):
        with self.Session() as db:
            db.get(SearchTask, search_id).report = report
            db.commit()

    def list_events(self, search_id):
        with self.Session() as db:
            rows = db.scalars(select(SearchEvent).where(
                SearchEvent.task_id == search_id).order_by(SearchEvent.id)).all()
            return [{"event_key": r.event_key, "message": r.message,
                     "at": r.created_at.isoformat() if r.created_at else None} for r in rows]

    def add_units(self, search_id, units):
        with self.Session() as db:
            for payload in units:
                self._insert_unit_if_absent(db, search_id, payload)
            db.commit()

    def set_precheck(self, search_id, precheck):
        with self.Session() as db:
            db.get(SearchTask, search_id).precheck = precheck
            db.commit()

    # ---- 内部辅助 ------------------------------------------------------------

    def _insert_unit_if_absent(self, db, search_id, payload):
        exists = db.scalar(select(SearchUnit.id).where(
            SearchUnit.task_id == search_id,
            SearchUnit.unit_key == payload["unit_key"]))
        if exists:
            return  # 幂等：重试不重复发布
        db.add(SearchUnit(
            task_id=search_id,
            unit_key=payload["unit_key"],
            level_limit=payload["level_limit"],
            stack=payload["stack"],
            status=UNIT_PENDING,
            attempts=0,
        ))
        self._adjust_stat(db, search_id, "units_pending", +1)

    def _merge_best(self, db, search_id, solution, worker_id):
        import uuid
        task = db.get(SearchTask, search_id)
        current = task.best_solution
        candidate_tuple = (solution["table_count"], solution["duplication"])
        if current is None or candidate_tuple < (
                current["table_count"], current["duplication"]):
            task.best_solution = dict(solution)
            self._safe_event(
                db, search_id,
                f"best:{candidate_tuple[0]}:{candidate_tuple[1]}:{uuid.uuid4().hex[:8]}",
                f"发现更优方案：{candidate_tuple[0]} 张表，重复存储 "
                f"{candidate_tuple[1]} 个属性（worker {worker_id}）",
            )

    @staticmethod
    def _safe_event(db, search_id, event_key, message):
        try:
            db.add(SearchEvent(task_id=search_id, event_key=event_key, message=message))
            db.flush()
        except IntegrityError:
            db.rollback()  # 幂等事件：并发/重试下已存在则忽略

    @staticmethod
    def _adjust_stat(db, search_id, key, delta):
        task = db.get(SearchTask, search_id)
        stats = dict(task.stats or {})
        stats[key] = stats.get(key, 0) + delta
        task.stats = stats

    @staticmethod
    def _task_record(task):
        stats = dict(task.stats or {})
        stats.setdefault("nodes", 0)
        stats.setdefault("units_pending", 0)
        stats.setdefault("units_running", 0)
        stats.setdefault("units_done", 0)
        stats.setdefault("exhausted_level", 0)
        stats.setdefault("runs", 0)
        return {
            "id": task.id,
            "spec": task.spec,
            "status": task.status,
            "precheck": task.precheck,
            "best_solution": task.best_solution,
            "report": task.report,
            "error": task.error,
            "stats": stats,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }
