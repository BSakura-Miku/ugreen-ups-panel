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
  end_reason: 'external' | 'offline' | 'gap' | 'unknown' | 'context_changed' | null;
  sample_count: number;
  status: 'complete' | 'incomplete' | 'ongoing';
  observed_duration_sec: number;
  duration_sec: number | null;
  soc_drop_pp: number | null;
  overlaps_boundary: boolean;
  energy?: BatterySessionEnergy | null;
};

export type BatteryEnergyBasis = {
  profile: string | null;
  revision: string | null;
  battery_gain: number | null;
  estimate_basis: string | null;
  source: string | null;
  device: Record<string, unknown> | null;
  decoder_version: number | string | null;
  formula_version: number | string | null;
};
export type BatterySessionEnergy = {
  schema: 1;
  estimate_wh: number | null;
  covered_duration_sec: number;
  observed_duration_sec: number;
  coverage_ratio: number | null;
  interval_count: number;
  status: 'available' | 'partial' | 'unavailable';
  reasons: string[];
  basis: BatteryEnergyBasis | null;
  start_soc: number | null;
  end_soc: number | null;
};

export type CellBalanceLevel = 'good' | 'minor' | 'elevated' | 'check';
export type CellBalanceReport = {
  schema: 1;
  state: 'unavailable' | 'charging' | 'discharging' | 'settling' | 'observing' | 'assessed';
  level: CellBalanceLevel | null;
  candidate_level: CellBalanceLevel | null;
  delta_mv: number | null;
  standby_duration_sec: number;
  required_standby_sec: number;
  persistence_sec: number;
  candidate_duration_sec: number;
  lowest_cells: number[];
  frequent_lowest_cell: number | null;
  recent_max_delta_mv: number | null;
  recent_window_sec: number;
  recent_sample_count: number;
  sample_timestamp: number | null;
  observed: boolean;
  reason: 'no_fresh_sample' | 'invalid_sample' | 'not_observed' | 'waiting_standby' | 'confirming_level' | null;
  thresholds: { good_below_mv: number; minor_below_mv: number; elevated_below_mv: number };
  reference_only: true;
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
export type CalibrationReportedConfig = CalibrationConfig | {
  schema: null;
  profile: string | null;
  coefficients: null;
  revision: string;
  invalid_reason: string;
};
export type CalibrationReadiness = {
  ready: boolean;
  code: string | null;
  message: string | null;
  issues: { code: string; message: string }[];
  can_save?: boolean;
};
export type CalibrationState = {
  schema: 1;
  defaults: CalibrationLegacyCoefficients;
  supported_config_schemas?: number[];
  desired: CalibrationReportedConfig;
  active: CalibrationReportedConfig | null;
  collector_ready: boolean;
  readiness?: CalibrationReadiness;
  edit_revision?: string;
  configuration_problem?: { source: 'desired' | 'active' | 'sample'; code: string; profile?: string } | null;
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
  cell_balance?: CellBalanceReport | null;
  calibration_validation?: { valid: boolean; reason: string | null; profile?: string; message?: string | null };
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

export type DiagnosticBuild = { version: string | null; revision: string | null; source_sha256?: string | null };
export type DiagnosticCheck = {
  id: string;
  label: string;
  status: 'ok' | 'waiting' | 'warning' | 'unknown' | 'error';
  detail: string;
};
export type RawObservationPoint = {
  timestamp: number;
  segment: number;
  device_alias: string;
  source: 'usbmon' | 'replay';
  mode: 'online' | 'charging' | 'battery' | 'unknown';
  raw_status: number | null;
  byte_26: number;
  byte_27: number;
  byte_28: number | null;
  soc: number | null;
  battery_voltage: number | null;
  adapter_input_voltage_v: number | null;
  ups_output_voltage_v: number | null;
  current: number | null;
  battery_charge_current_candidate_a: number | null;
  battery_discharge_current_candidate_a: number | null;
  decoder_version: number | null;
  formula_version: number | null;
  calibration_revision: string | null;
  calibration_profile: string | null;
};
export type DiagnosticView = {
  schema: 1;
  generated_at: number;
  capture_fresh: boolean;
  versions: {
    panel: DiagnosticBuild;
    collector: DiagnosticBuild | null;
    nut_driver: string | null;
    nut_version: string | null;
    nut_subdriver: string | null;
    ups_firmware: string | null;
    usb_device_version: string | null;
    decoder_version: number | null;
  };
  connection: {
    checks: DiagnosticCheck[];
    usb_age_sec: number | null;
    nut_query_age_sec: number | null;
    pollonly: boolean | null;
    counters: Record<string, number>;
    association: 'matched' | 'different' | 'unverified';
  };
  system: {
    available: boolean;
    fresh: boolean;
    status_raw: string | null;
    status_tokens: string[];
    notices: { code: string; label: string; level: 'info' | 'warning' }[];
    alarm_text: string | null;
    thresholds: { charge_low: number | null; runtime_low_sec: number | null };
    runtime: { raw: string | null; seconds: number | null; quality: 'unavailable' | 'unverified' | 'sentinel' | 'stale' | 'invalid'; label: string };
    values: Record<string, string>;
  };
  observation: {
    schema: 1;
    window_sec: number;
    max_samples: number;
    count: number;
    points: RawObservationPoint[];
    started_at: number | null;
    first_timestamp: number | null;
    last_timestamp: number | null;
    truncated: boolean;
    gap_count: number;
    conflict_count: number;
    rejected_count: number;
    latest: {
      fresh: boolean;
      observed: boolean;
      timestamp: number | null;
      byte_26: number | null;
      byte_27: number | null;
      byte_28: number | null;
      source: string | null;
      mode: string | null;
      device_alias: string | null;
    };
  };
};

export type CollectorUpdateAvailability = 'ready' | 'not_installed' | 'unreachable' | 'incompatible';
export type CollectorUpdateStage = 'checking' | 'downloading' | 'verifying' | 'installing' | 'restarting' | 'validating'
  | 'rolling_back' | 'succeeded' | 'failed' | 'interrupted';
export type CollectorUpdateStatus = {
  schema: 1;
  installed: boolean;
  availability: CollectorUpdateAvailability;
  updater_version: string | null;
  updater_schema: 1;
  current: { version: string; revision: string | null; source_sha256: string | null } | null;
  runtime?: DiagnosticBuild | null;
  source_status?: 'verified' | 'unverified' | 'modified' | 'unreadable' | 'unknown';
  source_error?: { code: string; message: string } | null;
  preflight?: { ready: boolean; code: string | null; target_verified: boolean };
  latest: {
    version: string;
    tag: string;
    release_id: number;
    sha256: string;
    size: number;
    published_at: string | null;
    notes: string;
    url: string;
  } | null;
  checked_at: number | null;
  update_available: boolean;
  auth_required: true;
  rollback: {
    available: boolean;
    version: string | null;
    reason: 'available' | 'no_previous' | 'legacy_backup' | 'incompatible' | 'unknown';
  };
  operation: {
    id: string;
    action: 'check' | 'install' | 'rollback';
    stage: CollectorUpdateStage;
    busy: boolean;
    started_at: number;
    updated_at: number;
    finished_at: number | null;
    from_version: string | null;
    to_version: string | null;
    outcome: 'updated' | 'rolled_back' | 'restored' | 'manual_required' | 'checked' | null;
    error: { code: string; message: string } | null;
  } | null;
};
