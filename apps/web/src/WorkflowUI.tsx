import { useCallback, useEffect, useRef, useState } from 'react';
import { request } from './api';
export const jsonOptions = (body?: unknown, key?: string): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json', ...(key ? { 'Idempotency-Key': key } : {}) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
export const show = (value: unknown): string => value === null || value === undefined ? '未记录' : typeof value === 'object' ? JSON.stringify(value) : String(value);
export function WorkflowHeading({ code, title, text, mode }: { code: string; title: string; text: string; mode: string }) { return <section className="page-heading"><div><div className="eyebrow">{code}</div><h1>{title}<span className="heading-dot">.</span></h1><p>{text}</p></div><span className="outlined-badge">{mode}</span></section>; }
export function RawDetails({ title = '原始证据', value }: { title?: string; value: unknown }) { return <details className="raw-details"><summary>{title}</summary><pre>{JSON.stringify(value, null, 2)}</pre></details>; }
export function EvidenceFields({ values }: { values: Record<string, unknown> }) { return <dl className="detail-list">{Object.entries(values).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{show(value)}</dd></div>)}</dl>; }
export function useResource<T>(path: string, interval = 0) {
  const [data, setData] = useState<T | null>(null), [error, setError] = useState(''), [loading, setLoading] = useState(true), [version, setVersion] = useState(0);
  const previousPath = useRef(path);
  const reload = useCallback(() => setVersion(v => v + 1), []);
  useEffect(() => { const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>; let stopped = false; if (previousPath.current !== path) { setData(null); previousPath.current = path; } setLoading(true); setError('');
    async function load() { try { const value = await request<T>(path, { signal: controller.signal }); if (!stopped) { setData(value); setError(''); } } catch (err) { if (!stopped) setError(err instanceof Error ? err.message : '连接失败'); } finally { if (!stopped) { setLoading(false); if (interval) timer = setTimeout(load, interval); } } }
    void load(); return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [path, interval, version]);
  return { data, error, loading, reload };
}
export function useAction() {
  const lock = useRef(false), mounted = useRef(true); const [busy, setBusy] = useState(false), [error, setError] = useState('');
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  async function run(action: () => Promise<void>) { if (lock.current) return; lock.current = true; setBusy(true); setError(''); try { await action(); } catch (err) { if (mounted.current) setError(err instanceof Error ? err.message : '连接失败，请重试。'); } finally { lock.current = false; if (mounted.current) setBusy(false); } }
  return { busy, error, setError, run, mounted };
}
export function LoadError({ message, retry }: { message: string; retry?: () => void }) { return message ? <div className="error-box" role="alert">{message}{retry && <button className="secondary" onClick={retry}>重新读取</button>}</div> : null; }
