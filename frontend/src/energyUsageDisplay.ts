export type EnergyDayStatus = 'future' | 'no_data' | 'partial' | 'complete' | 'today';
export type EnergyDaySummary = {
  date: string;
  status: EnergyDayStatus;
  estimate_kwh: number | null;
  covered_sec: number;
  expected_sec: number;
  coverage_ratio: number | null;
  average_power_w: number | null;
  basis_count: number;
};
export type EnergyMonthSummary = {
  estimate_kwh: number | null;
  covered_sec: number;
  expected_sec: number;
  coverage_ratio: number | null;
  complete_days: number;
  complete_day_average_kwh: number | null;
  recorded_days: number;
  basis_count: number;
};
export type EnergyHour = {
  hour: number;
  start_ts: number;
  end_ts: number;
  estimate_kwh: number | null;
  covered_sec: number;
  expected_sec: number;
  coverage_ratio: number | null;
  average_power_w: number | null;
  basis_count: number;
};
type EnergyMetadata = {
  schema: 1;
  timezone: string;
  utc_offset: string;
  generated_at: number;
  current_date: string;
  capture_fresh: boolean;
  storage_error?: string | null;
  dropped_intervals?: number;
};
export type EnergyUsageMonth = EnergyMetadata & {
  month: string;
  tracking_started_at: number | null;
  first_month: string | null;
  last_recorded_at: number | null;
  reason: string | null;
  today: EnergyDaySummary;
  summary: EnergyMonthSummary;
  days: EnergyDaySummary[];
};
export type EnergyUsageDay = EnergyMetadata & {
  date: string;
  day: EnergyDaySummary;
  hours: EnergyHour[];
  bases: { id: string; profile: string | null; revision: string | null; estimate_kwh: number; covered_sec: number; source: string | null; ac_model: string | null }[];
};

const object = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
export const energyFinite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const nonnegative = (value: unknown): value is number => energyFinite(value) && value >= 0;
const count = (value: unknown): value is number => nonnegative(value) && Number.isInteger(value);
const nullableNumber = (value: unknown) => value === null || nonnegative(value);
const nullableText = (value: unknown) => value === null || typeof value === 'string';

export function validEnergyMonth(value: unknown): value is string {
  return typeof value === 'string' && /^(?:19[7-9]\d|[2-9]\d{3})-(?:0[1-9]|1[0-2])$/.test(value) && Number(value.slice(0, 4)) <= 9998;
}

export function energyMonthDays(month: string): number {
  if (!validEnergyMonth(month)) return 0;
  const [year, monthNumber] = month.split('-').map(Number);
  return new Date(Date.UTC(year, monthNumber, 0)).getUTCDate();
}

export function validEnergyDate(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const day = Number(value.slice(8));
  return validEnergyMonth(value.slice(0, 7)) && day >= 1 && day <= energyMonthDays(value.slice(0, 7));
}

export function energyLocalDate(now = new Date()): string {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(now);
  const part = (type: string) => parts.find(value => value.type === type)?.value ?? '';
  return `${part('year')}-${part('month')}-${part('day')}`;
}

export function shiftEnergyMonth(month: string, offset: number): string | null {
  if (!validEnergyMonth(month) || !Number.isInteger(offset)) return null;
  const [year, monthNumber] = month.split('-').map(Number);
  const date = new Date(Date.UTC(year, monthNumber - 1 + offset, 1));
  const result = `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, '0')}`;
  return validEnergyMonth(result) ? result : null;
}

export function energyMonthLabel(month: string): string {
  return validEnergyMonth(month) ? `${Number(month.slice(0, 4))} 年 ${Number(month.slice(5))} 月` : '读取月份';
}

export function energyDateLabel(date: string): string {
  return validEnergyDate(date) ? `${Number(date.slice(5, 7))} 月 ${Number(date.slice(8))} 日` : '当日';
}

export function energyCalendarCells(month: string): (string | null)[] {
  const days = energyMonthDays(month);
  if (!days) return [];
  const weekday = new Date(`${month}-01T00:00:00Z`).getUTCDay();
  const leading = (weekday + 6) % 7;
  const cells: (string | null)[] = Array.from({ length: leading }, () => null);
  for (let day = 1; day <= days; day++) cells.push(`${month}-${String(day).padStart(2, '0')}`);
  while (cells.length % 7) cells.push(null);
  return cells;
}

