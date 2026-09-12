import type { DiagnosticView, RawObservationPoint } from './types';

export type ObservationStream = Omit<DiagnosticView['observation'], 'points'> & {
  cursor: string; reset: boolean; first_sequence: number;
  points: (RawObservationPoint & { sequence: number })[];
};

export function mergeObservation(previous: ObservationStream | null, next: ObservationStream): ObservationStream {
  if (next?.schema !== 1 || typeof next.cursor !== 'string' || typeof next.reset !== 'boolean'
    || !Number.isSafeInteger(next.first_sequence) || !Array.isArray(next.points) || next.points.length > 4096
    || next.points.some(point => !Number.isSafeInteger(point.sequence) || !Number.isFinite(point.timestamp))) {
    throw new Error('观测数据格式暂不兼容。');
  }
  const points = new Map<number, ObservationStream['points'][number]>();
  if (!next.reset) for (const point of previous?.points || []) if (point.sequence >= next.first_sequence) points.set(point.sequence, point);
  for (const point of next.points) points.set(point.sequence, point);
  return { ...next, points: [...points.values()].sort((a, b) => a.sequence - b.sequence).slice(-4096) };
}
