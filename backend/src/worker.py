import traceback, json
from pathlib import Path
from src.config.database import Session
from src.models.entities import Report, SearchTask
from src.services.normalization import analyze
from src.services import searches
from src.solver.solver_states import STATE_FAILED


def _run_report(ident):
    with Session() as db:
        obj = db.get(Report, ident)
        obj.status = 'running'
        db.commit()
        snapshot = obj.input_snapshot
    result = analyze(snapshot['relation']['attributes'], snapshot['dependencies'])
    with Session() as db:
        obj = db.get(Report, ident)
        obj.result = result
        obj.status = 'completed'
        db.commit()
    Path('/artifacts/report-{}.json'.format(ident)).write_text(
        json.dumps(result, ensure_ascii=False, indent=2))


def _run_search(ident, worker_id):
    try:
        searches.execute_task(ident, worker_id)
    except Exception:
        traceback.print_exc()
        with Session() as db:
            task = db.get(SearchTask, ident)
            if task:
                task.status = STATE_FAILED
                task.error = '求解执行异常，请查看 worker 日志后续跑'
                db.commit()


def _pop_report_id():
    """经典分解队列：统一经 Redis/内存弹性抽象弹出。"""
    try:
        return searches.dequeue_classic(timeout=1)
    except Exception:
        traceback.print_exc()
        return None


def main(worker_id='worker-1'):
    while True:
        Path('/tmp/worker-heartbeat').touch()
        # 优先受限搜索队列（含续跑消息），内部已封装 Redis/内存降级。
        try:
            search_id = searches.dequeue(timeout=1)
        except Exception:
            traceback.print_exc()
            search_id = None
        if search_id is not None:
            _run_search(search_id, worker_id)
            continue
        ident = _pop_report_id()
        if ident is None:
            continue
        try:
            _run_report(ident)
        except Exception:
            traceback.print_exc()
            with Session() as db:
                obj = db.get(Report, ident)
                if obj:
                    obj.status = 'failed'
                    obj.error = str('分解执行异常，请重新创建任务')
                    db.commit()


if __name__ == '__main__':
    main()
