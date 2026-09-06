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

export type CalibrationProfile = 'none' | 'local-19v-v1' | 'custom';
export type CalibrationCoefficients = { base_gain: number; charge_gain: number; battery_gain: number };
export type CalibrationConfig = {
  schema: 1;
  profile: CalibrationProfile;
  coefficients: CalibrationCoefficients | null;
  revision: string;
};
export type CalibrationState = {
  schema: 1;
  defaults: CalibrationCoefficients;
  desired: CalibrationConfig;
  active: CalibrationConfig | null;
  collector_ready: boolean;
  pending: boolean;
  error: string | null;
};
