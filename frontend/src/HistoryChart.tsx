import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { History, Point } from './types';

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer]);

const labels: Record<string, string> = {
  battery_energy_estimate_w: '电池放电功率 · 容量估算',
  ac_input_estimate_w: '交流输入功率 · 估算',
  battery_charge_power_candidate_w: '电池充电功率 · 估算',
  soc: '电量', adapter_input_voltage_v: '适配器输入电压',
  ups_output_voltage_v: 'UPS 输出电压', cell_delta_mv: '电芯压差',
  cell_1: '电芯 01', cell_2: '电芯 02', cell_3: '电芯 03', cell_4: '电芯 04',
};

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
    const keys = metric === 'cells' ? ['cell_1', 'cell_2', 'cell_3', 'cell_4'] : [metric];
    const points = [...history.points].sort((a, b) => (a.first ?? a.timestamp) - (b.first ?? b.timestamp));
    const modelKey = (point: Point) => JSON.stringify([
      point.context?.calibration_profile, point.context?.ac_estimate_model,
      point.context?.battery_estimate_basis, point.context?.formula_version,
      point.context?.decoder_version, point.context?.provenance,
    ]);
    instance.current.setOption({
      backgroundColor: 'transparent', color: ['#b6ee74', '#80bdff', '#d8a9f7', '#efb673'],
      tooltip: {
        trigger: 'axis', backgroundColor: '#222b2b', borderColor: '#3b4744',
        textStyle: { color: '#eef3ed' }, confine: true,
        valueFormatter: (value: unknown) => typeof value === 'number' && Number.isFinite(value)
          ? `${value.toFixed(unit === 'V' ? 3 : unit === '%' || unit === 'mV' ? 0 : 1)} ${unit}` : '—',
      },
      legend: { show: metric === 'cells', textStyle: { color: '#a9b5ac' } },
      grid: { top: 38, left: 48, right: 18, bottom: 35 },
      xAxis: {
        type: 'time', axisLine: { lineStyle: { color: '#343e3a' } },
        axisLabel: { color: '#a5b2ab', hideOverlap: true }, splitLine: { show: false },
      },
      yAxis: {
        type: 'value', scale: metric === 'cells', max: metric === 'soc' ? 100 : undefined,
        axisLabel: { color: '#a5b2ab' },
        splitLine: { lineStyle: { color: '#2b3531', type: 'dashed' } },
      },
      series: keys.map(key => {
        const data: [number, number | null][] = [];
        let previous: Point | undefined;
        points.forEach(point => {
          const timestamp = point.first ?? point.timestamp;
          if (previous && (timestamp - (previous.first ?? previous.timestamp) > history.resolution_sec * 1.5
              || point.mode !== previous.mode || modelKey(previous) !== modelKey(point))) {
            data.push([((previous.first ?? previous.timestamp) + .001) * 1000, null]);
          }
          const value = point.values[key];
          data.push([timestamp * 1000, Number.isFinite(value) ? value : null]);
          previous = point;
        });
        return {
          name: labels[key], type: 'line', data, showSymbol: points.length < 3, symbolSize: 5,
          connectNulls: false, smooth: false, lineStyle: { width: 2 },
          areaStyle: keys.length === 1 ? { opacity: .07 } : undefined,
        };
      }), animation: false,
    }, { notMerge: true, lazyUpdate: true });
  }, [history, metric, unit]);

  const values = history.points.flatMap(point => metric === 'cells'
    ? ['cell_1', 'cell_2', 'cell_3', 'cell_4'].map(key => point.values[key]) : [point.values[metric]])
    .filter(Number.isFinite);
  const digits = unit === 'V' ? 3 : 1;
  const description = `${metric === 'cells' ? '四节电芯电压' : labels[metric]}历史趋势。${values.length
    ? `记录范围 ${Math.min(...values).toFixed(digits)} 至 ${Math.max(...values).toFixed(digits)} ${unit}。详细数据可使用导出 CSV。` : '暂无记录。'}`;
  return <div className="chart" ref={container} role="img" aria-label={description} />;
}
