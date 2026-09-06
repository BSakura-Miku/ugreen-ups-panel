export type CalibrationVoltage = 12 | 19 | 20;
export type SamplingMode = 'online' | 'charging';
export type SamplingFrame = {
  timestamp: number;
  mode: string;
  inputVoltage: number | null;
  baseRaw: number | null;
  chargePower: number | null;
  device: string;
  source: string;
  revision: string;
  decoderVersion?: number | string;
  formulaVersion?: number | string;
  fresh: boolean;
};
type SamplingPoint = { timestamp: number; base: number; charge: number | null };
export type SamplingWindow = {
  mode: SamplingMode;
  voltage: CalibrationVoltage;
  first: number;
  last: number;
  count: number;
  meanBase: number;
  meanCharge: number | null;
  baseSpread: number;
  chargeSpread: number | null;
  identity: string;
};
export type SamplingState = {
  mode: SamplingMode;
  voltage: CalibrationVoltage;
  phase: 'collecting' | 'complete' | 'rejected';
  points: SamplingPoint[];
  identity: string | null;
  count: number;
  elapsed: number;
  reason: string | null;
  window: SamplingWindow | null;
};
export type CalculationResult = { value: number; error: null } | { value: null; error: string };
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

function exceedsTenPercent(values: number[], mean: number): boolean {
  const maximum = Math.max(...values), range = maximum - Math.min(...values);
  // Decimal telemetry can put an exact boundary a few binary rounding steps
  // above it. Compare at the raw-value scale; keep stored means/spreads intact.
  const roundoff = 4 * Number.EPSILON * Math.max(maximum, mean);
  return range - mean * .1 > roundoff;
}
function belowChargeMinimum(mean: number): boolean {
  return 2 - mean > 4 * Number.EPSILON * Math.max(2, Math.abs(mean));
}

export function suggestVoltage(input: number | null | undefined): CalibrationVoltage | null {
  if (!finite(input)) return null;
  const choices: CalibrationVoltage[] = [12, 19, 20];
  return choices.filter(value => Math.abs(value - input) <= 1).sort((a, b) => Math.abs(a - input) - Math.abs(b - input))[0] ?? null;
}
export function samplingIdentity(frame: SamplingFrame): string {
  return JSON.stringify([frame.mode, frame.device, frame.source, frame.revision, frame.decoderVersion, frame.formulaVersion]);
}
export function createSamplingState(mode: SamplingMode, voltage: CalibrationVoltage): SamplingState {
  return { mode, voltage, phase: 'collecting', points: [], identity: null, count: 0, elapsed: 0, reason: null, window: null };
}
function reset(state: SamplingState, reason: string): SamplingState {
  return { ...createSamplingState(state.mode, state.voltage), reason };
}

export function advanceSampling(state: SamplingState, frame: SamplingFrame): SamplingState {
  if (state.phase !== 'collecting') return state;
  if (!frame.fresh) return reset(state, '实时数据失鲜，采样已重置，等待连接恢复。');
  if (!finite(frame.timestamp)) return reset(state, '采样时间无效，采样已重置。');
  if (frame.mode !== state.mode) return reset(state, state.mode === 'online' ? '等待外部供电且未充电；状态变化后重新采样。' : '等待电池充电；状态变化后重新采样。');
  if (!finite(frame.inputVoltage) || Math.abs(frame.inputVoltage - state.voltage) > 1) return reset(state, `适配器输入需在 ${state.voltage - 1}–${state.voltage + 1} V，采样已重置。`);
  if (!finite(frame.baseRaw) || frame.baseRaw <= 0) return reset(state, '设备基底读数无效，采样已重置。');
  if (state.mode === 'charging' && (!finite(frame.chargePower) || frame.chargePower <= 0)) return reset(state, '回充原始功率无效，采样已重置。');
  const identity = samplingIdentity(frame);
  let next = state;
  const last = state.points.at(-1);
  if (state.identity !== null && state.identity !== identity) next = reset(state, '设备、来源或计算配置发生变化，采样已重置。');
  else if (last && frame.timestamp < last.timestamp) next = reset(state, '采样时间倒序，采样已重置。');
  else if (last && frame.timestamp - last.timestamp > 5) next = reset(state, '采样间隔超过 5 秒，采样已重置。');
  else if (last && frame.timestamp === last.timestamp) return state;
  const points = [...next.points, { timestamp: frame.timestamp, base: frame.baseRaw, charge: state.mode === 'charging' ? frame.chargePower : null }];
  const elapsed = frame.timestamp - points[0].timestamp;
  next = { ...next, points, identity, count: points.length, elapsed };
  if (elapsed < 30) return next;
  if (points.length < 12) return { ...next, phase: 'rejected', reason: '30 秒内独立采样不足 12 条，请重新采样。' };
  const bases = points.map(point => point.base);
  const meanBase = bases.reduce((sum, value) => sum + value, 0) / bases.length;
  const baseSpread = (Math.max(...bases) - Math.min(...bases)) / meanBase;
  const charges = state.mode === 'charging' ? points.map(point => point.charge!) : [];
  const meanCharge = charges.length ? charges.reduce((sum, value) => sum + value, 0) / charges.length : null;
  const chargeSpread = meanCharge !== null ? (Math.max(...charges) - Math.min(...charges)) / meanCharge : null;
  if (exceedsTenPercent(bases, meanBase) || (meanCharge !== null && exceedsTenPercent(charges, meanCharge))) return { ...next, phase: 'rejected', reason: '相关原始读数波动超过均值的 10%，请保持负载稳定后重新采样。' };
  if (meanCharge !== null && belowChargeMinimum(meanCharge)) return { ...next, phase: 'rejected', reason: '平均回充原始功率低于 2 W，暂不计算回充系数，请等待回充稳定后重采。' };
  return { ...next, phase: 'complete', reason: null, window: { mode: state.mode, voltage: state.voltage, first: points[0].timestamp, last: frame.timestamp, count: points.length, meanBase, meanCharge, baseSpread, chargeSpread, identity } };
}

export function calculateBase(window: SamplingWindow, watts: number): CalculationResult {
  if (!finite(watts) || watts <= 0) return { value: null, error: '请输入采样同期稳定的插座功率，且大于 0 W。' };
  if (window.mode !== 'online' || !finite(window.meanBase) || window.meanBase <= 0) return { value: null, error: '请先完成第一步的有效采样。' };
  const value = watts / window.meanBase;
  return finite(value) && value > 0 && value <= 10 ? { value, error: null } : { value: null, error: '交流基底系数需大于 0 且不超过 10，请核对同期功率后重新采样。' };
}
export function calculateCharge(window: SamplingWindow, watts: number, base: number): CalculationResult {
  if (!finite(watts) || watts <= 0) return { value: null, error: '请输入本次充电采样同期稳定的插座功率，且大于 0 W。' };
  if (!finite(base) || base <= 0 || base > 10) return { value: null, error: '请先完成或确认同一档位的交流基底系数。' };
  if (window.mode !== 'charging' || !finite(window.meanCharge) || belowChargeMinimum(window.meanCharge) || !finite(window.meanBase) || window.meanBase <= 0) return { value: null, error: '请完成平均回充原始功率至少 2 W 的有效充电采样。' };
  const value = (watts - base * window.meanBase) / window.meanCharge;
  return finite(value) && value >= 0 && value <= 10 ? { value, error: null } : { value: null, error: value < 0 ? '回充结果为负，请核对基底系数与同期插座功率后重新采样。' : '回充系数不能超过 10，请核对同期功率后重新采样。' };
}
