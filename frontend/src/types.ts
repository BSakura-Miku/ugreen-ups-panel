export type Point = {
  timestamp: number;
  first?: number;
  mode: string;
  context?: {
    calibration_profile?: string;
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
