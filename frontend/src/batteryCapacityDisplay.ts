import type { BatteryEnergyBasis } from './types';

export type BatteryCapacityStatus = 'not_configured' | 'incompatible' | 'waiting_reference' | 'collecting' | 'reference_ready' | 'preliminary' | 'trend';
export type CapacityObservation = {
  id: string | number;
  start_ts: number;
  end_ts: number;
  start_soc: number;
  end_soc: number;
  estimate_wh: number;
  duration_sec: number;
  avg_power_w: number;
  power_cv: number;
  min_power_w: number;
  max_power_w: number;
  min_battery_voltage_v: number | null;
  max_battery_voltage_v: number | null;
  max_cell_delta_mv: number | null;
  sample_count: number;
  interval_count: number;
  coverage_ratio: number;
  accepted: boolean;
  reason: string | null;
  index_pct: number | null;
  change_pct: number | null;
};
export type BatteryCapacityReport = {
  schema: 1;
  status: BatteryCapacityStatus;
  reason: string | null;
  epoch: { id: string | number; activated_at: number; basis: BatteryEnergyBasis } | null;
  current_basis: BatteryEnergyBasis | null;
  baseline: CapacityObservation | null;
  comparison: { sample_count: number; index_pct: number; change_pct: number; latest_end_ts: number } | null;
  progress: { start_ts: number; current_soc: number; completed_pp: number; target_drop_pp: number; estimate_wh: number; duration_sec: number } | null;
  recent: CapacityObservation[];
  criteria: { soc_start: number; soc_end: number; max_gap_sec: number; load_tolerance: number; max_power_cv: number; trend_samples: number; min_duration_sec: number };
  temperature_known: boolean;
  capture_fresh: boolean;
  sample_timestamp: number | null;
  generated_at: number;
};

export const finiteCapacityNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const positive = (value: unknown): value is number => finiteCapacityNumber(value) && value > 0;
const validId = (value: unknown) => typeof value === 'string' && value.length > 0 || finiteCapacityNumber(value);
const nullableText = (value: unknown) => value === null || typeof value === 'string';
const statuses: BatteryCapacityStatus[] = ['not_configured', 'incompatible', 'waiting_reference', 'collecting', 'reference_ready', 'preliminary', 'trend'];

function validObservation(value: unknown): value is CapacityObservation {
  if (!object(value)) return false;
  return validId(value.id)
    && positive(value.start_ts) && positive(value.end_ts) && value.end_ts >= value.start_ts
    && value.start_soc === 90 && value.end_soc === 80
    && positive(value.estimate_wh) && positive(value.duration_sec) && positive(value.avg_power_w)
    && typeof value.accepted === 'boolean' && nullableText(value.reason)
    && (value.accepted ? positive(value.index_pct) : value.index_pct === null || positive(value.index_pct))
    && (value.change_pct === null || finiteCapacityNumber(value.change_pct));
}

// Unknown schemas or malformed summaries never become a plausible capacity value.
export function parseBatteryCapacityReport(value: unknown): BatteryCapacityReport | null {
  if (!object(value) || value.schema !== 1 || !statuses.includes(value.status as BatteryCapacityStatus)
    || !nullableText(value.reason) || !Array.isArray(value.recent) || !value.recent.every(validObservation)
    || typeof value.capture_fresh !== 'boolean' || typeof value.temperature_known !== 'boolean'
    || !(value.sample_timestamp === null || positive(value.sample_timestamp)) || !positive(value.generated_at)) return null;
  if (value.epoch !== null && (!object(value.epoch) || !validId(value.epoch.id) || !positive(value.epoch.activated_at) || !object(value.epoch.basis))) return null;
  if (value.current_basis !== null && !object(value.current_basis)) return null;
  if (value.baseline !== null && (!validObservation(value.baseline) || !value.baseline.accepted || value.baseline.index_pct !== 100 || value.baseline.change_pct !== 0)) return null;
  if (value.comparison !== null && (!object(value.comparison) || !Number.isInteger(value.comparison.sample_count)
    || !positive(value.comparison.sample_count) || value.comparison.sample_count > 3 || !positive(value.comparison.index_pct)
    || !finiteCapacityNumber(value.comparison.change_pct) || !positive(value.comparison.latest_end_ts) || !value.baseline)) return null;
  if (value.progress !== null && (!object(value.progress) || !positive(value.progress.start_ts)
    || !finiteCapacityNumber(value.progress.current_soc) || value.progress.current_soc < 0 || value.progress.current_soc > 100
    || !finiteCapacityNumber(value.progress.completed_pp) || value.progress.completed_pp < 0 || value.progress.completed_pp > 10
    || value.progress.target_drop_pp !== 10 || !finiteCapacityNumber(value.progress.estimate_wh) || value.progress.estimate_wh < 0
    || !finiteCapacityNumber(value.progress.duration_sec) || value.progress.duration_sec < 0)) return null;
  if (!object(value.criteria) || value.criteria.soc_start !== 90 || value.criteria.soc_end !== 80
    || value.criteria.trend_samples !== 3 || !positive(value.criteria.max_gap_sec)
    || !positive(value.criteria.load_tolerance) || value.criteria.load_tolerance > 1 || !positive(value.criteria.max_power_cv)
    || !positive(value.criteria.min_duration_sec)) return null;
  return value as BatteryCapacityReport;
}

