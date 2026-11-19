from sqlalchemy import select
from redis import Redis
from fastapi import HTTPException
from src.models.entities import Relation,Dependency,Report
from src.config.settings import REDIS_URL
from src.constants.enums import RUN_STATES
from src.utils.serialization import record
from .common import save
# 显式连接/读写超时：Redis 不可用时快速失败并降级到内存队列，而不是无限挂起。
queue=Redis.from_url(REDIS_URL,decode_responses=True,socket_connect_timeout=2,socket_timeout=2,retry_on_timeout=False)
def listing(db):return [record(x) for x in db.scalars(select(Report).order_by(Report.id.desc()))]
def detail(db,ident):
    obj=db.get(Report,ident)
    if not obj:raise HTTPException(404,'报告不存在')
    return record(obj)
def create(db,data,user):
    relation=db.get(Relation,data.relation_id)
    if not relation:raise HTTPException(404,'关系不存在')
    deps=list(db.scalars(select(Dependency).where(Dependency.relation_id==relation.id)))
    snapshot={'relation':record(relation),'dependencies':[{'lhs':x.lhs,'rhs':x.rhs} for x in deps]}
    obj=Report(**data.model_dump(),input_snapshot=snapshot,status=RUN_STATES[0]);result=save(db,obj,user,'范式分解入队 '+data.name)
    try:
        from src.services.searches import enqueue_classic
        enqueue_classic(obj.id)
    except Exception:
        obj.status='failed';obj.error='队列暂时不可用，请重新创建';db.commit();raise HTTPException(503,obj.error)
    return result
