import { useEffect, useState } from 'react';
import { readJson, startVisiblePolling } from './readPolling';
import './usage-analysis.css';
import { energyCoverage } from './energyUsageDisplay';

type Period = { start: number; end: number; estimate_kwh: number | null; coverage_ratio: number | null; basis_count: number };
const currencyLabel = (unit: string | null) => unit === 'CNY' ? '元' : unit ?? '';
type Tariff = { effective_date: string; rate: number; currency: string };
type Analysis = {
  schema: number; current_date: string; generated_at: number; month: string; coverage_ratio: number | null;
  comparisons: { key: string; current: Period; previous: Period; reason: string | null; delta_kwh: number | null; change_percent: number | null }[];
  tariffs: { effective_date: string; rate: number; currency: string }[];
  cost_totals: Record<string, number>; unpriced_kwh: number;
  costs: { date: string; estimate_cost: number | null; currency: string | null; rate: number | null; coverage_ratio: number | null }[];
};
const numeric = (n: number | null | undefined, digits = 3) => typeof n === 'number' && Number.isFinite(n) ? n.toFixed(digits) : '—';
const coverage = energyCoverage;
const stamp = (n: number) => new Date(n * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });

export async function saveSetting(url: string, body: unknown, method = 'POST') {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(url, { method, signal: controller.signal, cache: 'no-store', mode: 'same-origin', redirect: 'error',
      headers: { 'Content-Type': 'application/json', 'X-UPS-Settings': '1' }, body: JSON.stringify(body) });
    if (!response.ok) {
      const data = await response.json().catch(() => null);
      throw new Error(response.status === 400 && typeof data?.detail === 'string' ? data.detail : '保存未确认，请重新读取后再试。');
    }
    return await response.json();
  } finally { clearTimeout(timer); }
}

