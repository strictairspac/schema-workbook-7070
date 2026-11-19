"""受限 3NF 分表求解器包。

- theory:       关系理论基础（闭包/候选键/最小覆盖/3NF/无损/依赖保持）
- search:       候选表枚举与分支限界 DFS、可序列化检查点
- orchestrator: 分层穷举编排与终态判定（纯函数 + 存储抽象）
- runner:       建案、续跑、终态证据的端到端流程
- memory_store: 零依赖内存存储（测试/本地演示）
- solver_states: 状态常量
"""
from .solver_states import (
    STATE_QUEUED, STATE_RUNNING, STATE_WAITING,
    STATE_OPTIMAL, STATE_INFEASIBLE, STATE_FAILED,
    STATE_LABELS, SEARCH_STATES,
)

__all__ = [
    "STATE_QUEUED", "STATE_RUNNING", "STATE_WAITING",
    "STATE_OPTIMAL", "STATE_INFEASIBLE", "STATE_FAILED",
    "STATE_LABELS", "SEARCH_STATES",
]
