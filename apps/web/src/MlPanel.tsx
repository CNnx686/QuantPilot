import { number, percent } from './api';
import type { Artifact, MlEvidence } from './types';
export default function MlPanel({ evidence, artifacts }: { evidence?: MlEvidence; artifacts: Artifact[] }) {
  if (!evidence) return <p className="panel-empty">该实验没有 ML 训练证据。SMA 基线不训练模型；ML 实验完成后展示实际审计结果。</p>;
  const p = evidence.protocol;
  return <div className="provenance"><div className="eyebrow">MODEL EVIDENCE / ACTUAL RUN</div><h3>{evidence.model_class}</h3><p className="field-help">LightGBM {evidence.lightgbm_version} · 预测未来 {p.horizon_candles} 小时收益率（不是概率）</p>
    <dl className="detail-list">{Object.entries({ '实际拟合次数': evidence.fit_count, '预测总数': evidence.prediction_count, '有效预测': evidence.valid_prediction_count, '无效预测（不作为信号）': evidence.invalid_prediction_count, '实际训练耗时（秒）': number(evidence.total_fit_seconds, 3), '时间审计': evidence.timestamp_audit, '最终测试状态': evidence.final_test }).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <h4>冻结的研究协议</h4><p className="evidence-copy">训练窗口 {p.train_period_days} 天；每 {p.backtest_period_days} 天重训；入场阈值 {percent(p.entry_threshold)}；退出阈值 {percent(p.exit_threshold)}；随机种子 {p.seed}。不进行超参数搜索。</p>
    <h4>时间语义 · UTC</h4><p className="evidence-copy">fit_cutoff / model_fit_at 表示理想化历史拟合截止时间，并非实际训练完成时刻。实际耗时单独记录，不模拟训练延迟。特征在决策时可用；训练标签最晚可用时间 ≤ 拟合截止时间 &lt; 决策时间。截止后才可获知的标签行已剔除。</p>
    <div className="table-scroll"><table><caption>每次实际训练的模型与标签审计 · UTC</caption><thead><tr><th>交易对 / 模型 ID</th><th>训练起点</th><th>拟合截止</th><th>标签最晚可用</th><th>剔除行</th><th>训练行</th><th>实际秒数</th></tr></thead><tbody>{evidence.audits.map(a => <tr key={`${a.pair}-${a.model_id}`}><td>{a.pair}<small>{a.model_id}</small></td><td>{a.training_start}</td><td>{a.fit_cutoff}</td><td>{a.model_label_max_at}</td><td>{a.purged_rows}</td><td>{a.training_rows}</td><td>{number(a.duration_seconds, 3)}</td></tr>)}</tbody></table></div>
    <h4>下载训练与逐小时预测证据</h4><div className="artifact-list">{artifacts.filter(a => Object.values(evidence.artifacts).includes(a.name)).map(a => <a href={a.url} download key={a.name}>{a.name}<span>↓</span></a>)}</div>
  </div>;
}
