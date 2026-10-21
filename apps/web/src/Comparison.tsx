import { useEffect, useState } from 'react';
import { number, percent, request } from './api';
import type { ComparisonResult, Job } from './types';
const roles: Record<string, string> = { development: '开发', validation: '验证', mixed_or_legacy_development: '混合或旧版开发' };
export default function Comparison({ jobs }: { jobs: Job[] }) {
  const completed = jobs.filter(j => j.status === 'succeeded' && j.result);
  const [left, setLeft] = useState('');
  const [right, setRight] = useState('');
  const [data, setData] = useState<ComparisonResult | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setData(null); setError(''); setLoading(false);
    if (!left || !right || left === right) return;
    const controller = new AbortController(); setLoading(true);
    request<ComparisonResult>(`/comparison?ids=${encodeURIComponent(left)},${encodeURIComponent(right)}`, { signal: controller.signal })
      .then(value => { if (!controller.signal.aborted) setData(value); })
      .catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : '比较加载失败'); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [left, right]);
  return <><section className="page-heading"><div><div className="eyebrow">ALIGNED EXPERIMENTS / 02</div><h1>实验对照<span className="heading-dot">.</span></h1><p>先核对研究条件，再阅读历史差异。</p></div></section><section className="document-card"><h2>选择两个已完成实验</h2><div className="form-grid comparison-select">{(['A', 'B'] as const).map((label, i) => <label key={label}>实验 {label}<select value={i ? right : left} onChange={e => (i ? setRight : setLeft)(e.target.value)}><option value="">请选择已完成实验</option>{completed.map(j => <option key={j.id} value={j.id}>{j.name} · {j.config.strategy} · {j.config.start} → {j.config.end} · {j.id.slice(0, 8)}</option>)}</select></label>)}</div><p>对齐区间、标的、资金、投入、成本、数据指纹和运行假设。最终测试区间保留未运行。</p>{completed.length < 2 && <div className="notice">至少需要两个已成功完成的实验。</div>}{left && left === right && <div className="error-box" role="alert">请选择两个不同实验。</div>}{loading && <p role="status">正在核对已保存的实验证据…</p>}{error && <div className="error-box" role="alert">{error}</div>}</section>
    {data && (!data.comparable ? <section className="document-card"><h2>研究条件不一致 · 拒绝直接比较</h2><div className="error-box" role="alert"><ul>{data.reasons.map(reason => <li key={reason}>{reason}</li>)}</ul></div><p>请重新选择条件一致的实验。以下不展示排名或胜负结论。</p></section> : <><section className="document-card"><div className="eyebrow">MATCHED CONDITIONS</div><h2>同条件历史记录</h2><div className="table-scroll"><table><caption>已实现余额口径；周转率为买卖名义金额之和 / 初始资金</caption><thead><tr><th>指标</th>{data.rows.map(r => <th key={r.id}>{r.name}<small>{r.strategy} · {r.id.slice(0, 8)}</small></th>)}</tr></thead><tbody>{[
      ['研究区间', (r: typeof data.rows[number]) => `${r.config.start} → ${r.config.end}`],
      ['区间角色', (r: typeof data.rows[number]) => roles[r.role] || r.role],
      ['净收益率', (r: typeof data.rows[number]) => percent(r.metrics.net_return)],
      ['已平仓回撤', (r: typeof data.rows[number]) => `${number(r.metrics.max_drawdown * 100)}%`],
      ['成交笔数', (r: typeof data.rows[number]) => r.metrics.trades],
      ['名义周转率', (r: typeof data.rows[number]) => `${number(r.metrics.turnover)}×`],
      ['资金时间敞口', (r: typeof data.rows[number]) => r.stake_time_exposure === null ? '—' : `${number(r.stake_time_exposure * 100)}%`],
    ].map(([label, render]) => <tr key={String(label)}><th>{String(label)}</th>{data.rows.map(r => <td key={r.id}>{typeof render === 'function' ? render(r) : render}</td>)}</tr>)}</tbody></table></div><p>资金时间敞口 = Σ（单笔投入 × 持仓秒数）/（初始资金 × 区间秒数），不按浮动盈亏估值。</p></section>
    <div className="comparison-grid">{data.rows.map(r => <section className="document-card" key={r.id}><div className="eyebrow">{r.strategy}</div><h2>{r.name}</h2><h3>按平仓月份归集的盈亏</h3><p>固定成交记录的事后归集；不是逐月盯市收益率。</p><div className="table-scroll"><table><thead><tr><th>平仓月份</th><th>笔数</th><th>盈亏 · USDT</th></tr></thead><tbody>{r.monthly.map(m => <tr key={m.month}><td>{m.month}</td><td>{m.trades}</td><td>{number(m.profit_abs)}</td></tr>)}</tbody></table></div>{!r.monthly.length && <p>没有平仓交易。</p>}<h3>额外成本压力</h3><p>在固定买卖金额上追加单边费用的事后估算；没有重跑引擎，不模拟仓位、撮合或交易路径变化。</p><div className="table-scroll"><table><thead><tr><th>额外单边费用</th><th>估算净收益率</th></tr></thead><tbody>{r.cost_sensitivity.map(c => <tr key={c.extra_fee_per_side}><td>{number(c.extra_fee_per_side * 100)}%</td><td>{percent(c.estimated_net_return)}</td></tr>)}</tbody></table></div><details className="comparison-definitions"><summary>查看计算口径与数据证据</summary><p>{r.monthly_definition}</p><p>{r.cost_definition}</p><p>{r.exposure_definition}</p><pre>{JSON.stringify(r.provenance, null, 2)}</pre></details></section>)}</div><div className="notice">{data.conclusion}</div></>)}</>;
}