const reasonLabels: Record<string, string> = {
  not_configured: '配置电池放电倍率后开始积累参考记录。',
  invalid_basis: '当前功率计算依据不完整，暂时不能比较。',
  stale: '采集数据已离线，显示上次已完成的观测。',
  invalid_timestamp: '本轮采样时间无效，等待新的连续放电记录。',
  timestamp_rollback: '采样时间发生回退，本轮观察已中止。',
  sample_gap: '本轮采样中断，等待新的连续放电记录。',
  context_changed: '设备或电池放电校准依据已变化，当前记录无法与固定基线比较。',
  restart: '采集服务已重启，等待新的连续放电记录。',
  unknown_mode: '供电状态暂未确认，本轮连续观察已暂停。',
  invalid_soc: '本轮电量读数无效，等待新的连续放电记录。',
  soc_rebound: '本轮电量出现回升，未计入可比较记录。',
  soc_jump: '本轮电量变化过快，未计入可比较记录。',
  missed_start: '本轮未完整记录 90% 起点，等待后续连续放电记录。',
  missed_end: '本轮未完整记录 80% 终点，未计入可比较记录。',
  invalid_power: '本轮功率读数无效，未计入可比较记录。',
  invalid_energy: '本轮能量积分无效，未计入可比较记录。',
  external_before_end: '本轮在 80% 前恢复外部供电，未形成完整区间记录。',
  unstable_load: '本轮负载波动较大，未计入可比较记录。',
  load_mismatch: '本轮平均负载与基线相差较大，未计入可比较记录。',
  waiting_external: '本次放电已记录，等待后续独立放电周期。',
  waiting_start: '等待后续经过 90%→80% 的连续放电记录。',
  short_window: '本轮区间持续时间过短，未计入可比较记录。',
  not_observed: '新的采样正在处理，稍后更新本轮观察进度。',
};

export function capacityReasonLabel(reason: string | null | undefined): string | null {
  if (!reason) return null;
  return Object.hasOwn(reasonLabels, reason) ? reasonLabels[reason] : '本轮尚不满足比较条件，等待后续连续放电记录。';
}

export function capacityDate(value: unknown): string {
  if (!positive(value)) return '—';
  const date = new Date(value * 1000);
  if (!Number.isFinite(date.getTime())) return '—';
  return date.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}

export function capacityNumber(value: unknown, digits = 1): string {
  return finiteCapacityNumber(value) && value >= 0 ? value.toFixed(digits) : '—';
}

export function capacityIndexLabel(value: unknown): string {
  if (!positive(value)) return '—';
  return value.toFixed(1).replace(/\.0$/, '');
}

export function capacityPresentation(report: BatteryCapacityReport | null, error = false) {
  const empty = { index: null, sampleCount: 0, isBaseline: false, historical: false, observationTimestamp: null, showProgress: false,
    label: error ? '暂不可比较' : '读取中', tone: 'muted', advice: error ? '相对容量参考暂时无法读取，正在重试。' : '正在读取本次校准的参考记录。' };
  if (error || !report) return empty;
  const historical = !report.capture_fresh || report.reason === 'stale';
  if (report.status === 'not_configured' || report.status === 'incompatible' || report.reason === 'invalid_basis') return {
    ...empty, label: '暂不可比较', historical, tone: 'muted',
    advice: capacityReasonLabel(report.reason) || (report.status === 'not_configured' ? reasonLabels.not_configured : reasonLabels.context_changed),
  };
  const sampleCount = report.comparison?.sample_count ?? 0;
  const index = report.baseline ? report.comparison?.index_pct ?? 100 : null;
  const showProgress = !!report.progress && !historical;
  const isBaseline = !!report.baseline && !report.comparison;
  const label = !report.baseline ? '基线建立中' : sampleCount >= report.criteria.trend_samples ? '可比较' : '初步观察';
  let advice = !report.baseline
    ? showProgress ? '正在记录参考区间；完整观察到 80% 后检查连续性与负载。' : '参考起点已固定，等待 90%→80% 的连续放电记录。'
    : isBaseline ? '本次校准基线已建立，定义为 100%；后续匹配记录将与它比较。'
    : `已积累 ${sampleCount} 次匹配放电，指数取最近 ${sampleCount} 次的中位数。`;
  if (report.reason && report.reason !== 'waiting_start' && report.reason !== 'waiting_external') advice = capacityReasonLabel(report.reason) ?? advice;
  if (historical) advice = report.baseline ? reasonLabels.stale : '采集数据已离线，参考起点已保留，等待恢复后继续观察。';
  return { index, sampleCount, isBaseline, historical, observationTimestamp: report.comparison?.latest_end_ts ?? report.baseline?.end_ts ?? null,
    showProgress, label, tone: sampleCount >= report.criteria.trend_samples ? 'ready' : 'observing', advice };
}

// The same accepted records used by the server summary supply the displayed
// median energy and load. A partial list must not masquerade as that summary.
export function capacityComparisonValues(report: BatteryCapacityReport | null): { estimateWh: number; avgPowerW: number; count: number } | null {
  if (!report?.baseline || !report.comparison || ['incompatible', 'not_configured'].includes(report.status) || report.reason === 'invalid_basis') return null;
  const count = report.comparison.sample_count;
  if (!Number.isInteger(count) || count < 1 || count > report.criteria.trend_samples) return null;
  const rows = report.recent.filter(row => row.accepted).sort((a, b) => b.end_ts - a.end_ts).slice(0, count);
  if (rows.length !== count || rows[0].end_ts !== report.comparison.latest_end_ts) return null;
  const median = (values: number[]) => { const sorted = [...values].sort((a, b) => a - b); const middle = Math.floor(sorted.length / 2); return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2; };
  const estimateWh = median(rows.map(row => row.estimate_wh));
  const avgPowerW = median(rows.map(row => row.avg_power_w));
  if (!positive(estimateWh) || !positive(avgPowerW)) return null;
  // Summary rounding is allowed; observations from a different aggregation are not.
  if (Math.abs(estimateWh / report.baseline.estimate_wh * 100 - report.comparison.index_pct) > 0.11) return null;
  return { estimateWh, avgPowerW, count };
}
