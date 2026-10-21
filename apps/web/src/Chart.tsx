import { useId, useState } from 'react';
import { number } from './api';
export default function Chart({ points }: { points: { time: string; value: number }[] }) {
  const id = useId().replace(/:/g, '');
  const [hover, setHover] = useState<number | null>(null);
  if (!points.length) return <div className="chart-empty">该实验没有可展示的余额数据。</div>;
  const values = points.map(p => p.value);
  const min = Math.min(...values), max = Math.max(...values), pad = Math.max((max - min) * .16, max * .002, 1);
  const bottom = min - pad, top = max + pad;
  const times = points.map(p => new Date(p.time).getTime());
  const span = times.at(-1)! - times[0];
  const x = (i: number) => 68 + (span > 0 ? (times[i] - times[0]) / span : i / Math.max(1, points.length - 1)) * 830;
  const y = (value: number) => 22 + (top - value) / (top - bottom) * 220;
  const line = points.map((p, i) => i ? `H${x(i).toFixed(2)} V${y(p.value).toFixed(2)}` : `M${x(i).toFixed(2)},${y(p.value).toFixed(2)}`).join(' ');
  const focus = hover === null ? null : points[hover];
  return <div className="chart-wrap"><svg viewBox="0 0 930 290" role="img" aria-label="按历史日期绘制的已实现余额曲线，单位 USDT" onMouseLeave={() => setHover(null)}>
    <defs><linearGradient id={id} x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#18856d" stopOpacity=".18"/><stop offset="100%" stopColor="#18856d" stopOpacity="0"/></linearGradient></defs>
    {[0, 1, 2, 3, 4].map(i => { const value = top - i / 4 * (top - bottom); return <g key={i}><line x1="68" x2="898" y1={y(value)} y2={y(value)} stroke="#e5e8e2" strokeDasharray="3 5"/><text x="56" y={y(value) + 4} textAnchor="end">{number(value, 0)}</text></g>; })}
    <path d={`${line} L${x(points.length - 1)},242 L68,242 Z`} fill={`url(#${id})`}/><path d={line} fill="none" stroke="#178069" strokeWidth="2.5" strokeLinejoin="round"/>
    {[0, Math.floor((points.length - 1) / 2), points.length - 1].filter((v, i, arr) => arr.indexOf(v) === i).map(i => <text key={i} x={x(i)} y="275" textAnchor={i === 0 ? 'start' : 'end'}>{points[i].time.slice(0, 10)}</text>)}
    {points.map((p, i) => <circle key={i} cx={x(i)} cy={y(p.value)} r="8" fill="transparent" onMouseEnter={() => setHover(i)}><title>{p.time}: {number(p.value)} USDT</title></circle>)}
    {hover !== null && focus && <g><line x1={x(hover)} x2={x(hover)} y1="22" y2="242" stroke="#178069" strokeDasharray="4 4"/><circle cx={x(hover)} cy={y(focus.value)} r="4" fill="#178069"/></g>}
  </svg><div className="chart-caption"><span><i className="legend-dot"/>{focus ? `${focus.time.slice(0, 16)} · ${number(focus.value)} USDT` : '已实现余额 · USDT'}</span><span>仅在成交平仓后更新</span></div></div>;
}
