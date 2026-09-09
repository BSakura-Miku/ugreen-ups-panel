import type { CalibrationConfig, CalibrationLegacyCoefficients, CalibrationReadiness, CalibrationReportedConfig, CalibrationState, LiveView } from './types';

const isObject = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = (value: unknown, limit = 1000): string | null => typeof value === 'string' && value.trim() ? value.slice(0, limit) : null;
const gain = (value: unknown, zero = false): value is number => typeof value === 'number' && Number.isFinite(value) && value <= 10 && (zero ? value >= 0 : value > 0);

export function calibrationLabel(profile: unknown): string {
  return profile === 'custom' ? '自定义 · 未独立验证' : profile === 'local-19v-v1' ? '开发样机 · 19 V v1'
    : profile === 'none' ? '未启用' : text(profile, 128) ? `不支持的配置：${text(profile, 128)}` : '尚未报告校准配置';
}

export function parseCalibrationConfig(value: unknown): CalibrationReportedConfig {
  const config = isObject(value) ? value : {};
  const profile = text(config.profile, 128), revision = text(config.revision, 256) ?? '';
  const invalid = (reason: string): CalibrationReportedConfig => ({ schema: null, profile, revision, coefficients: null, invalid_reason: reason });
  if (!['none', 'local-19v-v1', 'custom'].includes(profile ?? '')) return invalid(profile ? `不支持的校准配置 ${profile}。请主动选择官方校准方式；12 V 适配器使用“自定义系数”并选择 12 V。` : '校准配置缺少配置名称。');
  if (!revision) return invalid('校准配置缺少有效版本标识，请重新载入或检查采集器。');
  if (config.schema === 1 && profile === 'none' && config.coefficients === null) return { schema: 1, profile, coefficients: null, revision };
  const coefficients = config.coefficients;
  if (!isObject(coefficients) || !gain(coefficients.base_gain)) return invalid('校准配置缺少有效的交流基底系数，功率估算暂停。');
  if (config.schema === 1 && (profile === 'custom' || profile === 'local-19v-v1')
    && gain(coefficients.charge_gain, true) && gain(coefficients.battery_gain)) {
    return { schema: 1, profile, revision, coefficients: { base_gain: coefficients.base_gain, charge_gain: coefficients.charge_gain, battery_gain: coefficients.battery_gain } };
  }
  if (config.schema === 2 && profile === 'custom' && (config.ac_voltage_nominal_v === 12 || config.ac_voltage_nominal_v === 19 || config.ac_voltage_nominal_v === 20)
    && (coefficients.charge_gain === null || gain(coefficients.charge_gain, true)) && (coefficients.battery_gain === null || gain(coefficients.battery_gain))) {
    return { schema: 2, profile, revision, ac_voltage_nominal_v: config.ac_voltage_nominal_v,
      coefficients: { base_gain: coefficients.base_gain, charge_gain: coefficients.charge_gain, battery_gain: coefficients.battery_gain } };
  }
  return invalid('校准配置的版本、电压档位或系数无效。请核对后重新配置，不能沿用这些系数。');
}

export function usableCalibrationConfig(config: CalibrationReportedConfig | null | undefined): config is CalibrationConfig {
  return !!config && config.schema !== null;
}

function parseReadiness(value: unknown): CalibrationReadiness | undefined {
  if (value === undefined) return undefined;
  if (!isObject(value) || typeof value.ready !== 'boolean' || !Array.isArray(value.issues)
    || value.issues.some(issue => !isObject(issue) || !text(issue.code, 100) || !text(issue.message))) {
    return { ready: false, can_save: false, code: 'invalid_readiness', message: '校准就绪状态响应无效，请重新载入。', issues: [] };
  }
  return { ready: value.ready, code: text(value.code, 100), message: text(value.message),
    issues: value.issues.slice(0, 20).map(issue => ({ code: text(issue.code, 100)!, message: text(issue.message)! })),
    ...(value.can_save !== undefined ? { can_save: value.can_save === true } : {}) };
}