// Arrow keys stay inside the visible month and never focus a future day.
export function energyCalendarFocus(date: string, key: string, month: string, currentDate: string): string | null {
  if (!validEnergyDate(date) || date.slice(0, 7) !== month || !validEnergyDate(currentDate)) return null;
  const day = Number(date.slice(8));
  const weekday = (new Date(`${date}T00:00:00Z`).getUTCDay() + 6) % 7;
  const offset = ({ ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -weekday, End: 6 - weekday } as Record<string, number>)[key];
  if (typeof offset !== 'number') return null;
  const lastDay = month === currentDate.slice(0, 7) ? Number(currentDate.slice(8)) : energyMonthDays(month);
  const target = Math.min(lastDay, Math.max(1, day + offset));
  const result = `${month}-${String(target).padStart(2, '0')}`;
  return result <= currentDate ? result : null;
}

function validInterval(value: Record<string, unknown>): boolean {
  if (!nonnegative(value.covered_sec) || !nonnegative(value.expected_sec) || value.covered_sec > value.expected_sec + .01
    || !nullableNumber(value.estimate_kwh) || !count(value.basis_count)
    || !(value.coverage_ratio === null || nonnegative(value.coverage_ratio) && value.coverage_ratio <= 1)) return false;
  return value.covered_sec === 0 ? value.estimate_kwh === null : nonnegative(value.estimate_kwh);
}

function validDay(value: unknown, currentDate: string): value is EnergyDaySummary {
  if (!object(value) || !validEnergyDate(value.date) || !validInterval(value) || !nullableNumber(value.average_power_w)) return false;
  if (value.date > currentDate) return value.status === 'future' && value.covered_sec === 0 && value.expected_sec === 0;
  if (value.status === 'no_data') return value.covered_sec === 0 && value.average_power_w === null;
  if (!nonnegative(value.average_power_w) || value.covered_sec === 0) return false;
  return value.date === currentDate ? value.status === 'today' : value.status === 'partial' || value.status === 'complete';
}

function validMetadata(value: unknown): value is Record<string, unknown> & EnergyMetadata {
  return object(value) && value.schema === 1 && typeof value.timezone === 'string' && value.timezone.length > 0 && value.timezone.length <= 80
    && typeof value.utc_offset === 'string' && /^[+-](?:0\d|1\d):[0-5]\d$/.test(value.utc_offset)
    && energyFinite(value.generated_at) && value.generated_at > 0 && validEnergyDate(value.current_date)
    && typeof value.capture_fresh === 'boolean'
    && (value.storage_error === undefined || nullableText(value.storage_error))
    && (value.dropped_intervals === undefined || count(value.dropped_intervals));
}

export function parseEnergyUsageMonth(value: unknown): EnergyUsageMonth | null {
  if (!validMetadata(value) || !validEnergyMonth(value.month) || !nullableNumber(value.tracking_started_at)
    || !nullableNumber(value.last_recorded_at) || !nullableText(value.reason)
    || !(value.first_month === null || validEnergyMonth(value.first_month) && value.first_month <= value.current_date.slice(0, 7))
    || !validDay(value.today, value.current_date) || value.today.date !== value.current_date
    || !object(value.summary) || !validInterval(value.summary)
    || !count(value.summary.complete_days) || !count(value.summary.recorded_days)
    || value.summary.complete_days > value.summary.recorded_days || value.summary.recorded_days > energyMonthDays(value.month)
    || !(value.summary.complete_days === 0 ? value.summary.complete_day_average_kwh === null : nonnegative(value.summary.complete_day_average_kwh))
    || !Array.isArray(value.days) || value.days.length !== energyMonthDays(value.month)) return null;
  const dates = new Set<string>();
  for (const day of value.days) {
    if (!validDay(day, value.current_date) || day.date.slice(0, 7) !== value.month || dates.has(day.date)) return null;
    dates.add(day.date);
  }
  return value as EnergyUsageMonth;
}

export function parseEnergyUsageDay(value: unknown): EnergyUsageDay | null {
  if (!validMetadata(value) || !validEnergyDate(value.date) || !validDay(value.day, value.current_date) || value.day.date !== value.date
    || !Array.isArray(value.hours) || value.hours.length !== 24 || !Array.isArray(value.bases)) return null;
  const hours = new Set<number>();
  for (const hour of value.hours) {
    if (!object(hour) || !count(hour.hour) || hour.hour > 23 || hours.has(hour.hour)
      || !energyFinite(hour.start_ts) || !energyFinite(hour.end_ts) || hour.end_ts <= hour.start_ts
      || !validInterval(hour) || !nullableNumber(hour.average_power_w)
      || (hour.covered_sec === 0 ? hour.average_power_w !== null : !nonnegative(hour.average_power_w))) return null;
    hours.add(hour.hour);
  }
  for (const basis of value.bases) {
    if (!object(basis) || typeof basis.id !== 'string' || !basis.id || !nullableText(basis.profile) || !nullableText(basis.revision)
      || !nonnegative(basis.estimate_kwh) || !nonnegative(basis.covered_sec) || !nullableText(basis.source) || !nullableText(basis.ac_model)) return null;
  }
  return value as EnergyUsageDay;
}

