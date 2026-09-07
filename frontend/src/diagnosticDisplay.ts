const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);

export function rawChannelValue(value: unknown): number | null {
  return finite(value) && Number.isInteger(value) && value >= 0 && value <= 255 ? value : null;
}

export function rawChannelLabel(value: unknown, fresh: boolean): string {
  const valid = fresh ? rawChannelValue(value) : null;
  return valid === null ? '—' : String(valid);
}

export function diagnosticTime(value: unknown, date = false): string {
  if (!finite(value) || value < 0 || !Number.isFinite(new Date(value * 1000).getTime())) return '—';
  return new Date(value * 1000).toLocaleString('zh-CN', {
    ...(date ? { month: '2-digit', day: '2-digit' } : {}),
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  });
}

export function diagnosticCount(value: unknown): string {
  return finite(value) && value >= 0 && Number.isInteger(value) ? String(value) : '—';
}

export function diagnosticVersion(value: unknown): string {
  return typeof value === 'string' && value.trim() ? value.trim() : '未知';
}

export function runtimeObservation(raw: unknown): string {
  if (raw === '-1' || raw === -1 || raw === '65535' || raw === 65535 || raw === '4294967295' || raw === 4294967295) {
    return '该原值疑似无效占位值，不作为可用续航。';
  }
  return raw === null || raw === undefined || raw === ''
    ? '系统未提供续航原值。'
    : '系统上报原值尚未独立验证，不作为可靠续航预测。';
}

export type TracePoint = {
  timestamp: number;
  mode: string;
  channelA: unknown;
  channelB: unknown;
  segment: string;
  breakBefore?: boolean;
};

export type TracePath = { channel: 'A' | 'B'; path: string };

// Accept normalized drawing points, rather than assuming an API response shape.
// Keep device timestamps and draw only observed spans: no smoothing or gap fill.
export function buildRawTrace(points: TracePoint[], width = 640, height = 210) {
  const validTimes = points.filter(point => finite(point.timestamp) && point.timestamp >= 0);
  const values = validTimes.flatMap(point => [rawChannelValue(point.channelA), rawChannelValue(point.channelB)])
    .filter((value): value is number => value !== null);
  if (!validTimes.length || !values.length) return null;
  const first = Math.min(...validTimes.map(point => point.timestamp));
  const last = Math.max(...validTimes.map(point => point.timestamp));
  const low = Math.max(0, Math.min(...values) - 1), high = Math.min(255, Math.max(...values) + 1);
  const left = 38, right = width - 15, top = 15, bottom = height - 30;
  const x = (timestamp: number) => first === last ? (left + right) / 2 : left + (timestamp - first) / (last - first) * (right - left);
  const y = (value: number) => bottom - (value - low) / Math.max(1, high - low) * (bottom - top);
  const paths: TracePath[] = [];
  const markers: { timestamp: number; mode: string; x: number }[] = [];
  let priorMode: string | null = null;
  for (const point of validTimes) {
    if (priorMode !== null && point.mode !== priorMode) markers.push({ timestamp: point.timestamp, mode: point.mode, x: x(point.timestamp) });
    priorMode = point.mode;
  }
  const dots: { channel: 'A' | 'B'; x: number; y: number; value: number; timestamp: number }[] = [];
  for (const channel of ['A', 'B'] as const) {
    let previous: TracePoint | null = null;
    let path = '';
    let lastDot: typeof dots[number] | null = null;
    const flush = () => { if (path) paths.push({ channel, path }); path = ''; };
    for (const point of points) {
      const value = rawChannelValue(channel === 'A' ? point.channelA : point.channelB);
      if (!finite(point.timestamp) || point.timestamp < 0 || value === null) { flush(); previous = null; continue; }
      if (previous && point.timestamp === previous.timestamp && point.mode === previous.mode && point.segment === previous.segment
        && point.channelA === previous.channelA && point.channelB === previous.channelB && !point.breakBefore) continue;
      const interrupted = !previous || point.breakBefore || point.timestamp <= previous.timestamp
        || point.timestamp - previous.timestamp > 5 || point.mode !== previous.mode || point.segment !== previous.segment;
      if (interrupted) { flush(); path = `M ${x(point.timestamp)} ${y(value)}`; }
      else path += ` H ${x(point.timestamp)} V ${y(value)}`;
      lastDot = { channel, x: x(point.timestamp), y: y(value), value, timestamp: point.timestamp };
      previous = point;
    }
    flush();
    if (lastDot) dots.push(lastDot);
  }
  return { paths, markers, dots, first, last, low, high, left, right, top, bottom, width, height };
}
