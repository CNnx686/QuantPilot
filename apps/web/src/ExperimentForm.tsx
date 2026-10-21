import { useEffect, useRef, useState } from 'react';
import { request } from './api';
import type { Dataset, Job } from './types';
export default function ExperimentForm({ onClose, onCreated, dataset }: { onClose: () => void; onCreated: (job: Job) => void; dataset: Dataset | null }) {
  const [name, setName] = useState('双币种趋势基线');
  const [pairs, setPairs] = useState(['BTC/USDT', 'ETH/USDT']);
  const [start, setStart] = useState('2025-01-01');
  const [end, setEnd] = useState('2025-04-01');
  const [wallet, setWallet] = useState('10000');
  const [stake, setStake] = useState('1000');
  const [fee, setFee] = useState('0.1');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const submission = useRef<{ body: string; key: string } | null>(null);
  const lock = useRef(false);
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement;
    dialog.current?.querySelector<HTMLInputElement>('input')?.focus();
    const original = document.body.style.overflow; document.body.style.overflow = 'hidden';
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !lock.current) onClose();
      if (e.key === 'Tab') {
        const elements = dialog.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), a[href]');
        if (!elements?.length) return;
        const first = elements[0], last = elements[elements.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener('keydown', handler);
    return () => { document.body.style.overflow = original; document.removeEventListener('keydown', handler); previous?.focus(); };
  }, [onClose]);
  async function submit(e: React.FormEvent) {
    e.preventDefault(); if (lock.current) return;
    if (!pairs.length) { setError('请至少选择一个交易对。'); return; }
    if (!name.trim()) { setError('请输入实验名称。'); return; }
    if (end <= start) { setError('结束日期必须晚于开始日期。'); return; }
    if (Number(stake) > Number(wallet)) { setError('单笔投入不能超过初始资金。'); return; }
    const body = JSON.stringify({ name: name.trim(), pairs, start, end, wallet: Number(wallet), stake: Number(stake), fee: Number(fee) / 100, strategy: 'SmaTrend' });
    if (submission.current?.body !== body) submission.current = { body, key: crypto.randomUUID() };
    lock.current = true; setBusy(true); setError('');
    try { const job = await request<Job>('/experiments', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': submission.current.key }, body }); onCreated(job); }
    catch (err) { setError(`${err instanceof Error ? err.message : '连接中断'}。可重试；相同参数将复用提交标识，避免重复实验。`); }
    finally { lock.current = false; setBusy(false); }
  }
  return <div className="overlay" onMouseDown={e => { if (e.target === e.currentTarget && !busy) onClose(); }}><div className="drawer" ref={dialog} role="dialog" aria-modal="true" aria-labelledby="form-title">
    <div className="drawer-heading"><div><div className="eyebrow">NEW EXPERIMENT</div><h2 id="form-title">让一个假设接受检验。</h2></div><button className="icon-button" onClick={onClose} disabled={busy} aria-label="关闭新建实验">×</button></div>
    <form onSubmit={submit}><fieldset disabled={busy}><label>实验名称<input value={name} onChange={e => setName(e.target.value)} required maxLength={100}/></label>
      <div className="field-label">研究标的</div><div className="pair-options">{['BTC/USDT', 'ETH/USDT'].map(pair => <label key={pair} className="pair-choice"><input type="checkbox" checked={pairs.includes(pair)} onChange={e => setPairs(e.target.checked ? [...pairs, pair].sort() : pairs.filter(p => p !== pair))}/><span>{pair}</span></label>)}</div>
      <div className="form-grid"><label>开始日期<input type="date" required min={dataset?.available_start} max={dataset?.available_end} value={start} onChange={e => setStart(e.target.value)}/></label><label>结束日期（不含当天）<input type="date" required min={start} max={dataset?.available_end} value={end} onChange={e => setEnd(e.target.value)}/></label></div>
      <p className="field-help">日期采用 UTC；结束日期为开区间。{dataset?.available_start && ` 可用范围 ${dataset.available_start} 至 ${dataset.available_end}。`}</p>
      <div className="form-grid"><label>初始资金 · USDT<input type="number" min="1" max="1000000000" step="any" required value={wallet} onChange={e => setWallet(e.target.value)}/></label><label>单笔投入 · USDT<input type="number" min="1" max={wallet} step="any" required value={stake} onChange={e => setStake(e.target.value)}/></label></div>
      <label>单边手续费 · %<input type="number" min="0" max="10" step="0.01" required value={fee} onChange={e => setFee(e.target.value)}/></label>
      <div className="strategy-box"><div className="eyebrow">FIXED BASELINE / SMA TREND</div><h3>双均线趋势策略</h3><p>快速均线 <b>SMA 12</b> / 慢速均线 <b>SMA 48</b><br/>固定止损 <b>5%</b> · 现货历史回测</p><span>本阶段参数固定，保留可复现的研究基线。</span></div>
    </fieldset>{error && <div className="error-box" role="alert">{error}</div>}<div className="drawer-footer"><p>结果用于历史研究，不代表未来表现。</p><button className="primary" type="submit" disabled={busy}>{busy ? '正在提交…' : '创建并运行实验'}<span>↗</span></button></div></form>
  </div></div>;
}
