import type { CalibrationCoefficients, CalibrationConfig, CalibrationLegacyCoefficients, CalibrationProfile, CalibrationReportedConfig, CalibrationState, CalibrationVoltage } from './types';
import { calibrationEditRevision, parseCalibrationConfig } from './calibrationState.ts';

export type CoefficientKey = keyof CalibrationCoefficients;
export type CoefficientSource = 'empty' | 'existing' | 'legacy' | 'manual' | 'assistant' | 'preset';
export type CoefficientValues = Record<CoefficientKey, string>;
export type CalibrationDraft = {
  schema: 1 | 2;
  profile: CalibrationProfile | null;
  configurationIssue?: string;
  voltage: CalibrationVoltage | null;
  voltageConfirmed: boolean;
  values: CoefficientValues;
  displayValues: CoefficientValues;
  sources: Record<CoefficientKey, CoefficientSource>;
  revision: string;
};
const keys: CoefficientKey[] = ['base_gain', 'charge_gain', 'battery_gain'];
const emptyValues = (): CoefficientValues => ({ base_gain: '', charge_gain: '', battery_gain: '' });
export function formatCoefficient(value: number | null | undefined): string {
  if (value === null || value === undefined) return '未校准';
  if (!Number.isFinite(value)) return '待修正';
  const rounded = Number(value.toFixed(4));
  return rounded === 0 && value !== 0 ? value.toExponential(3).replace(/\.?0+e/, 'e') : String(rounded);
}
export function createDraft(value: CalibrationReportedConfig): CalibrationDraft {
  const config = parseCalibrationConfig(value);
  const values = emptyValues(), displayValues = emptyValues();
  const sources: CalibrationDraft['sources'] = { base_gain: 'empty', charge_gain: 'empty', battery_gain: 'empty' };
  if (config.schema === null) return { schema: 1, profile: null, voltage: null, voltageConfirmed: false, values, displayValues, sources, revision: config.revision, configurationIssue: config.invalid_reason };
  for (const key of keys) {
    const value = config.coefficients?.[key];
    if (typeof value === 'number') {
      values[key] = String(value); displayValues[key] = formatCoefficient(value);
      sources[key] = config.schema === 2 ? 'existing' : config.profile === 'local-19v-v1' ? 'preset' : 'legacy';
    }
  }
  return { schema: config.schema, profile: config.profile, voltage: config.schema === 2 ? config.ac_voltage_nominal_v : null,
    voltageConfirmed: config.schema === 2, values, displayValues, sources, revision: config.revision };
}
export function createStateDraft(data: CalibrationState): CalibrationDraft {
  const config = data.configuration_problem?.source === 'desired'
    ? { schema: null, profile: data.configuration_problem.profile ?? null, coefficients: null, revision: calibrationEditRevision(data), invalid_reason: '待保存配置无效，请主动选择校准方式并重新填写。' } as const : data.desired;
  return { ...createDraft(config), revision: calibrationEditRevision(data) };
}
export function chooseProfile(draft: CalibrationDraft, profile: string): CalibrationDraft {
  if (profile !== 'none' && profile !== 'custom' && profile !== 'local-19v-v1') return draft;
  return { ...draft, profile, configurationIssue: undefined };
}
function clearAc(draft: CalibrationDraft): CalibrationDraft {
  return { ...draft, values: { ...draft.values, base_gain: '', charge_gain: '' }, displayValues: { ...draft.displayValues, base_gain: '', charge_gain: '' }, sources: { ...draft.sources, base_gain: 'empty', charge_gain: 'empty' } };
}
export function chooseVoltage(draft: CalibrationDraft, voltage: CalibrationVoltage): CalibrationDraft {
  if (draft.voltage === voltage) return draft;
  return { ...clearAc(draft), schema: 2, profile: 'custom', voltage, voltageConfirmed: false };
}
export function confirmVoltage(draft: CalibrationDraft): CalibrationDraft {
  if (draft.voltage === null) return draft;
  return { ...(draft.schema === 1 ? clearAc(draft) : draft), schema: 2, profile: 'custom', voltageConfirmed: true };
}
export function editCoefficient(draft: CalibrationDraft, key: CoefficientKey, input: string): CalibrationDraft {
  let next = { ...draft, values: { ...draft.values, [key]: input }, displayValues: { ...draft.displayValues, [key]: input }, sources: { ...draft.sources, [key]: input.trim() ? 'manual' as const : 'empty' as const } };
  if (key === 'base_gain' && Number(input) !== Number(draft.values.base_gain) && draft.sources.charge_gain === 'assistant') {
    next = { ...next, values: { ...next.values, charge_gain: '' }, displayValues: { ...next.displayValues, charge_gain: '' }, sources: { ...next.sources, charge_gain: 'empty' } };
  }
  return next;
}
export type AssistantSuggestion = { voltage: CalibrationVoltage; base: number; charge: number | null; includeCharge: boolean };
export function applySuggestion(draft: CalibrationDraft, suggestion: AssistantSuggestion): CalibrationDraft {
  let next = draft.schema === 2 && draft.voltage === suggestion.voltage ? draft : clearAc(draft);
  if (!suggestion.includeCharge && next.sources.charge_gain === 'assistant' && Number(next.values.base_gain) !== suggestion.base) {
    next = { ...next, values: { ...next.values, charge_gain: '' }, displayValues: { ...next.displayValues, charge_gain: '' }, sources: { ...next.sources, charge_gain: 'empty' } };
  }
  next = { ...next, schema: 2, profile: 'custom', voltage: suggestion.voltage, voltageConfirmed: true };
  if (!next.values.base_gain || Number(next.values.base_gain) !== suggestion.base) next = { ...next,
    values: { ...next.values, base_gain: String(suggestion.base) }, displayValues: { ...next.displayValues, base_gain: formatCoefficient(suggestion.base) }, sources: { ...next.sources, base_gain: 'assistant' } };
  if (suggestion.includeCharge && (suggestion.charge === null ? !!next.values.charge_gain : !next.values.charge_gain || Number(next.values.charge_gain) !== suggestion.charge)) next = { ...next, values: { ...next.values, charge_gain: suggestion.charge === null ? '' : String(suggestion.charge) }, displayValues: { ...next.displayValues, charge_gain: suggestion.charge === null ? '' : formatCoefficient(suggestion.charge) }, sources: { ...next.sources, charge_gain: suggestion.charge === null ? 'empty' : 'assistant' } };
  return next;
}
export function presetDraft(draft: CalibrationDraft, defaults: CalibrationLegacyCoefficients, modern: boolean): CalibrationDraft {
  const config: CalibrationConfig = modern
    ? { schema: 2, profile: 'custom', ac_voltage_nominal_v: 19, coefficients: defaults, revision: draft.revision }
    : { schema: 1, profile: 'custom', coefficients: defaults, revision: draft.revision };
  return { ...createDraft(config), sources: { base_gain: 'preset', charge_gain: 'preset', battery_gain: 'preset' } };
}
export function coefficientSource(draft: CalibrationDraft, key: CoefficientKey): string {
  const source = draft.sources[key];
  return source === 'existing' ? key === 'battery_gain' ? '保留现有电池系数的完整精度' : `保留现有 ${draft.voltage} V 配置的完整精度`
    : source === 'legacy' ? '保留旧配置的完整精度'
    : source === 'assistant' ? '来自本次校准助手，待保存'
    : source === 'manual' ? '手动填写，待保存'
    : source === 'preset' ? '开发样机 19 V 预设值'
    : '未校准';
}
export type CalibrationPayload = { profile: CalibrationProfile; coefficients: CalibrationCoefficients | null; expected_revision: string; ac_voltage_nominal_v?: CalibrationVoltage };
export function calibrationPayload(draft: CalibrationDraft, supportsV2: boolean): { payload: CalibrationPayload | null; error: string | null; field?: CoefficientKey | 'voltage' } {
  if (!draft.revision) return { payload: null, error: '配置版本标识缺失，请重新载入后再保存。' };
  if (draft.profile === 'none' || draft.profile === 'local-19v-v1') return { payload: { profile: draft.profile, coefficients: null, expected_revision: draft.revision }, error: null };
  if (draft.profile !== 'custom') return { payload: null, error: '请先选择支持的校准方式；12 V 适配器使用自定义系数并选择 12 V。' };
  if (draft.schema === 2 && !supportsV2) return { payload: null, error: '当前采集器不支持电压档位配置，请先更新宿主机采集器。' };
  if (supportsV2 && (!draft.voltageConfirmed || draft.voltage === null)) return { payload: null, error: '请先确认适配器电压档位，再填写或采集交流系数。', field: 'voltage' };
  const coefficients: CalibrationCoefficients = { base_gain: 0, charge_gain: null, battery_gain: null };
  for (const key of keys) {
    const input = draft.values[key].trim();
    if (supportsV2 && key !== 'base_gain' && !input) continue;
    const value = Number(input);
    if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(input) || !Number.isFinite(value) || value > 10 || (key === 'charge_gain' ? value < 0 : value <= 0)) {
      return { payload: null, error: `${key === 'base_gain' ? '交流基底' : key === 'charge_gain' ? '回充补偿' : '电池放电'}系数需${key === 'charge_gain' ? '为 0 至 10' : '大于 0 且不超过 10'}${supportsV2 && key !== 'base_gain' ? '，未校准时可留空' : ''}。`, field: key };
    }
    coefficients[key] = value;
  }
  return { payload: { profile: 'custom', coefficients, expected_revision: draft.revision, ...(supportsV2 ? { ac_voltage_nominal_v: draft.voltage! } : {}) }, error: null };
}
