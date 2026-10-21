import { useEffect, useState } from 'react';
import { readable, request } from './api';
import type { DiagnosticReport } from './types';
const statuses = { passed_sampled: '抽查通过', flagged: '检查发现标记，需解释', unverified: '尚未验证' };
export default function Diagnostics({ jobId }: { jobId: string }) {
  const [reports, setReports] = useState<DiagnosticReport[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController(); setReports(null); setError('');
    async function refresh() {
      try { const value = await request<DiagnosticReport[]>(`/experiments/${jobId}/diagnostics`, { signal: controller.signal }); if (!controller.signal.aborted) { setReports(value); setError(''); } }
      catch (err) { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : '专项检查报告暂不可用'); }
    }
    void refresh(); const timer = setInterval(() => { void refresh(); }, 10000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [jobId]);
  return <><h4>后续专项检查 · 匹配本实验结果指纹</h4><p className="evidence-copy">以下报告在回测之后生成。上方「结果核验」是运行时快照；抽查通过仅覆盖报告范围，不证明全部信号不存在偏差。</p>{error && <div className="notice" role="status">{error}</div>}{reports === null ? !error && <p className="muted">正在读取专项检查…</p> : reports.length === 0 ? <p className="muted">尚未执行针对该实验的专项检查。</p> : reports.map(report => <section className="diagnostic-report" key={report.id}><h4>{statuses[report.status] || report.status} · {report.strategy}</h4><p className="evidence-copy">检查范围：{readable(report.scope)}</p>{!report.rows?.length && <p className="evidence-copy">该诊断未记录逐行信号表；请下载原始诊断文件核对检查范围。</p>}<div className="table-scroll"><table><thead><tr><th>偏差标记</th><th>信号数</th><th>入场标记</th><th>退出标记</th><th>指标标记</th></tr></thead><tbody>{(report.rows ?? []).map((row, i) => <tr key={i}><td>{readable(row.has_bias)}</td><td>{row.total_signals}</td><td>{row.biased_entry_signals}</td><td>{row.biased_exit_signals}</td><td>{readable(row.biased_indicators)}</td></tr>)}</tbody></table></div><div className="artifact-list">{report.files.map(file => <a href={file.url} download key={file.name}>{file.name}<span>↓</span></a>)}</div></section>)}</>;
}
