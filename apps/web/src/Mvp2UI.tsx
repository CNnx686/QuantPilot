import { useEffect, useRef, useState } from 'react';
import { ApiError, dateTime } from './api';
import { EvidenceFields, LoadError, useResource } from './WorkflowUI';

export type NewsEvidence = { id: string; source: string; title: string; url: string; content_hash: string; published_at: string | null; first_seen_at: string; revision_seen_at: string; available_at: string; historical_feature_eligible?: false; usage?: string };
export function safePublisherUrl(value: string) { try { const u = new URL(value); return u.protocol === 'https:' && !u.username && !u.password && ['federalreserve.gov', 'www.federalreserve.gov', 'ecb.europa.eu', 'www.ecb.europa.eu', 'coindesk.com', 'www.coindesk.com'].includes(u.hostname) ? u.href : undefined; } catch { return undefined; } }
export function NewsEvidenceCard({ evidence }: { evidence?: NewsEvidence }) { if (!evidence) return null; return <aside className="news-evidence"><div className="eyebrow">IMMUTABLE NEWS VERSION / {evidence.source}</div><h3>{evidence.title}</h3><a href={safePublisherUrl(evidence.url)} target="_blank" rel="noopener noreferrer">查看出版方原文 ↗</a><p>此新闻仅作为当代研究假设的背景。它在 2025 历史回测时不可用，不能作为历史特征或交易因果证据。修改草稿不会修改这份来源快照。</p><EvidenceFields values={{ '新闻版本': evidence.id, '发布时间（出版方）': evidence.published_at, '首次发现': evidence.first_seen_at, '此修订发现': evidence.revision_seen_at, '最早可用时间': evidence.available_at, '内容 SHA256': evidence.content_hash }}/></aside>; }
export type DataJob = { id: string; status: string; kind: string; source: string; error: string | null; created_at: string; finished_at: string | null; result?: unknown };
export function JobWatch({ id, onDone }: { id: string; onDone: () => void }) {
  const [terminal, setTerminal] = useState(false), notified = useRef('');
  const job = useResource<DataJob>(`/data/jobs/${encodeURIComponent(id)}`, terminal ? 0 : 2000);
  useEffect(() => { if (job.data && ['succeeded', 'failed'].includes(job.data.status)) { setTerminal(true); if (notified.current !== id) { notified.current = id; onDone(); } } }, [job.data, id, onDone]);
  return <div className="job-receipt" role="status"><b>持久化任务 · {job.data?.status ?? '读取中'}</b><code>{id}</code><span>{job.data?.kind} / {job.data?.source} · {dateTime(job.data?.finished_at ?? null)}</span><LoadError message={job.error || job.data?.error || ''} retry={job.reload}/></div>;
}
export function readSession(key: string) { try { return sessionStorage.getItem(key) || ''; } catch { return ''; } }
export function saveSession(key: string, value: string) { try { if (value) sessionStorage.setItem(key, value); else sessionStorage.removeItem(key); } catch { /* Storage is optional. */ } }
// A failed or interrupted POST keeps its key; a changed payload is a new intent.
export function useIdempotency(namespace: string) {
  const pending = useRef<Record<string, string> | null>(null), active = useRef('');
  if (!pending.current) { try { const value = JSON.parse(readSession(namespace) || '{}'); pending.current = value && typeof value === 'object' && !Array.isArray(value) ? value : {}; } catch { pending.current = {}; } }
  function clear() { delete pending.current![active.current]; saveSession(namespace, JSON.stringify(pending.current)); }
  return { key(path: string, body: unknown) { const fingerprint = JSON.stringify([path, body]); active.current = fingerprint; if (!pending.current![fingerprint]) { pending.current![fingerprint] = crypto.randomUUID(); saveSession(namespace, JSON.stringify(pending.current)); } return pending.current![fingerprint]; }, clear, failed(error: unknown) { if (error instanceof ApiError && error.status < 500 && !/pending|interrupted/i.test(error.message)) clear(); } };
}
export async function textArtifact(path: string, signal: AbortSignal) { const response = await fetch(path, { signal: AbortSignal.any([signal, AbortSignal.timeout(30000)]) }); if (!response.ok) throw new Error(`文件读取失败（${response.status}）`); return response.text(); }
