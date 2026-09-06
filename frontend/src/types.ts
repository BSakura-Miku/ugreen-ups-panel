export type Point = {
  timestamp: number;
  first?: number;
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

export type History = { points: Point[]; resolution_sec: number };

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
