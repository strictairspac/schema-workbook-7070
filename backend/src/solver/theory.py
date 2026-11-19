"""受限 3NF 分解求解器。

纯标准库实现，属性以整数位集表示（单关系最多 10 个属性时 int 足够），
函数依赖统一规范化为 ``(lhs:int, rhs:int)`` 且 rhs 为单属性位。
模块不接触数据库/网络，可被 worker、CLI 与单元测试直接复用。
"""
from __future__ import annotations

MAX_ATTRIBUTES = 10
MAX_DEPENDENCIES = 64


def bits_of(attributes):
    """属性名列表 -> (位集, 属性名列表)，属性名列表按位序排列。"""
    if not attributes:
        raise ValueError("关系必须至少包含 1 个属性")
    names = sorted(set(attributes))
    if len(names) > MAX_ATTRIBUTES:
        raise ValueError(f"精确求解最多支持 {MAX_ATTRIBUTES} 个属性，收到 {len(names)} 个")
    if len(names) != len(attributes):
        raise ValueError("属性名存在重复，请去重后重试")
    bit = {name: 1 << i for i, name in enumerate(names)}
    full = (1 << len(names)) - 1
    return names, bit, full


def normalize_fds(dependencies, bit, names):
    """把 {lhs:[...], rhs:[...]} 形式的依赖拆成单属性右端，返回 (lhs, rhs_bit) 列表。"""
    if len(dependencies) > MAX_DEPENDENCIES:
        raise ValueError(f"函数依赖最多 {MAX_DEPENDENCIES} 条，收到 {len(dependencies)} 条")
    fds, seen = [], set()
    valid = set(bit)
    for dep in dependencies:
        lhs_names, rhs_names = dep["lhs"], dep["rhs"]
        if not lhs_names or not rhs_names:
            raise ValueError("函数依赖的左右端都不能为空")
        if not set(lhs_names).issubset(valid) or not set(rhs_names).issubset(valid):
            raise ValueError("函数依赖引用了关系外属性")
        lhs = 0
        for name in lhs_names:
            lhs |= bit[name]
        for name in rhs_names:
            rhs = bit[name]
            if rhs & lhs:
                continue  # 平凡依赖 X -> X 恒成立，丢弃
            pair = (lhs, rhs)
            if pair not in seen:
                seen.add(pair)
                fds.append(pair)
    fds.sort(key=lambda p: (p[0].bit_count(), p[0], p[1]))
    return fds


def closure(attrs, fds):
    """属性位集 attrs 在 fds 下的闭包。"""
    out = attrs
    while True:
        before = out
        for lhs, rhs in fds:
            if (lhs & ~out) == 0:
                out |= rhs
        if out == before:
            return out


def fd_implies(fds, lhs, rhs):
    return (rhs & ~closure(lhs, fds)) == 0


def equivalent_fds(a, b):
    """两个依赖集是否等价（互相覆盖）。"""
    for lhs, rhs in a:
        if not fd_implies(b, lhs, rhs):
            return False
    for lhs, rhs in b:
        if not fd_implies(a, lhs, rhs):
            return False
    return True


def candidate_keys(full, fds):
    """枚举全部候选键（位集列表，按大小排序）。

    直接按尺寸枚举全集的全部子集（10 属性上限内至多 1024 个），
    闭包覆盖全集且不含更短候选键（真子集）者即为候选键。不能只枚举
    "非 RHS 属性 + 可选属性"：出现在某条依赖右端的属性仍可能属于候选键
    （如 A<->B 中 B 在 RHS，却是候选键 BC 的成员）。
    """
    from itertools import combinations
    attr_bits = [1 << i for i in range(full.bit_length()) if full & (1 << i)]
    keys = []
    for size in range(1, len(attr_bits) + 1):
        for part in combinations(attr_bits, size):
            cand = 0
            for one in part:
                cand |= one
            # 真子集判断必须用位包含 (k & cand)==k 且 k!=cand；
            # 整数 k<=cand 对互不包含的位集会误判（如 5 与 6）。
            if any((k & cand) == k and k != cand for k in keys):
                continue  # 已含更小候选键，必为超键而非候选键
            if closure(cand, fds) == full:
                keys.append(cand)
    if not keys:  # 理论上不会发生（全集必为超键），兜底
        keys = [full]
    keys.sort(key=lambda k: (k.bit_count(), k))
    return keys


