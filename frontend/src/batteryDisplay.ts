import type { BatteryEnergyBasis, BatterySessionEnergy, CellBalanceLevel, CellBalanceReport } from './types';

const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

export function energyWhLabel(value: number | null | undefined): string {
  if (!finite(value) || value < 0) return '—';
  return value > 0 && value < .01 ? '< 0.01' : value.toFixed(2);
}

export function energyCoverageLabel(value: number | null | undefined): string {
  if (!finite(value) || value < 0 || value > 1) return '—';
  if (value === 1) return '100%';
  if (value > 0 && value < .001) return '< 0.1%';
  // Do not round a partially covered observation up to "100%".
  return `${(Math.floor(value * 1000) / 10).toFixed(1).replace(/\.0$/, '')}%`;
}

const energyReasons: Record<string, string> = {
  not_configured: '电池放电尚未校准',
  invalid_basis: '部分样本缺少有效的功率依据',
  invalid_power: '部分功率读数不可用于能量估算',
  sample_gap: '采样间隔过长，缺口未补算',
  timestamp_rollback: '采样时间发生倒序，相关区间未计入',
  context_changed: '设备、数据来源或校准配置发生变化',
  continuity_lost: '观测连续性中断，缺口未补算',
  invalid_energy: '部分区间的能量计算不可用',
  insufficient_samples: '有效连续采样不足，尚不能估算能量',
};

export function energyReasonLabels(reasons: string[] | null | undefined): string[] {
  if (!Array.isArray(reasons)) return [];
  return [...new Set(reasons.map(reason => Object.hasOwn(energyReasons, reason) ? energyReasons[reason] : '部分观测存在数据缺口或尚未确认的原因'))];
}

export function energyEstimateValue(energy: BatterySessionEnergy | null | undefined): number | null {
  return energy?.schema === 1 && energy.interval_count > 0
    && (energy.status === 'available' || energy.status === 'partial')
    && finite(energy.estimate_wh) && energy.estimate_wh >= 0 ? energy.estimate_wh : null;
}

export function energyStatusLabel(energy: BatterySessionEnergy | null | undefined): string {
  if (!energy) return '旧记录未记录能量';
  if (energy.schema !== 1) return '此记录的能量暂不可展示';
  const hasEstimate = energyEstimateValue(energy) !== null;
  if (energy.status === 'available' && hasEstimate) return '观测区间已覆盖';
  if (energy.status === 'partial' && hasEstimate) return '覆盖不完整';
  if (Array.isArray(energy.reasons) && energy.reasons.includes('not_configured')) return '电池放电尚未校准';
  return '暂无可用能量估算';
}

export function energyBasisLabel(basis: BatteryEnergyBasis | null | undefined): string {
  if (!basis) return '尚无有效的功率依据';
  if (basis.estimate_basis === 'nominal_43_2wh_soc_v1') return '标称容量假设 · 未独立验证';
  if (basis.profile === 'custom' || (typeof basis.estimate_basis === 'string' && basis.estimate_basis.startsWith('us3000_battery_custom_'))) return '自定义倍率 · 未独立验证';
  return '已记录的电池放电倍率 · 依据待确认';
}

export function shortDurationLabel(seconds: number | null | undefined): string {
  if (!finite(seconds) || seconds < 0) return '—';
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600), minutes = Math.floor(total % 3600 / 60), remainder = total % 60;
  if (hours) return `${hours} 小时 ${minutes} 分 ${remainder} 秒`;
  return minutes ? `${minutes} 分 ${remainder} 秒` : `${remainder} 秒`;
}

const levels: Record<CellBalanceLevel, { label: string; advice: string }> = {
  good: { label: '一致性较好', advice: '当前待机窗口内压差较小，继续观察长期变化。' },
  minor: { label: '轻微差异', advice: '关注后续待机趋势，观察差异是否持续扩大。' },
  elevated: { label: '压差偏大', advice: '建议对照后续待机窗口复查，关注是否持续出现。' },
  check: { label: '建议检查', advice: '建议复查待机读数；若持续出现，可联系售后检查。' },
};

export type CellBalancePresentation = {
  label: string;
  advice: string;
  tone: 'neutral' | CellBalanceLevel;
  showStandby: boolean;
  showConfirmation: boolean;
};

export function cellBalancePresentation(report: CellBalanceReport | null | undefined, mode: string | undefined, fresh: boolean): CellBalancePresentation {
  const neutral = (label: string, advice: string, showStandby = false, showConfirmation = false): CellBalancePresentation => ({ label, advice, tone: 'neutral', showStandby, showConfirmation });
  if (!fresh) return neutral('等待实时数据', '采集恢复后再显示参考状态，过期读数不参与当前评级。');
  if (mode === 'charging') return neutral('充电中 · 仅显示读数', '充电会影响瞬时压差，待充电停止并连续待机后再参考分级。');
  if (mode === 'battery') return neutral('放电中 · 仅显示读数', '负载会影响瞬时压差，待恢复外部供电并连续待机后再参考分级。');
  if (mode !== 'online') return neutral('等待工况确认', '当前供电状态尚未确认，暂不显示参考等级。');
  if (!report || report.schema !== 1) return neutral('参考状态暂未提供', '当前服务未提供连续待机观察，请更新面板后等待有效数据。');
  if (report.state === 'unavailable') return neutral('读数暂不可评估', report.reason === 'invalid_sample' ? '电芯读数暂不可用，等待下一条有效采样。' : '后台尚无新的有效观测，等待采集恢复。');
  if (!report.observed) return neutral('等待后台观测', '当前样本尚未纳入连续待机观察，收到后台观测后开始计时。');
  if (report.state === 'settling') return neutral('等待待机稳定', '保持外部供电且电池未充电，连续待机满 30 分钟后再参考分级。', true);
  if (report.state === 'observing') return neutral('等级观察中', '当前参考区间仍在确认，持续 2 分钟后再显示已确认等级。', true, true);
  if (report.state === 'assessed' && report.level && Object.hasOwn(levels, report.level)) {
    const level = levels[report.level];
    return { label: level.label, advice: level.advice, tone: report.level, showStandby: true, showConfirmation: false };
  }
  return neutral('参考状态待确认', '当前没有已确认的参考等级，请等待后续有效观测。');
}

export function cellNumberLabel(index: number | null | undefined): string {
  return finite(index) && Number.isInteger(index) && index >= 1 && index <= 4 ? `电芯 0${index}` : '无单一结果';
}
