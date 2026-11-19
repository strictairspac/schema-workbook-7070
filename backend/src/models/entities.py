from sqlalchemy import Column,Integer,String,JSON,DateTime,ForeignKey,Text,Float,UniqueConstraint,Index
from datetime import datetime,timezone
from src.config.database import Base
class User(Base):
    __tablename__='users'
    id=Column(Integer,primary_key=True);email=Column(String(120),unique=True,nullable=False);password=Column(String(300),nullable=False);role=Column(String(20),nullable=False)
class Relation(Base):
    __tablename__='relations'
    id=Column(Integer,primary_key=True);code=Column(String(80),unique=True,nullable=False);title=Column(String(160),nullable=False);attributes=Column(JSON,nullable=False);note=Column(String(300),default='')
class Dependency(Base):
    __tablename__='dependencies'
    id=Column(Integer,primary_key=True);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False);lhs=Column(JSON,nullable=False);rhs=Column(JSON,nullable=False);note=Column(String(300),default='')
class Report(Base):
    __tablename__='reports'
    id=Column(Integer,primary_key=True);name=Column(String(120),nullable=False);relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False)
    status=Column(String(20),nullable=False,default='queued');input_snapshot=Column(JSON,nullable=False);result=Column(JSON,nullable=True);error=Column(Text,nullable=True)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
class Audit(Base):
    __tablename__='audit_logs'
    id=Column(Integer,primary_key=True);actor=Column(String(120),nullable=False);action=Column(String(160),nullable=False);created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))


class SearchTask(Base):
    """受限 3NF 分表求解任务：冻结草案快照 + 表数/每表属性上限 + 时限。"""
    __tablename__='search_tasks'
    id=Column(Integer,primary_key=True)
    name=Column(String(120),nullable=False)
    relation_id=Column(Integer,ForeignKey('relations.id'),nullable=False)
    # queued/running/waiting/optimal/infeasible/failed
    status=Column(String(20),nullable=False,default='queued',index=True)
    spec=Column(JSON,nullable=False)              # 冻结的属性/依赖/上限/时限
    precheck=Column(JSON,nullable=True)
    best_solution=Column(JSON,nullable=True)      # 已验证的最好方案（随时限推进更新）
    report=Column(JSON,nullable=True)             # 终态证据报告（含校验和）
    error=Column(Text,nullable=True)
    # 搜索进度统计（节点数、单元计数、已穷尽层级等），续跑时据此恢复
    stats=Column(JSON,nullable=False,default=dict)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),onupdate=lambda:datetime.now(timezone.utc))


class SearchUnit(Base):
    """可序列化的 DFS 工作单元；(task_id, unit_key) 幂等，租约支持崩溃重领。"""
    __tablename__='search_units'
    __table_args__=(
        UniqueConstraint('task_id','unit_key',name='uq_search_unit_key'),
        Index('ix_search_unit_claim','task_id','status','lease_expires'),
    )
    id=Column(Integer,primary_key=True)
    task_id=Column(Integer,ForeignKey('search_tasks.id'),nullable=False)
    unit_key=Column(String(120),nullable=False)
    level_limit=Column(Integer,nullable=False)
    stack=Column(JSON,nullable=False)             # DFS 前沿节点栈（检查点）
    # pending/running/done/checkpointed
    status=Column(String(20),nullable=False,default='pending')
    lease_owner=Column(String(80),nullable=True)
    lease_expires=Column(Float,nullable=True)
    attempts=Column(Integer,nullable=False,default=0)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
    updated_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc),onupdate=lambda:datetime.now(timezone.utc))


class SearchEvent(Base):
    """求解事件流；event_key 幂等，保证并发/重试不重复发布。"""
    __tablename__='search_events'
    __table_args__=(
        UniqueConstraint('task_id','event_key',name='uq_search_event_key'),
        Index('ix_search_event_task','task_id','id'),
    )
    id=Column(Integer,primary_key=True)
    task_id=Column(Integer,ForeignKey('search_tasks.id'),nullable=False)
    event_key=Column(String(160),nullable=False)
    message=Column(String(400),nullable=False)
    created_at=Column(DateTime(timezone=True),default=lambda:datetime.now(timezone.utc))
