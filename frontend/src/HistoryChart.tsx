import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { History, Point } from './types';

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

const labels: Record<string, string> = {
  battery_energy_estimate_w: '电池放电功率 · 估算',
  ac_input_estimate_w: '交流输入功率 · 估算',
  battery_charge_power_candidate_w: '电池充电功率 · 估算',
  soc: '电量', adapter_input_voltage_v: '适配器输入电压',
  ups_output_voltage_v: 'UPS 输出电压', cell_delta_mv: '电芯压差',
  cell_1: '电芯 01', cell_2: '电芯 02', cell_3: '电芯 03', cell_4: '电芯 04',
};
const modeLabels: Record<string, string> = { online: '外部供电', charging: '电池充电', battery: '电池供电', unknown: '状态待确认' };
const isNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const firstSample = (point: Point) => isNumber(point.first) ? point.first : point.timestamp;
const lastSample = (point: Point) => isNumber(point.last) ? point.last : firstSample(point);
const pointPosition = (point: Point, history: History) => {
  // Boundary-day values remain whole-day statistics. Only their visual
  // anchor is clipped; the original interval and samples remain in the tip.
  let position = firstSample(point);
  if (isNumber(history.requested_start)) position = Math.max(position, history.requested_start);
  if (isNumber(history.requested_end)) position = Math.min(position, history.requested_end);
  return position;
};
const modelKey = (point: Point) => JSON.stringify([
  point.context?.calibration_profile, point.context?.ac_estimate_model,
  point.context?.battery_estimate_basis, point.context?.formula_version,
  point.context?.decoder_version, point.context?.provenance, point.context?.calibration_revision,
]);

type HistoryDatum = { value: [number, number | null]; point?: Point; key: string; statistic: 'mean' | 'peak' };
type HistoryLine = { key: string; name: string; statistic: 'mean' | 'peak'; color: string; data: HistoryDatum[] };

// Keep the backend's independent statistics intact: a delta peak is already
// the maximum observed per-sample delta, not a difference of cell extrema.
export function buildHistoryLines(history: History, metric: string): HistoryLine[] {
  const keys = metric === 'cells' ? ['cell_1', 'cell_2', 'cell_3', 'cell_4'] : [metric];
  const definitions: Omit<HistoryLine, 'data'>[] = metric === 'cell_delta_mv'
    ? [{ key: metric, name: '压差均值', statistic: 'mean', color: '#b6ee74' }, { key: metric, name: '压差峰值', statistic: 'peak', color: '#efb673' }]
    : keys.map((key, index) => ({ key, name: `${labels[key]} · 均值`, statistic: 'mean', color: ['#b6ee74', '#80bdff', '#d8a9f7', '#efb673'][index] }));
  const points = [...history.points].filter(point => isNumber(firstSample(point)))
    .sort((a, b) => firstSample(a) - firstSample(b));
  // A long aggregation window must not turn a known multi-minute outage
  // between two returned groups into a continuous line.
  const gapThreshold = Math.min(history.resolution_sec, 60) * 1.5;
  return definitions.map(definition => {
    const data: HistoryDatum[] = [];
    let previous: Point | undefined;
    for (const point of points) {
      const timestamp = pointPosition(point, history);
      if (previous && (firstSample(point) - lastSample(previous) > gapThreshold
          || point.mode !== previous.mode || modelKey(previous) !== modelKey(point))) {
        data.push({ value: [(pointPosition(previous, history) + timestamp) / 2 * 1000, null], key: definition.key, statistic: definition.statistic });
      }
      const value = (definition.statistic === 'peak' ? point.max : point.values)[definition.key];
      data.push({ value: [timestamp * 1000, isNumber(value) ? value : null], point, key: definition.key, statistic: definition.statistic });
      previous = point;
    }
    return { ...definition, data };
  });
}

function calendarTime(timestamp: number, utc: boolean, seconds = false) {
  return new Date(timestamp * 1000).toLocaleString('zh-CN', {
    ...(utc ? { timeZone: 'UTC' } : {}), year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}), hour12: false,
  });
}
function statisticValue(value: unknown, unit: string) {
  return isNumber(value) ? `${value.toFixed(unit === 'V' ? 3 : 1)} ${unit}` : '—';
}