export function parseCalibrationState(value: unknown): CalibrationState | null {
  if (!isObject(value) || value.schema !== 1 || typeof value.collector_ready !== 'boolean'
    || typeof value.pending !== 'boolean' || !isObject(value.defaults) || !Object.hasOwn(value, 'desired') || !Object.hasOwn(value, 'active')) return null;
  const defaults = value.defaults;
  if (!gain(defaults.base_gain) || !gain(defaults.charge_gain, true) || !gain(defaults.battery_gain)) return null;
  const problem = value.configuration_problem;
  const configurationProblem: CalibrationState['configuration_problem'] = isObject(problem) && ['desired', 'active', 'sample'].includes(String(problem.source)) && text(problem.code, 100)
    ? { source: problem.source as 'desired' | 'active' | 'sample', code: text(problem.code, 100)!, ...(text(problem.profile, 128) ? { profile: text(problem.profile, 128)! } : {}) } : null;
  if (problem !== undefined && problem !== null && !configurationProblem) return null;
  return { schema: 1, defaults: { base_gain: defaults.base_gain, charge_gain: defaults.charge_gain, battery_gain: defaults.battery_gain } satisfies CalibrationLegacyCoefficients,
    desired: parseCalibrationConfig(value.desired), active: value.active === null ? null : parseCalibrationConfig(value.active),
    collector_ready: value.collector_ready, pending: value.pending, error: text(value.error), readiness: parseReadiness(value.readiness),
    edit_revision: text(value.edit_revision, 256) ?? undefined, configuration_problem: configurationProblem,
    supported_config_schemas: Array.isArray(value.supported_config_schemas) ? value.supported_config_schemas.filter((schema): schema is number => schema === 1 || schema === 2) : undefined };
}

export function calibrationCanSave(data: CalibrationState | null): boolean {
  if (!data || !calibrationEditRevision(data)) return false;
  if (!usableCalibrationConfig(data.active) || data.configuration_problem?.source === 'active' || data.configuration_problem?.source === 'sample') return false;
  if (data.desired.schema === null && data.readiness?.can_save !== true) return false;
  return data.readiness?.can_save ?? (data.collector_ready && (data.readiness?.ready ?? true));
}

export function calibrationEditRevision(data: CalibrationState): string {
  return data.edit_revision ?? data.desired.revision;
}

export function calibrationReadinessMessage(data: CalibrationState | null): string {
  if (!data) return '正在读取采集器与校准配置。';
  if (data.readiness?.message) return data.readiness.message;
  if (data.readiness?.issues.length) return data.readiness.issues.map(issue => issue.message).join(' ');
  if (data.desired.schema === null) return data.desired.invalid_reason;
  if (data.active?.schema === null) return data.active.invalid_reason;
  if (data.error) return data.error;
  return data.collector_ready ? '' : '采集器尚未确认可读取校准文件。请在诊断页核对采集器版本、校准路径和连接状态；旧版接口未提供具体原因。';
}

export function calibrationEstimateIssue(view: LiveView | null): string | null {
  const validation = view?.calibration_validation;
  if (!validation || validation.valid !== false) return null;
  if (typeof validation.message === 'string' && validation.message.trim()) return validation.message;
  const messages: Record<string, string> = {
    missing_metadata: '采集器未提供完整校准信息。', unsupported_profile: '采集器使用了面板不支持的校准配置。',
    invalid_config: '校准配置或系数无效。', revision_mismatch: '采集值与校准配置的版本标识不一致。',
    config_mismatch: '采样与采集器报告的校准配置不一致。', invalid_estimate: '采样中的估算结果或来源信息无效。',
  };
  return typeof validation.reason === 'string' && Object.hasOwn(messages, validation.reason) ? messages[validation.reason] : '校准信息未通过校验，请在功率校准页查看原因。';
}
