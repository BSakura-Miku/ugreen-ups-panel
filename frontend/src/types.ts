export type Point = {
  timestamp: number;
  first?: number;
  last?: number;
  bucket_start?: number;
  bucket_end?: number;
  partial_range?: boolean;
  mode: string;
  context?: {
    calibration_profile?: string;
    calibration_revision?: string;
    calibration_coefficients?: CalibrationCoefficients;
    ac_estimate_model?: string;
    battery_estimate_basis?: string;
    formula_version?: number;
    decoder_version?: number;
    provenance?: string;
  };
  values: Record<string, number>;
  min: Record<string, number>;
  max: Record<string, number>;
};

export type History = {
  points: Point[];
  resolution_sec: number;
  requested_start?: number | null;
  requested_end?: number | null;
  available_start?: number | null;
  available_end?: number | null;
};

export type BatterySession = {
  id: string;
  start_ts: number;
  end_ts: number | null;
  last_ts: number;
  start_soc: number | null;
  end_soc: number | null;
  start_known: boolean;
  end_reason: 'external' | 'offline' | 'gap' | 'unknown' | null;
  sample_count: number;
  status: 'complete' | 'incomplete' | 'ongoing';
  observed_duration_sec: number;
  duration_sec: number | null;
  soc_drop_pp: number | null;
  overlaps_boundary: boolean;
};

export type BatterySessions = {
  schema: 1;
  requested_start: number;
  requested_end: number;
  recording_since: number | null;
  capture_fresh: boolean;
  summary: {
    confirmed_starts: number;
    complete_count: number;
    incomplete_count: number;
    ongoing_count: number;
    observed_duration_sec: number;
    complete_duration_sec: number;
    soc_drop_pp: number | null;
    soc_records: number;
  };
  records: BatterySession[];
  total_records: number;
  has_more: boolean;
};

export type CalibrationVoltage = 12 | 19 | 20;
export type CalibrationProfile = 'none' | 'local-19v-v1' | 'custom';
export type CalibrationLegacyCoefficients = { base_gain: number; charge_gain: number; battery_gain: number };
export type CalibrationCoefficients = { base_gain: number; charge_gain: number | null; battery_gain: number | null };
export type CalibrationConfig = {
  schema: 1;
  profile: CalibrationProfile;
  coefficients: CalibrationLegacyCoefficients | null;
  revision: string;
} | {
  schema: 2;
  profile: 'custom';
  ac_voltage_nominal_v: CalibrationVoltage;
  coefficients: CalibrationCoefficients;
  revision: string;
};
export type CalibrationState = {
  schema: 1;
  defaults: CalibrationLegacyCoefficients;
  supported_config_schemas?: number[];
  desired: CalibrationConfig;
  active: CalibrationConfig | null;
  collector_ready: boolean;
  pending: boolean;
  error: string | null;
};

export type Sample = {
  calibration_profile?: string;
  calibration_schema?: number;
  calibration_revision?: string;
  calibration_coefficients?: CalibrationCoefficients | null;
  ac_voltage_nominal_v?: CalibrationVoltage;
  decoder_version?: number;
  formula_version?: number;
  adapter_input_voltage_v?: number | null;
  battery_energy_estimate_w?: number | null;
  battery_estimate_quality?: string;
  ac_input_estimate_w?: number | null;
  ac_estimate_quality?: string;
  battery_charge_current_candidate_a?: number | null;
  battery_discharge_current_candidate_a?: number | null;
  battery_charge_power_candidate_w?: number | null;
  battery_discharge_power_candidate_w?: number | null;
  timestamp: number;
  mode: string;
  soc: number;
  input_voltage: number | null;
  output_voltage: number | null;
  current: number | null;
  current_kind: string;
  power_w: number | null;
  dc_power_estimate_w?: number | null;
  battery_voltage: number;
  cells: number[];
  cell_delta_mv: number;
  runtime_sec: number | null;
  load_percent: number | null;
  warnings: string[];
  raw_fields?: { be_u16: Record<string, number>; byte_26?: number; byte_27?: number; byte_28: number; frame_hex: string };
};
export type LiveView = {
  calibration?: { config: CalibrationConfig | null; configurable: boolean; error: string | null };
  fresh: boolean;
  source: string;
  age_sec: number | null;
  sample: Sample | null;
  device?: { serial?: string; bus?: number; device?: number; address?: number; vendor?: string; product?: string; path?: string } | null;
  nut?: { fresh: boolean; values?: Record<string, string> };
  diagnostics?: { frames?: number; dropped?: number; rejected?: number; error?: string };
  storage_error?: string | null;
};