export function historyTooltip(raw: unknown, history: History, unit: string) {
  const entries = (Array.isArray(raw) ? raw : [raw]) as { data?: HistoryDatum; seriesName?: string }[];
  const visible = entries.filter(entry => entry.data?.point && isNumber(entry.data.value[1]));
  const point = visible[0]?.data?.point;
  if (!point) return '';
  const daily = history.resolution_sec >= 86400;
  const start = point.bucket_start ?? point.timestamp;
  const end = point.bucket_end ?? start + history.resolution_sec;
  const lines = [
    `统计周期（${daily ? 'UTC' : '本地时间'}）`,
    calendarTime(start, daily, history.resolution_sec % 60 !== 0),
    `至 ${calendarTime(end, daily, history.resolution_sec % 60 !== 0)}（不含）`,
    `样本起（本地）：${isNumber(point.first) ? calendarTime(point.first, false, true) : '未记录'}`,
    `样本止（本地）：${isNumber(point.last) ? calendarTime(point.last, false, true) : '未记录'}`,
    `供电状态：${modeLabels[point.mode] || modeLabels.unknown}`,
  ];
  if (point.partial_range) lines.push('边界周期含所选时间外的记录');
  for (const entry of visible) {
    const datum = entry.data!;
    lines.push(`${entry.seriesName}：${statisticValue(datum.value[1], unit)}`);
    if (datum.statistic === 'mean') {
      const lo = datum.point!.min[datum.key], hi = datum.point!.max[datum.key];
      if (isNumber(lo) && isNumber(hi)) lines.push(`  范围：${statisticValue(lo, unit)} – ${statisticValue(hi, unit)}`);
    }
  }
  if (daily) lines.push('仅统计已采集样本，不代表全天连续记录');
  return lines.join('\n');
}

export default function HistoryChart({ history, metric, unit }: { history: History; metric: string; unit: string }) {
  const container = useRef<HTMLDivElement>(null);
  const instance = useRef<echarts.EChartsType | null>(null);

  useEffect(() => {
    if (!container.current) return;
    const chart = echarts.init(container.current);
    instance.current = chart;
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(container.current);
    return () => { observer.disconnect(); chart.dispose(); instance.current = null; };
  }, []);

  useEffect(() => {
    if (!instance.current) return;
    const daily = history.resolution_sec >= 86400;
    const lines = buildHistoryLines(history, metric);
    instance.current.setOption({
      backgroundColor: 'transparent', useUTC: daily,
      tooltip: {
        trigger: 'axis', renderMode: 'richText', backgroundColor: '#222b2b', borderColor: '#3b4744',
        textStyle: { color: '#eef3ed', fontSize: 12, lineHeight: 18 }, confine: true,
        formatter: (raw: unknown) => historyTooltip(raw, history, unit),
      },
      legend: { show: metric === 'cells' || metric === 'cell_delta_mv', top: 0, left: 'center', textStyle: { color: '#a9b5ac', fontSize: 12 }, itemWidth: 18, itemGap: 12 },
      grid: { top: metric === 'cells' ? 58 : 38, left: 46, right: 18, bottom: 35 },
      xAxis: {
        type: 'time', min: isNumber(history.requested_start) ? history.requested_start * 1000 : undefined,
        max: isNumber(history.requested_end) ? history.requested_end * 1000 : undefined,
        axisLine: { lineStyle: { color: '#343e3a' } },
        axisLabel: {
          color: '#a5b2ab', hideOverlap: true, fontSize: 12,
          ...(daily ? { formatter: (value: number) => new Date(value).toISOString().slice(0, 10) } : {}),
        }, splitLine: { show: false }, splitNumber: 4,
      },
      yAxis: {
        type: 'value', scale: metric === 'cells', max: metric === 'soc' ? 100 : undefined,
        axisLabel: { color: '#a5b2ab' },
        splitLine: { lineStyle: { color: '#2b3531', type: 'dashed' } },
      },
      series: lines.map(line => ({
        name: line.name, type: 'line', data: line.data, showSymbol: true, symbolSize: 4,
        connectNulls: false, smooth: false, itemStyle: { color: line.color },
        lineStyle: { width: line.statistic === 'peak' ? 1.75 : 2, type: line.statistic === 'peak' ? 'dashed' : 'solid', color: line.color },
        areaStyle: lines.length === 1 ? { opacity: .07, color: line.color } : undefined,
      })), animation: false,
    }, { notMerge: true, lazyUpdate: true });
  }, [history, metric, unit]);

  const values = history.points.flatMap(point => metric === 'cells'
    ? ['cell_1', 'cell_2', 'cell_3', 'cell_4'].map(key => point.values[key])
    : metric === 'cell_delta_mv' ? [point.values[metric], point.max[metric]] : [point.values[metric]])
    .filter(isNumber);
  const digits = unit === 'V' ? 3 : 1;
  const description = `${metric === 'cells' ? '四节电芯电压均值' : metric === 'cell_delta_mv' ? '电芯压差均值与峰值' : labels[metric]}历史趋势。${values.length
    ? `记录范围 ${Math.min(...values).toFixed(digits)} 至 ${Math.max(...values).toFixed(digits)} ${unit}。断档与模式或校准变更处不连线。详细数据可使用导出 CSV。` : '暂无记录。'}`;
  return <div className="chart" ref={container} role="img" aria-label={description} />;
}
