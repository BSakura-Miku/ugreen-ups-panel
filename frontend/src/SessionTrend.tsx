import { lazy, Suspense, useEffect, useState } from 'react';
import type { BatterySession, History } from './types';
import { readJson, startVisiblePolling } from './readPolling';
const Chart = lazy(() => import('./HistoryChart'));
export default function SessionTrend({ record }: { record: BatterySession }) {
  const [history, setHistory] = useState<History | null>(null);
  const [error, setError] = useState(false);
  const [metric, setMetric] = useState('soc');
  useEffect(() => startVisiblePolling(async signal => {
    const end = (record.end_ts ?? record.last_ts) + 60;
    const hours = Math.max(1, Math.ceil((end - record.start_ts + 60) / 3600));
    try { const next = await readJson<History>(`/api/history?hours=${Math.min(hours, 8760)}&end=${end}`, signal); if (!signal.aborted) { setHistory(next); setError(false); } }
    catch { if (!signal.aborted) setError(true); }
  }, () => 30000), [record.id, record.end_ts, record.last_ts, record.start_ts]);
  return <section><label>本次供电附近趋势 <select value={metric} onChange={e => setMetric(e.target.value)}><option value="soc">电量</option><option value="battery_energy_estimate_w">电池放电功率 · 估算</option><option value="cell_delta_mv">电芯压差</option></select></label><p className="muted">按保留精度显示，窗口包含切入前的部分记录；缺口不连接。</p>{error ? <p role="status">趋势暂不可用，正在重试。</p> : !history ? <p>正在读取…</p> : <Suspense fallback={<p>正在加载趋势…</p>}><Chart history={history} metric={metric} unit={metric === 'soc' ? '%' : metric === 'cell_delta_mv' ? 'mV' : 'W'}/></Suspense>}</section>;
}
