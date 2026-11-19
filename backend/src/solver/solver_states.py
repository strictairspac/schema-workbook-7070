"""求解器共享状态常量（纯常量，避免循环导入）。"""

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_WAITING = "waiting"      # 本次时限耗尽，待续跑
STATE_OPTIMAL = "optimal"      # 已证明最优
STATE_INFEASIBLE = "infeasible"  # 已证明无解
STATE_FAILED = "failed"

SEARCH_STATES = (
    STATE_QUEUED, STATE_RUNNING, STATE_WAITING,
    STATE_OPTIMAL, STATE_INFEASIBLE, STATE_FAILED,
)

UNIT_PENDING = "pending"
UNIT_RUNNING = "running"
UNIT_DONE = "done"
UNIT_CHECKPOINTED = "checkpointed"

# 前端徽章与详情页消费的中文状态文案。
STATE_LABELS = {
    STATE_QUEUED: "排队中",
    STATE_RUNNING: "执行中",
    STATE_WAITING: "待续跑",
    STATE_OPTIMAL: "已最优",
    STATE_INFEASIBLE: "无解",
    STATE_FAILED: "失败",
}