def minimal_cover(fds):
    """求一个最小覆盖：右端单属性、左端无多余属性、无冗余依赖。

    对属性/依赖做确定性排序，保证相同输入得到相同输出（可复现检查点）。
    """
    cover = list(dict.fromkeys(fds))  # 去重保序
    # 1) 消除左端多余属性：A 多余当且仅当在“当前完整覆盖 F”下 (X-A)+ 已含右端。
    #    判定集合必须保留原依赖 X->Y，不能用化简后的依赖，否则循环论证恒真。
    changed = True
    while changed:
        changed = False
        for idx, (lhs, rhs) in enumerate(cover):
            for bit_index in sorted(range(lhs.bit_length()), reverse=True):
                attr = 1 << bit_index
                if not (lhs & attr):
                    continue
                reduced = lhs & ~attr
                if fd_implies(cover, reduced, rhs):
                    cover[idx] = (reduced, rhs)
                    changed = True
                    break
            if changed:
                break
    # 2) 消除冗余依赖（确定性顺序）
    result = []
    for idx, (lhs, rhs) in enumerate(cover):
        others = result + cover[idx + 1:]
        if not fd_implies(others, lhs, rhs):
            result.append((lhs, rhs))
    result.sort(key=lambda p: (p[0].bit_count(), p[0], p[1]))
    return result


def projected_fds(relation, fds):
    """F+ 在关系属性集 relation 上的精确投影（左端枚举 relation 的全部子集）。"""
    bits = [1 << i for i in range(relation.bit_length()) if relation & (1 << i)]
    out = []
    seen = set()
    from itertools import combinations
    for size in range(len(bits) + 1):
        for part in combinations(bits, size):
            lhs = 0
            for one in part:
                lhs |= one
            reach = closure(lhs, fds) & relation
            for bit_index in range(reach.bit_length()):
                rhs = 1 << bit_index
                if (reach & rhs) and not (lhs & rhs) and (lhs, rhs) not in seen:
                    seen.add((lhs, rhs))
                    out.append((lhs, rhs))
    return out


def is_3nf_relation(relation, fds):
    """判断关系模式 relation（携带 F 在其上的投影依赖）是否满足 3NF。

    3NF：对每个非平凡投影依赖 X->A，要么 X 是 relation 的超键，
    要么 A 是主属性（属于某个候选键）。返回 (是否3NF, 局部候选键, 违规列表)。
    """
    projected = projected_fds(relation, fds)
    keys = candidate_keys(relation, projected)
    prime = 0
    for key in keys:
        prime |= key
    violations = []
    for lhs, rhs in projected:
        is_superkey = (closure(lhs, projected) & relation) == relation
        if not is_superkey and not (prime & rhs):
            violations.append((lhs, rhs))
    return (not violations), keys, violations


def lossless_chase(full, relations, fds, names):
    """并查集版 tableau 追赶法判定分解是否无损连接。

    relations 为位集列表。出现全区分符号（a 符号）行即无损。
    返回结构化证据，供报告展示。
    """
    attrs = [1 << i for i in range(full.bit_length()) if full & (1 << i)]
    parent = {}

    def node(kind, row, attr):
        value = f"{kind}:{row}:{names[attr.bit_length()-1]}" if kind == "b" else f"a:{names[attr.bit_length()-1]}"
        parent.setdefault(value, value)
        return value

    tableau = []
    for row_index, relation in enumerate(relations):
        row = []
        for attr in attrs:
            if relation & attr:
                row.append(node("a", row_index, attr))
            else:
                row.append(node("b", row_index, attr))
        tableau.append(row)

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != x:
            parent[x], x = root, parent[x]
        return root

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx == ry:
            return False
        if rx.startswith("a:"):
            parent[ry] = rx
        elif ry.startswith("a:"):
            parent[rx] = ry
        else:
            parent[max(rx, ry)] = min(rx, ry)
        return True

    steps = []
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            lhs_cols = [i for i, attr in enumerate(attrs) if lhs & attr]
            rhs_col = next(i for i, attr in enumerate(attrs) if attr == rhs)
            for i in range(len(tableau)):
                for j in range(i + 1, len(tableau)):
                    if all(find(tableau[i][c]) == find(tableau[j][c]) for c in lhs_cols):
                        if union(tableau[i][rhs_col], tableau[j][rhs_col]):
                            changed = True
                            steps.append({
                                "rows": [i + 1, j + 1],
                                "dependency": {"lhs": _names_of(lhs, names), "rhs": _names_of(rhs, names)},
                            })
    final = [[find(v) for v in row] for row in tableau]
    witnesses = [i + 1 for i, row in enumerate(final) if all(v.startswith("a:") for v in row)]
    return {
        "lossless": bool(witnesses),
        "witness_rows": witnesses,
        "steps": steps,
        "tableau": final,
    }


def dependency_preserved(relations, fds):
    """依赖保持：G = 各关系投影依赖的并集，原始每条 FD 都能由 G 推出。"""
    projected = []
    for relation in relations:
        projected.extend(projected_fds(relation, fds))
    return all(fd_implies(projected, lhs, rhs) for lhs, rhs in fds), projected


def _names_of(bitset, names):
    return [names[i] for i in range(len(names)) if bitset & (1 << i)]


def render_relations(relations, names):
    return [_names_of(relation, names) for relation in relations]
