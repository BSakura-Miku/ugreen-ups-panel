import { BatteryCharging, CircleHelp } from 'lucide-react';
import type { CellBalanceReport, Sample } from './types';
import { cellBalancePresentation, cellNumberLabel, shortDurationLabel } from './batteryDisplay';

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const rawNumber = (value: unknown, digits: number) => finite(value) ? value.toFixed(digits) : '—';
const progress = (value: number, maximum: number) => finite(value) ? Math.max(0, Math.min(maximum, value)) : 0;

export default function CellBalanceCard({ sample, report, fresh, trendHours, trendMetric, onTrend }: {
  sample: Sample | null | undefined;
  report: CellBalanceReport | null | undefined;
  fresh: boolean;
  trendHours: number;
  trendMetric: string;
  onTrend: (days: 7 | 30) => void;
}) {
  const current = fresh ? sample : null;
  const view = cellBalancePresentation(report, sample?.mode, fresh);
  const supported = report?.schema === 1 ? report : null;
  const standbyRequired = supported && finite(supported.required_standby_sec) && supported.required_standby_sec > 0 ? supported.required_standby_sec : 1800;
  const persistence = supported && finite(supported.persistence_sec) && supported.persistence_sec > 0 ? supported.persistence_sec : 120;
  const recent = !!supported && supported.recent_sample_count > 0;
  const currentLowest = fresh && supported && ['online', 'charging', 'battery'].includes(sample?.mode ?? '') ? supported.lowest_cells : [];
  return <article className="panel cells cell-balance" aria-labelledby="cell-balance-heading">
    <div className="panel-heading"><div><h3 id="cell-balance-heading">电芯状态</h3><p>4 节串联 · 待机压差参考</p></div><BatteryCharging size={19} /></div>
    <div className="delta cell-balance-delta"><div><strong>{rawNumber(current?.cell_delta_mv, 0)}<span>mV</span></strong><span>当前压差</span></div><span className={`cell-reference-state ${view.tone}`}>{view.label}</span></div>
    <p className="cell-balance-advice">{view.advice}</p>
    {view.showStandby && supported && <div className="cell-observation">
      <div><span>连续待机观察</span><strong>{shortDurationLabel(supported.standby_duration_sec)}{supported.standby_duration_sec < standbyRequired ? ' / 30 分钟' : ' · 已满足 30 分钟'}</strong></div>
      <progress value={progress(supported.standby_duration_sec, standbyRequired)} max={standbyRequired} aria-label="连续待机观察进度" />
      {view.showConfirmation && <><div className="cell-confirmation-label"><span>本轮等级确认</span><strong>{shortDurationLabel(supported.candidate_duration_sec)} / 2 分钟</strong></div><progress value={progress(supported.candidate_duration_sec, persistence)} max={persistence} aria-label="当前参考等级确认进度" /></>}
      <p>{view.showConfirmation ? '等级变化后重新确认，观察期间不沿用先前等级。' : '按后台收到的连续样本计时；中断或工况变化后重新观察。'}</p>
    </div>}
    <div className="cell-list">{[0, 1, 2, 3].map(index => <div key={index} className={`cell-row ${currentLowest?.includes(index + 1) ? 'cell-current-lowest' : ''}`}>
      <span>电芯 0{index + 1}</span><div className="cell-track"><i style={{ width: finite(current?.cells?.[index]) ? `${Math.max(0, Math.min(100, (current.cells[index] - 2.5) / 1.8 * 100))}%` : '0%' }} /></div><strong>{rawNumber(current?.cells?.[index], 3)} <small>V</small></strong>
    </div>)}</div>
    {currentLowest?.length > 0 && <p className="cell-current-note">当前最低：{currentLowest.map(cellNumberLabel).join('、')}{currentLowest.length > 1 ? '（并列）' : ''} · 单次读数不代表异常</p>}
    <div className="cell-recent-window">
      <h4>{fresh ? '最近待机窗口' : '上次收到的待机统计'} <span>最近 30 分钟内</span></h4>
      {recent ? <><div className="cell-recent-values"><div><span>最常偏低</span><strong>{cellNumberLabel(supported.frequent_lowest_cell)}</strong></div><div><span>窗口压差峰值</span><strong>{rawNumber(supported.recent_max_delta_mv, 0)} <small>mV</small></strong></div></div><p>{supported.recent_sample_count} 条本次连续待机样本{finite(supported.sample_timestamp) ? ` · 截至 ${new Date(supported.sample_timestamp * 1000).toLocaleTimeString('zh-CN', { hour12: false })}` : ''}</p></> : <p>连续待机后积累窗口数据，充放电读数不计入此统计。</p>}
    </div>
    <div className="cell-history-actions"><span>查看压差趋势</span><div className="range" role="group" aria-label="电芯压差趋势快捷范围">{([7, 30] as const).map(days => <button key={days} type="button" aria-pressed={trendMetric === 'cell_delta_mv' && trendHours === days * 24} className={trendMetric === 'cell_delta_mv' && trendHours === days * 24 ? 'active' : ''} onClick={() => onTrend(days)}>{days} 天</button>)}</div></div>
    <details className="cell-threshold-details"><summary>参考阈值与观察方式</summary>
      <dl><div><dt>小于 20 mV</dt><dd>一致性较好</dd></div><div><dt>20 至小于 50 mV</dt><dd>轻微差异</dd></div><div><dt>50 至小于 100 mV</dt><dd>压差偏大</dd></div><div><dt>100 mV 及以上</dt><dd>建议检查</dd></div></dl>
      <p>这些参考阈值未获厂家验证。仅在外部供电且电池未充电、连续待机满 30 分钟后使用；当前参考区间还需持续 2 分钟才确认。</p>
      <p>最常偏低按最近窗口中单独最低的出现次数统计；同次并列不计入次数，次数相同时不指定单一电芯。窗口峰值来自实际压差读数。</p>
    </details>
    <div className="cell-note"><CircleHelp size={14} /><span>压差不等于电池容量或健康度（SOH），不能据此计算健康百分比。</span></div>
  </article>;
}