export function energyKwh(value: unknown, digits = 3): string {
  if (!nonnegative(value)) return '—';
  if (value > 0 && value < 10 ** -digits) return `<${(10 ** -digits).toFixed(digits)}`;
  return value.toFixed(digits);
}

export function energyPower(value: unknown): string {
  return nonnegative(value) ? value.toFixed(1) : '—';
}

export function energyCoverage(value: unknown): string {
  if (!nonnegative(value) || value > 1) return '—';
  if (value === 1) return '100%';
  if (value > .999) return '>99.9%';
  if (value > 0 && value < .001) return '<0.1%';
  return `${(Math.floor(value * 1000) / 10).toFixed(1).replace(/\.0$/, '')}%`;
}

export function energyDuration(value: unknown): string {
  if (!nonnegative(value)) return '—';
  const seconds = Math.floor(value);
  if (value > 0 && seconds === 0) return '<1秒';
  if (seconds < 60) return `${seconds}秒`;
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60), remainder = seconds % 60;
  return `${hours ? `${hours}小时` : ''}${minutes ? `${minutes}分` : ''}${remainder ? `${remainder}秒` : ''}`;
}

export function energyTimestamp(value: unknown, timezone = 'Asia/Shanghai'): string {
  if (!energyFinite(value) || value <= 0) return '—';
  try {
    return new Date(value * 1000).toLocaleString('zh-CN', { timeZone: timezone, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
  } catch { return '—'; }
}

export function energyDayState(day: EnergyDaySummary | null | undefined, currentDate: string): string {
  if (!day) return '暂无记录';
  if (day.date === currentDate) return day.status === 'no_data' ? '今天 · 暂无记录' : '今天 · 截至当前';
  return ({ future: '尚未到来', no_data: '暂无记录', partial: '部分记录', complete: '完整记录', today: '截至当前' } as const)[day.status];
}

export function energyHeatLevel(day: EnergyDaySummary | null | undefined, maximum: number): number {
  if (!day || day.status === 'future' || day.status === 'no_data' || !nonnegative(day.estimate_kwh)) return 0;
  if (day.estimate_kwh === 0 || !energyFinite(maximum) || maximum <= 0) return 1;
  return Math.min(5, Math.max(1, Math.ceil(day.estimate_kwh / maximum * 5)));
}

export function energyUsageNotice(report: EnergyUsageMonth | null): string | null {
  if (!report) return null;
  if (report.storage_error) return '记录暂时异常，以下为已记录累计；缺失时段未补算。';
  if ((report.dropped_intervals ?? 0) > 0) return '采集曾丢弃部分区间，以下为已记录累计；历史缺口未补算。';
  if (report.reason === 'not_configured') return '配置市电功率估算后开始记录用电量。';
  if (report.reason === 'charge_not_configured') return '回充尚未校准，充电时段暂不能累计。';
  if (report.reason === 'invalid_basis') return '市电功率估算尚未就绪，等待有效记录。';
  if (!report.capture_fresh) return '采集数据已离线，保留上次已记录累计。';
  return null;
}

async function readEnergyReport<T>(url: string, signal: AbortSignal, parse: (value: unknown) => T | null): Promise<T> {
  if (signal.aborted) throw new DOMException('Read aborted', 'AbortError');
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  const timeout = setTimeout(abort, 8000);
  try {
    const response = await fetch(url, { signal: controller.signal, cache: 'no-store', mode: 'same-origin', redirect: 'error' });
    if (!response.ok) throw new Error('energy_usage_unavailable');
    const parsed = parse(await response.json());
    if (controller.signal.aborted) throw new DOMException('Read aborted', 'AbortError');
    if (!parsed) throw new Error('invalid_energy_usage');
    return parsed;
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener('abort', abort);
  }
}

export async function readEnergyUsageMonth(month: string | null, signal: AbortSignal): Promise<EnergyUsageMonth> {
  if (month !== null && !validEnergyMonth(month)) throw new Error('invalid_energy_month');
  const report = await readEnergyReport(`/api/energy-usage${month ? `?month=${month}` : ''}`, signal, parseEnergyUsageMonth);
  if (month !== null && report.month !== month) throw new Error('energy_month_mismatch');
  return report;
}

export async function readEnergyUsageDay(date: string, signal: AbortSignal): Promise<EnergyUsageDay> {
  if (!validEnergyDate(date)) throw new Error('invalid_energy_date');
  const report = await readEnergyReport(`/api/energy-usage/day?date=${date}`, signal, parseEnergyUsageDay);
  if (report.date !== date) throw new Error('energy_date_mismatch');
  return report;
}
