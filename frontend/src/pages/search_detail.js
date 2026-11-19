import {esc,date} from '../utils/html.js';
import {session} from '../hooks/session.js';
import {SEARCH_STATES} from '../constants/enums.js';

const badge=status=>{
  const cls=status==='optimal'?'completed':(status==='infeasible'||status==='failed')?'failed':status;
  return `<span class="badge ${esc(cls)}">${esc(SEARCH_STATES[status]||status)}</span>`;
};

function verdict(task){
  const s=task.status;
  if(s==='optimal'){
    const r=task.report;
    return `<div class="verdict">
      <div class="verdict-icon">✓</div>
      <div><span class="eyebrow">PROVED OPTIMAL</span><h2>已证明最优</h2>
        <p>${esc(r.optimality?.reason||'必要层级已全部搜遍，该方案表数最少且重复存储最小。')}</p></div>
      <div class="equation">${r.table_count}<span> 表</span> · ${r.duplication}<span> 重复属性</span></div>
    </div>`;
  }
  if(s==='infeasible'){
    const notes=(task.precheck?.notes||[]).map(n=>`<li>${esc(n)}</li>`).join('');
    return `<div class="verdict">
      <div class="verdict-icon" style="color:var(--red)">✕</div>
      <div><span class="eyebrow">PROVED INFEASIBLE</span><h2>已证明无解</h2>
        <p>1..${task.spec.max_tables} 张表的全部合规组合均已排除。</p>
        ${notes?`<ul class="help">${notes}</ul>`:''}</div>
    </div>`;
  }
  if(s==='waiting'){
    const best=task.best_solution;
    return `<div class="verdict">
      <div class="verdict-icon" style="color:var(--amber)">↻</div>
      <div><span class="eyebrow">CHECKPOINT SAVED</span><h2>待续跑</h2>
        <p>本轮时限到，DFS 检查点已保存，续跑会沿用原草案与上限、跳过已排除组合。</p>
        ${best?`<p>当前已验证最好方案：${best.table_count} 张表，重复存储 ${best.duplication} 个属性（尚未证明最优）。</p>`
          :'<p>暂未找到可行方案。</p>'}</div>
    </div>`;
  }
  if(s==='failed'){
    return `<div class="verdict"><div class="verdict-icon" style="color:var(--red)">!</div>
      <div><span class="eyebrow">FAILED</span><h2>求解失败</h2><p>${esc(task.error||'输入不合法或执行异常')}</p></div></div>`;
  }
  return `<div class="verdict"><div class="verdict-icon" style="color:var(--amber)">…</div>
    <div><span class="eyebrow">RUNNING</span><h2>${badge(s)}</h2><p>正在穷举合规组合，页面将自动刷新。</p></div></div>`;
}

function decomposition(task){
  const r=task.report;
  if(!r?.decomposition) return '';
  return `<section class="form-panel" style="margin-top:24px">
    <span class="eyebrow">LOSSLESS DECOMPOSITION</span>
    <h2>无损分表（${r.decomposition.length} 张）</h2>
    ${r.decomposition.map((attrs,i)=>`
      <div class="decomposed"><b>R${i+1}</b><div>${attrs.map(a=>`<span class="attribute">${esc(a)}</span>`).join('')}</div></div>`).join('')}
    <div class="evidence-notes" style="margin-top:18px">
      <div><h3>校验结论</h3>
        <p>全部 3NF：<strong>${r.all_3nf?'是':'否'}</strong><br>
           依赖保持：<strong>${r.dependency_preserved?'是':'否'}</strong><br>
           无损连接：<strong>${r.chase?.lossless?'是':'否'}</strong>（见证行 ${(r.chase?.witness_rows||[]).join(', ')}）<br>
           重复存储：<strong>${r.duplication}</strong> 个属性 · 总存储 ${r.total_stored_attributes}</p>
      </div>
      <div><h3>候选键与最小覆盖</h3>
        <p>候选键：${(r.candidate_keys||[]).map(k=>`<span class="attribute">{${esc(k.join(','))}}</span>`).join(' ')}<br>
           最小覆盖：${(r.minimal_cover||[]).map(f=>`<span class="attribute">${esc(f.lhs.join(','))}→${esc(f.rhs.join(','))}</span>`).join(' ')}</p>
      </div>
    </div>
  </section>`;
}

export function searchDetailPage(task){
  const canWrite=session.user.role!=='viewer';
  const isOpen=['queued','running','waiting'].includes(task.status);
  const st=task.stats||{};
  return `${verdict(task)}
    <div class="evidence-top">
      <div><h2>${esc(task.name)}</h2>
        <p>表≤${task.spec?.max_tables} · 每表≤${task.spec?.max_attrs_per_table} · 时限 ${task.spec?.time_limit_seconds}s · 创建于 ${date(task.created_at)}</p>
      </div>
      <div>
        ${isOpen?`<button class="small primary" id="resume-btn" ${canWrite?'':'disabled'}>续跑求解 ↻</button>`:''}
        ${task.report?`<button class="small" id="search-export">导出报告 JSON</button>`:''}
      </div>
    </div>
    <section class="form-panel">
      <span class="eyebrow">PROGRESS</span>
      <h2>搜索进度</h2>
      <div class="table-wrap"><table>
        <thead><tr><th>已穷尽层级</th><th>访问节点</th><th>叶子</th><th>剪枝</th><th>待办单元</th><th>执行中</th><th>完成单元</th><th>续跑轮次</th></tr></thead>
        <tbody><tr>
          <td class="quantity">${st.exhausted_level??0}</td><td>${st.nodes??0}</td><td>${st.leaves??0}</td>
          <td>${st.pruned??0}</td><td>${st.units_pending??0}</td><td>${st.units_running??0}</td>
          <td>${st.units_done??0}</td><td>${st.runs??0}</td>
        </tr></tbody>
      </table></div>
    </section>
    ${decomposition(task)}
    <section class="form-panel" style="margin-top:24px">
      <span class="eyebrow">EVENT LOG</span>
      <h2>事件流（幂等，重试不重复发布）</h2>
      <div class="table-wrap"><table><thead><tr><th>时间</th><th>事件</th></tr></thead>
      <tbody>${(task.events||[]).slice().reverse().map(e=>
        `<tr><td>${date(e.at)}</td><td>${esc(e.message)}</td></tr>`).join('')||'<tr><td colspan="2">暂无事件</td></tr>'}</tbody></table></div>
    </section>`;
}
