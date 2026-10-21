import type { SystemStatus } from './types';
import { EvidenceFields, LoadError, useResource, WorkflowHeading } from './WorkflowUI';

const steps = [
  ['research', '01', '写下研究假设', '使用本地模板生成规格，人工修改、保存，确认后才运行。'],
  ['workbench', '02', '验证历史结果', '查看真实回测、运行日志与逐笔成交；保留零成交和失败记录。'],
  ['report', '03', '带走完整证据', '核对原始文件后生成确定性报告，下载 HTML / JSON 并打印。'],
  ['releases', '04', '审计后再模拟', '审核通过的 SMA 可进入当前行情 dry-run；创建后初始暂停。'],
];

export default function OverviewPanel({ navigate, newExperiment }: { navigate: (page: string) => void; newExperiment: () => void }) {
  const system = useResource<SystemStatus>('/system', 15000);
  const data = system.data;
  return <>
    <WorkflowHeading code="QUANTPILOT / MVP1" title="从假设，到可复查的证据" text="本地量化研究空间 · 历史回测、证据报告与教学模拟。" mode="MVP1 · 本地优先"/>
    <LoadError message={system.error} retry={system.reload}/>
    <section className="overview-start">
      <div className="overview-intro"><div className="eyebrow">START WITH A QUESTION</div><h2>先验证，<br/>再相信。</h2><p>从一个受限、可复现的实验开始，把每个结论留给原始证据。</p><div className="workflow-actions"><button className="primary" onClick={() => navigate('research')}>从本地模板开始 ↗</button><button className="secondary" onClick={newExperiment}>直接新建实验</button></div><small>本地模板无需在线模型；只有明确选择在线模型并点击生成才会调用。</small></div>
      <div className="overview-demo"><div className="eyebrow">EXPLORE SAVED EVIDENCE</div><h3>已有结果，直接复查</h3><p>查看已保存实验的报告与历史回放，无需启动新回测或模拟引擎。</p><div className="workflow-actions"><button className="secondary" onClick={() => navigate('report')}>查看证据报告 →</button><button className="secondary" onClick={() => navigate('replay')}>历史回放</button></div><div className="overview-counts">{[['experiments', '实验记录'], ['research', '研究规格'], ['releases', '发布审计']].map(([key, label]) => <div key={key}><strong>{data?.saved_counts[key as keyof SystemStatus['saved_counts']] ?? '—'}</strong><span>{label}</span></div>)}</div><small>来自本地数据库的保存记录；不代表通过审核或正在运行。</small></div>
    </section>
    <section className="overview-workflow" aria-label="研究工作流">{steps.map(([page, index, title, text]) => <button className="workflow-route" key={page} onClick={() => navigate(page)}><span>{index} <i>↗</i></span><h3>{title}</h3><p>{text}</p></button>)}</section>
    <div className="overview-bottom"><section className="document-card"><div className="section-label"><div className="eyebrow">LOCAL READINESS</div><button className="secondary" onClick={system.reload} disabled={system.loading}>重新检查</button></div><h2>{system.error ? '环境状态读取失败' : data ? data.core_ready ? '本地研究环境已就绪' : '还有准备工作需要完成' : '正在检查本地环境…'}</h2><p>只读检查本地文件与执行器心跳，不连接交易所，不启动引擎。</p>{data?.checklist.map(check => <div className="check" key={check.id}><span className={check.status === 'ok' ? 'positive' : check.status === 'error' ? 'negative' : 'readiness-warning'}>{check.status === 'ok' ? '✓ 就绪' : check.status === 'warning' ? '△ 提示' : '! 待处理'}</span><div><b>{check.detail}</b>{check.action && <p>{check.action}</p>}</div></div>)}{data && <EvidenceFields values={{ '版本': data.version, 'Python / Freqtrade': `${data.python_version} / ${data.engine_version ?? '未安装'}`, '在线模型（可选）': data.llm.configured ? '已配置；未调用' : '未配置；可使用本地模板', '检查时间 · UTC': data.generated_at }}/>}</section>
    <section className="document-card"><div className="eyebrow">TWO DISTINCT MODES</div><h2>历史证据与当前模拟</h2><p><b>历史回放</b>只播放已完成实验的原始成交，不产生新交易。</p><p><b>当前行情模拟</b>使用公开行情和模拟资金，必须经过 SMA 发布审核并显式启用。暂停禁止新开仓；停止引擎不会强制平仓。</p><div className="notice">ML / FreqAI 仅限历史研究。已知原始前视诊断 flagged 尚未解决，禁止用于模拟部署。最终保留测试仍未验证。</div><h3>保存的部署状态 · 未实时核验</h3><p>以下仅是数据库记录，不表示引擎当前存活、连接正常或持仓已平仓。</p>{data?.deployments.records.length ? data.deployments.records.map(record => <div className="persisted-state" key={record.id}><code>{record.id}</code><span>保存状态：{record.state} · 未核验</span></div>) : <p>{data ? '暂无保存的部署记录。' : '等待读取部署记录…'}</p>}<div className="workflow-actions"><button className="secondary" onClick={() => navigate('simulation')}>进入模拟交易</button><button className="secondary" onClick={() => navigate('help')}>阅读使用指南</button></div></section></div>
  </>;
}
