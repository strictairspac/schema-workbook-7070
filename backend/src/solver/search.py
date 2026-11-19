"""受限 3NF 分解的分支限界搜索。

目标（字典序）：
  1) 表数最少（1 .. max_tables）；
  2) 表数相同时，重复存储的属性最少，即 sum(|Ri|) - |U| 最小。

每个候选表都预先验证为 3NF；叶子再统一验证依赖保持与无损连接。
搜索按表数 k=1..K 逐层穷举，配合可接受下界剪枝；只有把某一层
（以及更小的所有层）搜遍才能宣告该层无解，全部层搜遍才宣告整体无解。

工作单元（unit）携带 DFS 前沿栈，可序列化为 JSON：
  - 正常跑完 -> done；
  - 到时 -> 把未展开的前沿拆成新 unit 落库（pending），本 unit 标 checkpointed，
    续跑沿用原草案与上限，已排除/已完成的组合不会重复执行。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from itertools import combinations

from . import theory

# 根单元在该前缀深度处切分初始并发单元；属性上限 10，候选子集有界。
SPLIT_DEPTH_CAP = 3
MAX_INITIAL_UNITS = 32  # 首批单元数量上界，避免小深度组合爆炸；不足再由检查点细分


@dataclass
class Problem:
    attributes: list
    dependencies: list
    max_tables: int
    max_attrs_per_table: int
    time_limit_seconds: float

    # 派生量
    names: list = field(default_factory=list)
    bit: dict = field(default_factory=dict)
    full: int = 0
    fds: list = field(default_factory=list)
    cover: list = field(default_factory=list)
    keys: list = field(default_factory=list)
    candidates: list = field(default_factory=list)  # list[Candidate]
    precheck: dict = field(default_factory=dict)

    @classmethod
    def build(cls, attributes, dependencies, max_tables, max_attrs_per_table, time_limit_seconds):
        if max_tables < 1:
            raise ValueError("表数上限至少为 1")
        if max_attrs_per_table < 1:
            raise ValueError("每表属性数上限至少为 1")
        if time_limit_seconds <= 0:
            raise ValueError("求解时限必须为正数秒")
        names, bit, full = theory.bits_of(attributes)
        fds = theory.normalize_fds(dependencies, bit, names)
        cover = theory.minimal_cover(fds)
        keys = theory.candidate_keys(full, fds)
        problem = cls(
            attributes=names,
            dependencies=dependencies,
            max_tables=int(max_tables),
            max_attrs_per_table=int(max_attrs_per_table),
            time_limit_seconds=float(time_limit_seconds),
            names=names, bit=bit, full=full, fds=fds, cover=cover, keys=keys,
        )
        problem.precheck = problem._precheck()
        problem.candidates = problem._build_candidates()
        return problem

    def _precheck(self):
        """结构性不可行判定（无需搜索即可证明无解的条件）。"""
        n_attr = len(self.names)
        notes = []
        feasible = True

        # 容量上界：K 张表、每张至多 M 个属性，覆盖不了全集。
        if self.max_tables * self.max_attrs_per_table < n_attr:
            feasible = False
            notes.append(
                f"容量不足：{self.max_tables} 张表 × 每表 {self.max_attrs_per_table} 属性 "
                f"< 全集 {n_attr} 个属性"
            )

        # 注意：不能用"最小覆盖依赖的左右端必须同表"做不可行预检——依赖保持
        # 允许一条 FD 经多张表的投影依赖间接推出（如 X->Y、Y->Z 分置两表仍保持
        # X->Z），该条件充分非必要，过强会漏判可行解。精确判定留给搜索。

        # 无损分解必含一张携带候选键（超键）的表；最小候选键超容量则无解。
        smallest_key = min(self.keys, key=lambda k: k.bit_count())
        if smallest_key.bit_count() > self.max_attrs_per_table:
            feasible = False
            notes.append(
                f"最小候选键含 {smallest_key.bit_count()} 个属性，超过每表上限，"
                "无损分解必须有表携带候选键"
            )
        return {"feasible": feasible, "notes": notes}

    def _build_candidates(self):
        """枚举所有可行的候选表：满足 3NF、不超每表上限，且至少
        （a）承载一条最小覆盖依赖，或（b）含某个候选键。其余子集不可能
        出现在最优解中（去掉它不影响覆盖、依赖保持与无损性）。"""
        attr_bits = [1 << i for i in range(len(self.names))]
        useful = []
        for size in range(1, self.max_attrs_per_table + 1):
            if size > len(self.names):
                break
            for part in combinations(attr_bits, size):
                relation = 0
                for one in part:
                    relation |= one
                ok3nf, local_keys, _ = theory.is_3nf_relation(relation, self.fds)
                if not ok3nf:
                    continue
                embeds = [i for i, (lhs, rhs) in enumerate(self.cover)
                          if ((lhs | rhs) & relation) == (lhs | rhs)]
                has_key = any((key & relation) == key for key in self.keys)
                if not embeds and not has_key:
                    continue
                useful.append(Candidate(
                    relation=relation,
                    size=relation.bit_count(),
                    embeds=tuple(embeds),
                    has_key=has_key,
                    local_keys=local_keys,
                ))
        # 确定性顺序：小表优先、键表优先、位序兜底，保证检查点可复现。
        useful.sort(key=lambda c: (c.size, 0 if c.has_key else 1, c.relation))
        return useful


@dataclass(frozen=True)
class Candidate:
    relation: int
    size: int
    embeds: tuple
    has_key: bool
    local_keys: list


@dataclass
class Node:
    """DFS 前沿节点：path 为已选候选下标（严格递增）。"""
    path: tuple
    covered: int
    embedded: int  # 已承载的最小覆盖依赖位集
    has_key: bool
    cost: int      # sum 表大小

    def to_payload(self):
        return {"path": list(self.path), "covered": self.covered,
                "embedded": self.embedded, "has_key": self.has_key, "cost": self.cost}

    @classmethod
    def from_payload(cls, data):
        return cls(tuple(data["path"]), data["covered"], data["embedded"],
                   data["has_key"], data["cost"])


def initial_units(problem: Problem, level_limit: int):
    """在根 DFS 的固定深度处切分初始单元，全部绑定同一表数层 level_limit。

    返回单元 payload 列表（确定性顺序，unit_key 全局唯一）。
    """
    candidates = problem.candidates
    # 自适应深度：在 SPLIT_DEPTH_CAP 内选择不超过 MAX_INITIAL_UNITS 个前缀的最大深度，
    # 保证首批单元有界且仍保留并发度。
    depth = 1
    for candidate_depth in range(1, min(SPLIT_DEPTH_CAP, level_limit) + 1):
        estimated = math.comb(len(candidates), candidate_depth)
        if estimated <= MAX_INITIAL_UNITS:
            depth = candidate_depth
        else:
            break
    prefixes = [()]
    for _ in range(depth):
        nxt = []
        for prefix in prefixes:
            start = (prefix[-1] + 1) if prefix else 0
            for idx in range(start, len(candidates)):
                nxt.append(prefix + (idx,))
        prefixes = nxt
    units = []
    for seq, prefix in enumerate(prefixes):
        node = _node_of(problem, prefix)
        if node is None:
            continue
        units.append({
            "unit_key": f"L{level_limit}-root-{seq:04d}",
            "level_limit": level_limit,
            "stack": [node.to_payload()],
        })
    if not units:  # 候选极多、任何非空前缀都超上界时，保留整棵搜索树作为单个根单元
        units.append({
            "unit_key": f"L{level_limit}-root-0000",
            "level_limit": level_limit,
            "stack": [Node((), 0, 0, False, 0).to_payload()],
        })
    return units


def _node_of(problem: Problem, path):
    covered, embedded, cost = 0, 0, 0
    has_key = False
    for idx in path:
        cand = problem.candidates[idx]
        covered |= cand.relation
        embedded |= _embed_mask(cand)
        has_key = has_key or cand.has_key
        cost += cand.size
    return Node(path, covered, embedded, has_key, cost)


def _embed_mask(cand):
    mask = 0
    for e in cand.embeds:
        mask |= 1 << e
    return mask


def _children(problem: Problem, node: Node, level_limit: int):
    """扩展下一层候选（下标严格递增），应用结构性剪枝。

    仅保留可证明安全的剪枝：容量上界与键表必要性。依赖承载数（embedded）
    不能用于剪枝，因为依赖保持允许间接推导；它仅随节点累计用于报告展示。
    """
    candidates = problem.candidates
    start = (node.path[-1] + 1) if node.path else 0
    out = []
    used = len(node.path)
    remaining_slots = level_limit - used - 1
    for idx in range(start, len(candidates)):
        cand = candidates[idx]
        new_path = node.path + (idx,)
        if len(new_path) > level_limit:
            break
        covered = node.covered | cand.relation
        embedded = node.embedded | _embed_mask(cand)
        has_key = node.has_key or cand.has_key
        cost = node.cost + cand.size

        # 剩余表位即使全选也覆盖不了缺失属性（按最大表容量估计）。
        missing = problem.full & ~covered
        if remaining_slots * problem.max_attrs_per_table < missing.bit_count():
            continue
        # 无损必须有键表：已选无键且之后再无表位时剪枝。
        if not has_key and remaining_slots == 0:
            continue
        out.append(Node(new_path, covered, embedded, has_key, cost))
    return out


def _is_leaf_solution(problem: Problem, node: Node, level_limit: int):
    """叶子校验：表数恰为当前层、覆盖全集、含键表（无损必要条件）、
    依赖保持、无损连接。候选表本身已保证 3NF。

    注意不要求每条最小覆盖依赖都被某张表直接承载：依赖保持允许一条 FD
    经多表投影依赖间接推出，直接承载只是充分非必要条件（否则漏判可行解）。
    embedded 位集仅随节点累计用于报告展示，不作为叶子门槛或剪枝条件。
    """
    if len(node.path) != level_limit:
        return None
    if node.covered != problem.full:
        return None
    if not node.has_key:
        return None
    relations = [problem.candidates[i].relation for i in node.path]
    preserved, _ = theory.dependency_preserved(relations, problem.fds)
    if not preserved:
        return None
    chase = theory.lossless_chase(problem.full, relations, problem.fds, problem.names)
    if not chase["lossless"]:
        return None
    duplication = node.cost - len(problem.names)
    return {
        "relations": relations,
        "table_count": len(relations),
        "duplication": duplication,
        "total_stored": node.cost,
        "chase": chase,
    }


def run_unit(problem: Problem, payload, deadline, best_so_far=None, stats=None):
    """执行一个工作单元直到完成或到时。

    best_so_far: 当前全局最优 (table_count, duplication) 或 None，仅用于可接受剪枝。
    返回 (status, result, spawned)：
      status: "done" | "timeout"
      result: 本单元发现的最优可行解（dict）或 None
      spawned: 超时时未处理前沿生成的新单元 payload 列表
    """
    stats = stats if stats is not None else {"nodes": 0, "pruned": 0, "leaves": 0}
    level_limit = payload["level_limit"]
    stack = [Node.from_payload(item) for item in payload["stack"]]
    best = None
    spawn_seq = 0

    def better(solution):
        return best is None or (solution["table_count"], solution["duplication"]) < (
            best["table_count"], best["duplication"])

    while stack:
        if time.monotonic() >= deadline:
            spawned = [{
                "unit_key": f"{payload['unit_key']}-c{spawn_seq + i:04d}",
                "level_limit": level_limit,
                "stack": [node.to_payload()],
            } for i, node in enumerate(stack)]
            return "timeout", best, spawned
        node = stack.pop()
        stats["nodes"] += 1

        # 可接受下界：当前路径最终重复存储量至少为 cost + 剩余表位(至少1属性) - |U|。
        # 仅当该下界已不可能优于全局最优时剪枝（同表数且重复量 >= 已知最优）。
        if best_so_far is not None and level_limit >= best_so_far[0]:
            lower_duplication = (
                node.cost + max(0, level_limit - len(node.path)) - len(problem.names)
            )
            if lower_duplication >= best_so_far[1]:
                stats["pruned"] += 1
                continue

        if len(node.path) == level_limit:
            stats["leaves"] += 1
            solution = _is_leaf_solution(problem, node, level_limit)
            if solution and better(solution):
                best = solution
            continue

        children = _children(problem, node, level_limit)
        # 逆序压栈，保持候选下标从小到大的确定性展开顺序。
        for child in reversed(children):
            stack.append(child)

    return "done", best, []


def solution_evidence(problem: Problem, solution):
    """为最终方案补齐 3NF/依赖保持/无损的完整证据，供报告与导出。"""
    relations = solution["relations"]
    checks = []
    for relation in relations:
        ok, local_keys, violations = theory.is_3nf_relation(relation, problem.fds)
        checks.append({
            "attributes": theory._names_of(relation, problem.names),
            "is_3nf": ok,
            "candidate_keys": [theory._names_of(k, problem.names) for k in local_keys],
            "violations": [
                {"lhs": theory._names_of(lhs, problem.names),
                 "rhs": theory._names_of(rhs, problem.names)}
                for lhs, rhs in violations
            ],
        })
    preserved, projected = theory.dependency_preserved(relations, problem.fds)
    chase = theory.lossless_chase(problem.full, relations, problem.fds, problem.names)
    return {
        "decomposition": theory.render_relations(relations, problem.names),
        "table_count": len(relations),
        "duplication": solution["duplication"],
        "total_stored_attributes": solution["total_stored"],
        "all_3nf": all(item["is_3nf"] for item in checks),
        "normal_form_checks": checks,
        "dependency_preserved": preserved,
        "projected_dependencies": [
            {"lhs": theory._names_of(lhs, problem.names),
             "rhs": theory._names_of(rhs, problem.names)}
            for lhs, rhs in projected
        ],
        "chase": chase,
    }
