import type { BatterySessionEnergy as Energy } from './types';
import { energyBasisLabel, energyCoverageLabel, energyEstimateValue, energyReasonLabels, energyStatusLabel, energyWhLabel, shortDurationLabel } from './batteryDisplay';
import { formatCoefficient } from './calibrationDraft';

const soc = (value: number | null) => typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1).replace(/\.0$/, '')}%` : '—';

export default function BatterySessionEnergy({ energy }: { energy: Energy | null | undefined }) {
  const current = energy?.schema === 1 ? energy : null;
  const reasons = energyReasonLabels(current?.reasons);
  const gain = current?.basis?.battery_gain;
  const hasGain = typeof gain === 'number' && Number.isFinite(gain) && gain > 0;
  return <div className="session-energy">
    <div className="session-energy-value"><strong>{energyWhLabel(energyEstimateValue(current))}</strong><small className="session-inline-unit">Wh · 估算</small></div>
    <small className={`session-energy-status ${current?.status === 'partial' ? 'partial' : ''}`}>{energyStatusLabel(energy)}</small>
    {current && <>
      <small>覆盖 {energyCoverageLabel(current.coverage_ratio)} · {shortDurationLabel(current.covered_duration_sec)}</small>
      <details className="session-energy-details"><summary>依据与缺口</summary>
        <p>功率依据：{energyBasisLabel(current.basis)}</p>
        {hasGain && <p>电池放电倍率 g：<span title={String(gain)}>{formatCoefficient(gain)}</span>（保留原始精度计算）</p>}
        <p>观测时长：{shortDurationLabel(current.observed_duration_sec)}；有效覆盖：{shortDurationLabel(current.covered_duration_sec)}。</p>
        <p>能量观测电量：{soc(current.start_soc)} → {soc(current.end_soc)}。</p>
        {reasons.length ? <ul>{reasons.map(reason => <li key={reason}>{reason}</li>)}</ul> : <p>{current.status === 'available' ? '首末电池供电样本之间未记录到能量覆盖缺口。' : '尚无足够的连续数据可供估算。'}</p>}
        <p>按相邻有效样本的未平滑放电原始功率 × g 积分；与主卡的 8 秒平滑功率显示不同。仅代表本次观测范围内的电池端能量。</p>
      </details>
    </>}
  </div>;
}
