import { lazy, Suspense, useEffect, useState } from 'react';
import type { History } from './types';
import { readJson, startVisiblePolling } from './readPolling';
import { saveSetting } from './UsageAnalysis';

const Chart = lazy(() => import('./HistoryChart'));
type Event = { id: string; kind: string; detail: string; source: string; occurred_at: number | null; observed_at: number; note: string };
const kinds: Record<string, string> = { power: '供电切换', connection: '连接变化', calibration: '校准变更', application: '应用启动', nut: '系统 UPS 状态' };
const modes: Record<string, string> = { online: '外部供电', charging: '充电', battery: '电池供电', unknown: '状态未知' };
const sources: Record<string, string> = { panel: '面板', collector: '采集器', collector_observation: '采集观察', nut: 'NAS 系统 UPS 服务' };
const stamp = (n: number) => new Date(n * 1000).toLocaleString('zh-CN', { hour12: false });

function NearbyTrend({ event }: { event: Event }) {
  const [history, setHistory] = useState<History | null>(null);
  const [error, setError] = useState('');
  const [metric, setMetric] = useState('soc');
  useEffect(() => {
    const end = (event.occurred_at ?? event.observed_at) + 3600;
    return startVisiblePolling(async signal => {
      try { const next = await readJson<History>(`/api/history?hours=2&end=${end}`, signal); if (!signal.aborted) { setHistory(next); setError(''); } }
      catch { if (!signal.aborted) setError('附近趋势暂不可用，正在重试。'); }
    }, () => 60000);
  }, [event.id, event.occurred_at, event.observed_at]);
  return <div><label>附近趋势 <select aria-label="事件附近趋势指标" value={metric} onChange={e => setMetric(e.target.value)}><option value="soc">电量</option><option value="ac_input_estimate_w">交流输入估算</option><option value="cell_delta_mv">电芯压差</option></select></label>
    <p className="muted">以{event.occurred_at === null ? '首次观察' : '发生'}时间为中心，查看前后一小时；较早记录按保留精度展示。时间相近不代表因果关系。</p>
    {error ? <p role="status">{error}</p> : !history ? <p>正在读取趋势…</p> : history.points.length ? <Suspense fallback={<p>正在加载图表…</p>}><Chart history={history} metric={metric} unit={metric === 'soc' ? '%' : metric === 'cell_delta_mv' ? 'mV' : 'W'}/></Suspense> : <p>附近没有保留的趋势记录。</p>}
  </div>;
}

function Note({ event, onSaved }: { event: Event; onSaved: () => void }) {
  const [note, setNote] = useState(event.note);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  async function save(e: React.FormEvent) {
    e.preventDefault(); if (saving) return; setSaving(true); setMessage('');
    try { await saveSetting(`/api/timeline/${encodeURIComponent(event.id)}/note`, { note }, 'PUT'); setMessage('备注已保存。'); onSaved(); }
    catch (cause) { setMessage(cause instanceof Error ? cause.message : '备注暂未保存。'); }
    finally { setSaving(false); }
  }
  return <details><summary>编辑备注</summary><form onSubmit={save}><label>事件备注<textarea maxLength={300} value={note} onChange={e => setNote(e.target.value)}/></label><button disabled={saving}>{saving ? '正在保存…' : '保存备注'}</button>{message && <p role="status">{message}</p>}</form></details>;
}

export default function EventTimeline() {
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState('');
  const [kind, setKind] = useState('all');
  const [limit, setLimit] = useState(5);
  const [selected, setSelected] = useState<string | null>(null);
  const [generation, refresh] = useState(0);
  useEffect(() => startVisiblePolling(async signal => {
    try {
      const next = await readJson<Event[]>(`/api/timeline?limit=${limit}`, signal);
      if (!Array.isArray(next)) throw new Error();
      if (!signal.aborted) { setEvents(next); setError(''); }
    } catch { if (!signal.aborted) setError('事件查询暂不可用，已有内容为上次查询结果。'); }
  }, () => 10000), [limit, generation]);
  const visible = events.filter(event => kind === 'all' || event.kind === kind);
  return <section className="panel event-timeline" aria-labelledby="timeline-heading">
    <div className="panel-heading"><h3 id="timeline-heading">事件时间轴</h3><label>筛选 <select aria-label="事件类型" value={kind} onChange={e => setKind(e.target.value)}><option value="all">全部事件</option>{Object.entries(kinds).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label></div>
    <p className="muted">串联供电、连接、系统状态与校准变化。新类型从本版本启用后开始记录，旧记录保留原有首次观察时间。</p>
    {error && <p role="status">{error}</p>}
    <ol className="timeline-list">{visible.map(event => <li key={event.id}>
      <header><strong>{kinds[event.kind] || '状态事件'}</strong><time>{stamp(event.occurred_at ?? event.observed_at)}</time></header>
      <p>{event.detail === 'offline' ? '采集数据离线' : event.detail.startsWith('online:') ? `已连接 · ${modes[event.detail.split(':')[1]] || '状态更新'}` : event.detail}</p>
      <p className="muted">来源：{sources[event.source] || '观察记录'} · 首次观察 {stamp(event.observed_at)}{event.occurred_at === null ? '；发生时间未知' : `；发生时间 ${stamp(event.occurred_at)}`}</p>
      {event.note && <p>备注：{event.note}</p>}
      <div className="timeline-actions"><button aria-expanded={selected === event.id} onClick={() => setSelected(selected === event.id ? null : event.id)}>{selected === event.id ? '收起趋势' : '查看附近趋势'}</button><Note event={event} onSaved={() => refresh(v => v + 1)}/></div>
      {selected === event.id && <NearbyTrend key={event.id} event={event}/>}
    </li>)}</ol>
    {!visible.length && <p className="muted">当前范围内暂无此类事件。</p>}
    {limit < 100 && <button onClick={() => setLimit(limit < 20 ? 20 : 100)}>查看更多（最多 100 条）</button>}
  </section>;
}
