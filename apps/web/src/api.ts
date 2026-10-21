export class ApiError extends Error { constructor(message: string, public status: number) { super(message); } }
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const timeout = AbortSignal.timeout(30000);
  const signal = options?.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
  const response = await fetch(`/api${path}`, { ...options, signal });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    const detail = data?.detail;
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((item: { msg: string }) => item.msg).join('；') : `请求失败（${response.status}）`;
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}
export const number = (value: number, digits = 2) => value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
export const percent = (value: number) => `${value > 0 ? '+' : ''}${number(value * 100)}%`;
export const dateTime = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—';
export const readable = (value: unknown): string => typeof value === 'string' ? value : JSON.stringify(value);
