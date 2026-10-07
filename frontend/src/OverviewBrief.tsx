import { useEffect, useState } from 'react';
import type { BatterySessions, LiveView } from './types';
import { readJson, startVisiblePolling } from './readPolling';
import { readEnergyUsageMonth, energyKwh, energyCoverage } from './energyUsageDisplay';
import { cellBalancePresentation } from './batteryDisplay';
import { capacityPresentation, parseBatteryCapacityReport } from './batteryCapacityDisplay';
import type { BatteryCapacityReport } from './batteryCapacityDisplay';
import type { EnergyUsageMonth } from './energyUsageDisplay';

export default function OverviewBrief({ view, fresh, estimateIssue }: { view: LiveView | null; fresh: boolean; estimateIssue: string | null }) {
  const [month, setMonth] = useState<EnergyUsageMonth | null>(null);
  const [sessions, setSessions] = useState<BatterySessions | null>(null);
  const [capacity, setCapacity] = useState<BatteryCapacityReport | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => startVisiblePolling(async signal => {
    try {
      const [m, s, c] = await Promise.all([readEnergyUsageMonth(null, signal), readJson<BatterySessions>('/api/battery-sessions?days=90&limit=1', signal), readJson('/api/battery-capacity', signal)]);
      if (!signal.aborted) { setMonth(m); setSessions(s); setCapacity(parseBatteryCapacityReport(c)); setError(false); }
    } catch { if (!signal.aborted) setError(true); }
  }, () => 30000), []);
  const cell = cellBalancePresentation(view?.cell_balance, view?.sample?.mode, fresh);
  const capacityView = capacityPresentation(capacity, error);
  const latest = sessions?.records[0];
  const issues = [!fresh ? { text: '采集暂不可用，实时读数隐藏，历史累计可能出现缺口。', link: '#diagnostics', action: '检查采集连接' } : null,
    view?.storage_error ? { text: '历史写入异常，实时读数仍可查看。', link: '#diagnostics', action: '检查存储状态' } : null,
    estimateIssue ? { text: `功率与能量估算暂停：${estimateIssue}`, link: '#calibration', action: '检查校准配置' } : null].filter(Boolean);
  return <>
    {!!issues.length && <section className="panel anomaly-summary"><h3>需要关注</h3>{issues.map((issue, i) => issue && <p key={i}>{issue.text} <a href={issue.link}>{issue.action} →</a></p>)}</section>}
    <section className="brief-grid">
      <article className="panel"><h3>本月用电 · 估算</h3><strong className="brief-number">{error ? '—' : energyKwh(month?.summary.estimate_kwh)} <small>kWh</small></strong><p className="muted">{error ? '摘要暂不可用，正在重试' : `已记录部分 · 覆盖 ${energyCoverage(month?.summary.coverage_ratio)}`}</p><a href="#energy">查看日历与费用 →</a></article>
      <article className="panel"><h3>电池观察</h3><p>电芯压差 <strong>{fresh && Number.isFinite(view?.sample?.cell_delta_mv) ? `${view?.sample?.cell_delta_mv} mV` : '—'}</strong> · {cell.label}</p><p className="muted">容量参考 · {capacityView.label}{capacity && !error && !['not_configured', 'incompatible'].includes(capacity.status) ? ` · 匹配 ${capacityView.sampleCount}/3` : ''}</p><a href="#battery">查看电芯与参考进度 →</a></article>
      <article className="panel"><h3>最近一次电池供电</h3>{error ? <p>摘要暂不可用</p> : latest ? <><p>{new Date(latest.start_ts * 1000).toLocaleString('zh-CN', {hour12:false})}</p><strong>{Math.floor(latest.observed_duration_sec / 60)} 分钟 · {latest.status === 'complete' ? '完整记录' : latest.status === 'ongoing' ? '最近记录中' : '不完整记录'}</strong></> : <p className="muted">近 90 天暂无记录</p>}<p><a href="#battery">查看供电详情 →</a></p></article>
    </section>
  </>;
}