export default function UsageAnalysis({ month, view }: { month: string; view: 'comparison' | 'cost' }) {
  const [data, setData] = useState<Analysis | null>(null);
  const [error, setError] = useState('');
  const [generation, refresh] = useState(0);
  const [rate, setRate] = useState('');
  const [currency, setCurrency] = useState('CNY');
  const [effective, setEffective] = useState('');
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [editing, setEditing] = useState<string | null>(null);
  const [expected, setExpected] = useState<Tariff[] | null>(null);
  const [pending, setPending] = useState<{ prices: Tariff[]; expected: Tariff[]; description: string } | null>(null);
  useEffect(() => {
    return startVisiblePolling(async signal => {
      try {
        const next = await readJson<Analysis>(`/api/energy-usage/analysis${month ? '?month=' + month : ''}`, signal);
        if (next.schema !== 1 || !Array.isArray(next.comparisons) || !Array.isArray(next.costs)) throw new Error();
        if (!signal.aborted) { setData(next); setEffective(old => old || next.current_date); setError(''); }
      } catch { if (!signal.aborted) setError('用电分析暂不可用，正在重试。'); }
    }, () => 30000);
  }, [month, generation]);
  function save(event: React.FormEvent) {
    event.preventDefault();
    if (saving || !data) return;
    const original = expected ?? data.tariffs;
    const prices = original.filter(item => item.effective_date !== editing);
    if (prices.some(item => item.effective_date === effective)) { setMessage('该日期已有电价，请点击对应记录的「修改」。'); return; }
    prices.push({ effective_date: effective, rate: Number(rate), currency });
    setPending({ prices, expected: original, description: `保存 ${effective} 起的电价 ${rate} ${currencyLabel(currency)} / kWh。${editing && editing !== effective ? `原 ${editing} 的记录将移至该日期。` : ''}受影响日期的历史费用将重新计算，其他电价继续按生效日期适用。` });
  }
  async function confirmChange() {
    if (saving || !pending) return;
    setSaving(true); setMessage('');
    try {
      const prices = await saveSetting('/api/energy-usage/tariffs', { expected: pending.expected, tariffs: pending.prices }, 'PUT') as Tariff[];
      setData(old => old ? { ...old, tariffs: prices } : old);
      setMessage('电价已保存，历史费用已按新设置重新计算。');
      setPending(null); setEditing(null); setExpected(null); setRate(''); refresh(v => v + 1);
    } catch (cause) { setMessage(cause instanceof Error ? cause.message : '电价暂未保存。'); }
    finally { setSaving(false); }
  }
  function edit(price: Tariff) {
    if (!data) return;
    setEditing(price.effective_date); setExpected(data.tariffs); setEffective(price.effective_date); setRate(String(price.rate)); setCurrency(price.currency); setMessage('');
  }
  const names: Record<string, string> = { day: '今日与昨日同期', week: '本周与上周同期', month: '所选月与前月同期' };
  return <section className="usage-analysis" aria-labelledby="usage-analysis-heading">
    <h4 id="usage-analysis-heading" className="visually-hidden">{view === 'comparison' ? '同期对比' : '用电费用'}</h4>
    {view === 'comparison' && <p className="muted">按北京时间比较相同长度的完整小时。覆盖率不足 99% 或校准依据不同，暂不计算增减比例。</p>}
    {error && <p role="status">{error} 以下如有记录为上次查询结果。</p>}
    {!data ? <p className="muted">正在读取用电分析…</p> : <>
      {view === 'comparison' && <div className="usage-comparisons">{data.comparisons.map(item => <article key={item.key}>
        <h4>{names[item.key]}</h4>{item.reason && <p className="comparison-unavailable">不可直接比较</p>}<strong className={item.reason ? "comparison-secondary" : ""}>{numeric(item.current.estimate_kwh)} <small>kWh</small></strong><p>对照 {numeric(item.previous.estimate_kwh)} kWh</p>
        <p>{item.reason ? item.reason === 'basis_changed' ? '两段记录的校准依据不同' : '有效记录不足以支持同期比较'
          : <>变化 {item.delta_kwh !== null && item.delta_kwh > 0 ? '+' : ''}{numeric(item.delta_kwh)} kWh · {item.change_percent === null ? '对照为零，无百分比' : `${item.change_percent > 0 ? '+' : ''}${numeric(item.change_percent, 1)}%`}</>}</p>
        <small>覆盖 {coverage(item.current.coverage_ratio)} / {coverage(item.previous.coverage_ratio)}</small>
        <details><summary>比较范围与依据</summary><p>{stamp(item.current.start)} 至 {stamp(item.current.end)}</p><p>对照 {stamp(item.previous.start)} 至 {stamp(item.previous.end)}</p><p>估算依据数量 {item.current.basis_count} / {item.previous.basis_count}；月份天数不同时只比较共同长度。</p></details>
      </article>)}</div>}
      {view === 'cost' && <><div className="usage-cost"><h4>{data.month} · 已记录用电费用估算</h4>
        {Object.keys(data.cost_totals).length ? Object.entries(data.cost_totals).map(([unit, amount]) => <strong key={unit}>{numeric(amount, 2)} {currencyLabel(unit)} </strong>) : <p>尚无可计价的用电记录。</p>}
        <p className="muted">记录覆盖 {coverage(data.coverage_ratio)}；未配置对应电价的记录 {numeric(data.unpriced_kwh)} kWh。费用只对应已记录电量，缺采时段不补算。</p>
        <details><summary>每日费用</summary><div className="usage-table"><table><thead><tr><th>日期</th><th>费用估算</th><th>电价 / kWh</th><th>覆盖</th></tr></thead><tbody>{data.costs.map(day => <tr key={day.date}><td>{day.date}</td><td>{numeric(day.estimate_cost, 2)} {currencyLabel(day.currency)}</td><td>{numeric(day.rate, 4)}</td><td>{coverage(day.coverage_ratio)}</td></tr>)}</tbody></table></div></details>
      </div>
      <details className="usage-tariffs"><summary>管理电价与历史费用</summary><p className="muted">可选择过去的生效日期，也可修改或删除已有电价。每条电价适用至下一条生效前一天，历史费用随设置重新计算；用电量记录保持不变。</p>
        <form onSubmit={save} onFocus={() => setExpected(old => old ?? data.tariffs)}><label>生效日期<input disabled={saving || !!pending} type="date" required value={effective} onInput={e => setEffective(e.currentTarget.value)} onChange={e => setEffective(e.target.value)}/></label><label>每 kWh 电价<input disabled={saving || !!pending} type="number" min="0" max="10000" step="0.000001" required value={rate} onChange={e => setRate(e.target.value)}/></label><label>币种<input disabled={saving || !!pending} aria-label="币种代码" pattern="[A-Z]{3}" maxLength={3} required value={currency} onChange={e => setCurrency(e.target.value.toUpperCase())}/></label><button disabled={saving || !!error || !!pending}>{editing ? '预览修改' : '预览保存'}</button><button type="button" disabled={saving} onClick={() => { setEditing(null); setExpected(null); setPending(null); setRate(''); setMessage(''); refresh(v => v + 1); }}>取消编辑 / 重新读取</button></form>
        {message && <p role="status">{message}</p>}
        {pending && <div className="tariff-confirm" role="region" aria-label="确认电价变更"><p>{pending.description}</p><p>只重新计算费用，不会删除采集和用电量记录。</p><button type="button" disabled={saving || !!error} onClick={confirmChange}>{saving ? '正在保存…' : '确认变更并重算'}</button><button type="button" disabled={saving} onClick={() => setPending(null)}>取消</button></div>}
        <ul className="tariff-list">{data.tariffs.map(price => <li key={price.effective_date}><span>{price.effective_date} 起 · {price.rate} {currencyLabel(price.currency)} / kWh</span><button type="button" disabled={saving || !!pending} onClick={() => edit(price)}>修改</button><button type="button" disabled={saving || !!pending || !!error} onClick={() => setPending({ expected: data.tariffs, prices: data.tariffs.filter(item => item.effective_date !== price.effective_date), description: `删除 ${price.effective_date} 起的电价。对应日期将沿用更早的电价；没有更早电价时显示为未计价。` })}>删除</button></li>)}</ul>
        {data.tariffs.length > 0 && <button type="button" disabled={saving || !!pending || !!error} onClick={() => setPending({ expected: data.tariffs, prices: [], description: '清空全部电价后，所有历史费用将显示为未计价。重新设置起始日期与电价后，即可重新计算此前记录的费用。' })}>清空全部电价</button>}
      </details></>}
    </>}
  </section>;
}
