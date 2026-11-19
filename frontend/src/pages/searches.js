import {table} from '../components/common/table.js';
import {esc,date} from '../utils/html.js';
import {session} from '../hooks/session.js';
import {SEARCH_STATES} from '../constants/enums.js';

const stateBadge=status=>{
  const cls=status==='optimal'?'completed':status==='infeasible'||status==='failed'?'failed':status;
  return `<span class="badge ${esc(cls)}">${esc(SEARCH_STATES[status]||status)}</span>`;
};

export function searchesPage(items,relations){
  const rows=items.map(s=>{
    const rel=relations.find(x=>x.id===s.relation_id);
    const best=s.best_solution
      ? `${s.best_solution.table_count} 表 / 重复 ${s.best_solution.duplication}`
      : '—';
    return [
      `<strong>${esc(s.name)}</strong><small>${date(s.created_at)}</small>`,
      esc(rel?rel.code:('#'+s.relation_id)),
      `表≤${s.spec?.max_tables ?? '—'} · 每表≤${s.spec?.max_attrs_per_table ?? '—'} · ${s.spec?.time_limit_seconds ?? '—'}s`,
      stateBadge(s.status),
      esc(best),
      `<a href="#search/${s.id}">查看 →</a>`,
    ];
  });
  return `<div class="split">
    <section>
      <div class="section-top"><h2>受限分表求解</h2><span>BOUNDED 3NF / EXHAUSTIVE</span></div>
      ${items.length?table(['任务 / 时间','草案','上限','状态','已验证最好','报告'],rows)
        :'<div class="empty">还没有受限分表任务。在右侧设定表数与每表属性上限后建案。</div>'}
      <div class="protocol">
        <h3>求解与续跑协议</h3>
        <ol>
          <li>预检容量、候选键等结构性不可行条件，再枚举全部合规候选表（均已验证 3NF）</li>
          <li>按表数 1..K 分层穷举，叶子做依赖保持与无损连接校验，先求表数最少、再求重复最少</li>
          <li>到时把 DFS 前沿落为检查点，状态置「待续跑」，保留已验证最好方案</li>
          <li>续跑沿用原草案与上限、跳过已排除组合；搜遍才报「已最优」或「无解」</li>
        </ol>
      </div>
    </section>
    <section class="form-panel">
      <span class="eyebrow">BOUNDED SYNTHESIS</span>
      <h2>新建受限求解</h2>
      <form id="search-form">
        <label>任务名称<input name="name" value="bounded-${new Date().toISOString().slice(0,10)}" required></label>
        <label>选择业务草案<select name="relation_id">${relations.map(r=>`<option value="${r.id}">${esc(r.code)} · ${esc(r.title)}</option>`).join('')}</select></label>
        <div class="fields">
          <label>表数上限 K<input name="max_tables" type="number" min="1" max="10" value="3" required></label>
          <label>每表属性上限 M<input name="max_attrs_per_table" type="number" min="1" max="10" value="3" required></label>
        </div>
        <label>单轮时限（秒）<input name="time_limit_seconds" type="number" min="0.1" max="3600" step="0.1" value="5" required></label>
        <button class="primary" ${session.user.role==='viewer'?'disabled':''}>开始受限求解 ↗</button>
      </form>
      <p class="help">入队即冻结关系与依赖快照。时限到会保留检查点与当前最好方案，可随时续跑；只有把必要层级全部搜遍才会判定「已最优」或「无解」。</p>
    </section>
  </div>`;
}
