import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { ChevronDown, ChevronLeft, ChevronRight, PlugZap } from 'lucide-react';
import { startVisiblePolling } from './readPolling';
import {
  energyCalendarCells, energyCalendarFocus, energyCoverage, energyDateLabel, energyDayState, energyDuration,
  energyFinite, energyHeatLevel, energyKwh, energyLocalDate, energyMonthDays, energyMonthLabel, energyPower,
  energyTimestamp, energyUsageNotice, readEnergyUsageDay, readEnergyUsageMonth, shiftEnergyMonth,
} from './energyUsageDisplay';
import type { EnergyHour, EnergyUsageDay, EnergyUsageMonth } from './energyUsageDisplay';
import './energy-usage.css';

type ReadState<T> = { key: string | null; state: 'loading' | 'ready' | 'error'; data: T | null };
const WEEKDAYS = ['一', '二', '三', '四', '五', '六', '日'];

function LoadingNumber() {
  return <span className="energy-skeleton energy-number-skeleton" aria-hidden="true" />;
}

function HourlyPower({ hours }: { hours: EnergyHour[] }) {
  const sorted = [...hours].sort((a, b) => a.hour - b.hour);
  const valid = sorted.filter(hour => hour.covered_sec > 0 && energyFinite(hour.average_power_w));
  if (!valid.length) return <div className="energy-chart-empty">该日暂无有效小时记录</div>;
  const maximum = Math.max(0, ...valid.map(hour => hour.average_power_w ?? 0));
  const scale = maximum > 0 ? Math.ceil(maximum / 10) * 10 : 1;
  const left = 34, width = 408, top = 15, height = 95, bottom = top + height, slot = width / 24;
  return <figure className="energy-hourly-chart">
    <svg viewBox="0 0 456 142" role="img" aria-label="按小时统计的市电输入平均功率；有效记录的小时均值，缺失时段留空，精确数值见小时记录表">
      {maximum > 0 && <><line x1={left} x2={left + width} y1={top} y2={top} className="energy-chart-grid"/><text x={left - 6} y={top + 4} textAnchor="end" className="energy-chart-axis">{scale}</text></>}
      <line x1={left} x2={left + width} y1={bottom} y2={bottom} className="energy-chart-grid"/>
      <text x={left - 6} y={bottom + 4} textAnchor="end" className="energy-chart-axis">0</text>
      <text x={left - 6} y={6} textAnchor="end" className="energy-chart-axis">W</text>
      {sorted.map(hour => {
        if (hour.covered_sec <= 0 || !energyFinite(hour.average_power_w)) return null;
        const barHeight = hour.average_power_w / scale * height;
        const x = left + hour.hour * slot + 3;
        const partial = hour.covered_sec + 5 < hour.expected_sec;
        const title = `${String(hour.hour).padStart(2, '0')}:00–${String(hour.hour + 1).padStart(2, '0')}:00，平均 ${energyPower(hour.average_power_w)} W，覆盖 ${energyDuration(hour.covered_sec)}，${energyCoverage(hour.coverage_ratio)}`;
        return <g key={hour.hour}><title>{title}</title>{hour.average_power_w === 0
          ? <line x1={x} x2={x + slot - 6} y1={bottom} y2={bottom} className="energy-hour-zero"/>
          : <rect x={x} y={bottom - barHeight} width={slot - 6} height={barHeight} rx={2} className={`energy-hour-bar ${partial ? 'energy-hour-partial' : ''}`}/>}</g>;
      })}
      {maximum === 0 && <text x={left + width / 2} y={top + height / 2} textAnchor="middle" className="energy-chart-zero-label">有效时段均为 0 W</text>}
      {[0, 6, 12, 18, 24].map(hour => <text key={hour} x={left + hour / 24 * width} y={bottom + 20} textAnchor={hour === 0 ? 'start' : hour === 24 ? 'end' : 'middle'} className="energy-chart-axis">{String(hour).padStart(2, '0')} 时</text>)}
    </svg>
    <figcaption>小时均值（有效记录） · 未记录时段留空</figcaption>
  </figure>;
}

function DayDetail({ date, data, state, currentDate }: { date: string | null; data: EnergyUsageDay | null; state: 'loading' | 'ready' | 'error'; currentDate: string }) {
  const day = data?.day;
  const loading = state === 'loading';
  const recordingIssue = !!data?.storage_error || (data?.dropped_intervals ?? 0) > 0;
  return <section className="energy-day-detail" id="energy-day-detail" aria-labelledby="energy-day-heading" aria-busy={loading}>
    <div className="energy-day-heading"><h4 id="energy-day-heading">{date ? `${energyDateLabel(date)} · 当日明细` : '当日明细'}</h4>
      {day && <span className="energy-day-status">{data && !data.capture_fresh && day.date === data.current_date ? '今天 · 已记录' : energyDayState(day, data?.current_date ?? currentDate)}</span>}</div>
    {state === 'error' ? <p className="energy-read-error" role="status">该日记录暂时无法读取，正在重试。</p>
      : !date ? <p className="energy-detail-placeholder">选择日期查看已记录电量与小时功率。</p>
      : <>
        <div className="energy-day-totals"><div><span>已记录累计</span><strong>{loading ? <LoadingNumber/> : energyKwh(day?.estimate_kwh)}<small>kWh</small></strong></div>
          <div><span>平均输入功率</span><strong>{loading ? <LoadingNumber/> : energyPower(day?.average_power_w)}<small>W</small></strong></div></div>
        <div className="energy-day-coverage"><div><span>记录覆盖率</span><strong>{loading ? '读取中…' : energyCoverage(day?.coverage_ratio)}</strong></div>
          <progress max={1} value={day?.coverage_ratio ?? 0} aria-label="所选日期已过去时段的记录覆盖率"/>
          <p>{loading ? '正在读取覆盖时长' : <>已记录 {energyDuration(day?.covered_sec)} / {date === (data?.current_date ?? currentDate) ? '已过去' : '应记录'} {energyDuration(day?.expected_sec)}</>}</p></div>
        {recordingIssue && <p className="energy-detail-note">记录存在异常，累计仅含已记录区间。</p>}
        {!!day && day.basis_count > 1 && <p className="energy-detail-note"><span className="energy-segment-badge">分段估算</span>各段按当时生效的校准累计，旧记录不重算。</p>}
        {loading ? <div className="energy-chart-loading" aria-hidden="true"><span className="energy-skeleton"/><span className="energy-skeleton"/><span className="energy-skeleton"/><span className="energy-skeleton"/></div>
          : data && <HourlyPower hours={data.hours}/>}
        {data && <details className="energy-hour-details"><summary>查看小时记录 <ChevronDown size={13}/></summary>
          <table><caption className="visually-hidden">{date} 每小时已记录电量、有效记录平均功率与覆盖时长；空缺不计作零</caption>
            <thead><tr><th scope="col">小时</th><th scope="col">kWh</th><th scope="col">均值 W</th><th scope="col">覆盖</th></tr></thead>
            <tbody>{[...data.hours].sort((a, b) => a.hour - b.hour).map(hour => <tr key={hour.hour}>
              <th scope="row">{String(hour.hour).padStart(2, '0')}–{String(hour.hour + 1).padStart(2, '0')}</th>
              <td>{energyKwh(hour.estimate_kwh)}</td><td>{energyPower(hour.average_power_w)}</td><td>{hour.expected_sec > 0 ? energyDuration(hour.covered_sec) : '—'}</td>
            </tr>)}</tbody></table>
        </details>}
      </>}
  </section>;
}

export default function EnergyUsageCard() {
  const [requestedMonth, setRequestedMonth] = useState<string | null>(null);
  const [monthRead, setMonthRead] = useState<ReadState<EnergyUsageMonth>>({ key: null, state: 'loading', data: null });
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const [focusDate, setFocusDate] = useState<string | null>(null);
  const [dayRead, setDayRead] = useState<ReadState<EnergyUsageDay>>({ key: null, state: 'loading', data: null });
  const [bounds, setBounds] = useState(() => ({ currentDate: energyLocalDate(), firstMonth: null as string | null }));
  const monthGeneration = useRef(0), dayGeneration = useRef(0);
  const dayButtons = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    const generation = ++monthGeneration.current;
    setMonthRead({ key: requestedMonth, state: 'loading', data: null });
    const stop = startVisiblePolling(async signal => {
      try {
        const data = await readEnergyUsageMonth(requestedMonth, signal);
        if (!signal.aborted && generation === monthGeneration.current) {
          setMonthRead({ key: requestedMonth, state: 'ready', data });
          setBounds({ currentDate: data.current_date, firstMonth: data.first_month });
        }
      } catch {
        if (!signal.aborted && generation === monthGeneration.current) setMonthRead({ key: requestedMonth, state: 'error', data: null });
      }
    }, () => 10000);
    return () => { monthGeneration.current++; stop(); };
  }, [requestedMonth]);

  const monthState = monthRead.key === requestedMonth ? monthRead.state : 'loading';
  const report = monthState === 'ready' && monthRead.key === requestedMonth ? monthRead.data : null;
  const currentDate = report?.current_date ?? bounds.currentDate;
  const currentMonth = currentDate.slice(0, 7);
  const month = requestedMonth ?? report?.month ?? currentMonth;
  const firstMonth = bounds.firstMonth ?? currentMonth;
  const dayDate = selectedDate?.slice(0, 7) === month && selectedDate <= currentDate ? selectedDate : null;

  useEffect(() => {
    if (!report || report.month !== month || dayDate) return;
    const next = month === report.current_date.slice(0, 7) ? report.current_date
      : `${month}-${String(energyMonthDays(month)).padStart(2, '0')}`;
    if (next <= report.current_date) { setSelectedDate(next); setFocusDate(next); }
  }, [report, month, dayDate]);

  useEffect(() => {
    const generation = ++dayGeneration.current;
    setDayRead({ key: dayDate, state: 'loading', data: null });
    if (!dayDate) return;
    const stop = startVisiblePolling(async signal => {
      try {
        const data = await readEnergyUsageDay(dayDate, signal);
        if (!signal.aborted && generation === dayGeneration.current) setDayRead({ key: dayDate, state: 'ready', data });
      } catch {
        if (!signal.aborted && generation === dayGeneration.current) setDayRead({ key: dayDate, state: 'error', data: null });
      }
    }, () => 10000);
    return () => { dayGeneration.current++; stop(); };
  }, [dayDate]);

  const dayState = dayRead.key === dayDate ? dayRead.state : 'loading';
  const selectedDay = dayState === 'ready' && dayRead.key === dayDate ? dayRead.data : null;
  const previousMonth = shiftEnergyMonth(month, -1), nextMonth = shiftEnergyMonth(month, 1);
  const canPrevious = !!previousMonth && previousMonth >= firstMonth;
  const canNext = !!nextMonth && nextMonth <= currentMonth;
  const cells = energyCalendarCells(month);
  const calendarDays = new Map(report?.days.map(day => [day.date, day]) ?? []);
  const maximum = Math.max(0, ...[...calendarDays.values()].map(day => day.estimate_kwh ?? 0));
  const tabDate = focusDate?.slice(0, 7) === month && focusDate <= currentDate ? focusDate : dayDate;
  const notice = energyUsageNotice(report);
  const monthLoading = monthState === 'loading';

  function changeMonth(next: string | null) {
    setRequestedMonth(next); setSelectedDate(null); setFocusDate(null);
  }

  function onDayKey(event: KeyboardEvent<HTMLButtonElement>, date: string) {
    const next = energyCalendarFocus(date, event.key, month, currentDate);
    if (!next) return;
    event.preventDefault(); setFocusDate(next); dayButtons.current.get(next)?.focus();
  }

  return <article className="panel energy-usage" aria-labelledby="energy-usage-heading">
    <div className="panel-heading energy-heading"><div><h3 id="energy-usage-heading"><PlugZap size={18}/>用电统计 · 估算</h3><p>市电输入已记录累计 · 1 kWh = 1 度电</p></div>
      {report && <span className="energy-zone-label">{report.timezone === 'Asia/Shanghai' ? '北京时间' : report.timezone}</span>}</div>
    {monthState === 'error' && <p className="energy-read-error" role="status">用电记录暂时无法读取，正在重试。</p>}
    {notice && <p className="energy-recording-note" role="status">{notice}{report && ['not_configured', 'charge_not_configured'].includes(report.reason ?? '') && <a className="energy-calibration-link" href="#calibration">前往功率校准</a>}</p>}
    <dl className="energy-summaries" aria-busy={monthLoading}>
      <div><dt>今日累计</dt><dd>{monthLoading ? <LoadingNumber/> : energyKwh(report?.today.estimate_kwh)}<small>kWh</small></dd><p>{report && !report.capture_fresh ? '已记录 · 采集离线' : '已记录 · 截至当前'}</p></div>
      <div><dt>所选月累计</dt><dd>{monthLoading ? <LoadingNumber/> : energyKwh(report?.summary.estimate_kwh)}<small>kWh</small></dd><p>{report ? `已记录 ${report.summary.recorded_days} 天` : '已记录累计'}{(report?.summary.basis_count ?? 0) > 1 && <span className="energy-segment-badge">分段估算</span>}</p></div>
      <div><dt>完整记录日均</dt><dd>{monthLoading ? <LoadingNumber/> : energyKwh(report?.summary.complete_day_average_kwh)}<small>kWh</small></dd><p>{report?.summary.complete_days ? `${report.summary.complete_days} 个已结束完整日` : '暂无完整日'}</p></div>
    </dl>
    <div className="energy-main-grid">
      <section className="energy-calendar-section" aria-labelledby="energy-month-heading" aria-busy={monthLoading}>
        <div className="energy-month-navigation"><h4 id="energy-month-heading">{energyMonthLabel(month)}</h4><div className="energy-month-buttons">
          {month !== currentMonth && <button className="energy-current-month" type="button" onClick={() => changeMonth(null)}>本月</button>}
          <button type="button" aria-label="查看上个月用电记录" disabled={!canPrevious} onClick={() => previousMonth && changeMonth(previousMonth)}><ChevronLeft size={17}/></button>
          <button type="button" aria-label="查看下个月用电记录" disabled={!canNext} onClick={() => nextMonth && changeMonth(nextMonth)}><ChevronRight size={17}/></button>
        </div></div>
        <table className="energy-calendar" role="grid" aria-label={`${energyMonthLabel(month)}用电日历，单位 kWh`} aria-describedby="energy-calendar-help">
          <thead><tr>{WEEKDAYS.map(day => <th key={day} scope="col" abbr={`星期${day}`}>{day}</th>)}</tr></thead>
          <tbody>{Array.from({ length: cells.length / 7 }, (_, row) => <tr key={row}>{cells.slice(row * 7, row * 7 + 7).map((date, column) => {
            if (!date) return <td key={`empty-${column}`} aria-hidden="true"/>;
            const day = calendarDays.get(date), future = date > currentDate, today = date === currentDate;
            const selected = date === dayDate, heat = energyHeatLevel(day, maximum);
            return <td key={date} aria-selected={selected}><button type="button" disabled={future || !report} tabIndex={tabDate === date ? 0 : -1}
              ref={element => { if (element) dayButtons.current.set(date, element); else dayButtons.current.delete(date); }}
              className={`energy-calendar-day energy-heat-${heat}${selected ? ' is-selected' : ''}${today ? ' is-today' : ''}${future ? ' is-future' : ''}`}
              aria-current={today ? 'date' : undefined} aria-pressed={selected} aria-controls="energy-day-detail"
              aria-label={`${energyDateLabel(date)}，${future ? '尚未到来' : monthLoading ? '正在读取' : !report ? '记录暂不可用' : `${energyDayState(day, currentDate)}，${day?.estimate_kwh === null || !day ? '无有效电量记录' : `${energyKwh(day.estimate_kwh)} 千瓦时`}`}`}
              onFocus={() => setFocusDate(date)} onKeyDown={event => onDayKey(event, date)} onClick={() => setSelectedDate(date)}>
              <span className="energy-calendar-date"><span>{Number(date.slice(8))}</span>{today ? <small>今</small> : day?.status === 'partial' ? <i className="energy-partial-dot" aria-hidden="true"/> : null}</span>
              <span className="energy-calendar-value">{future ? <span aria-hidden="true">&nbsp;</span> : monthLoading ? <span className="energy-skeleton" aria-hidden="true"/> : energyKwh(day?.estimate_kwh)}</span>
            </button></td>;
          })}</tr>)}</tbody>
        </table>
        <div className="energy-calendar-legend" id="energy-calendar-help"><span><i className="energy-partial-dot"/>部分记录</span><span className="energy-heat-legend">记录电量 少{[1, 2, 3, 4, 5].map(level => <i className={`energy-heat-${level}`} key={level}/>)}多</span></div>
      </section>
      <DayDetail date={dayDate} data={selectedDay} state={dayState} currentDate={currentDate}/>
    </div>
    <div className="energy-recorded-at"><span>{report?.last_recorded_at ? `记录截至 ${energyTimestamp(report.last_recorded_at, report.timezone)}` : report ? '尚无已记录区间' : '等待统计记录'}{report && !report.capture_fresh ? ' · 采集离线' : ''}</span>
      {report?.tracking_started_at && <span>开始记录于 {energyTimestamp(report.tracking_started_at, report.timezone)}</span>}</div>
    <details className="energy-method"><summary>统计口径 <ChevronDown size={14}/></summary>
      <p>按{report?.timezone === 'Asia/Shanghai' || !report ? '北京时间（UTC+8）' : `${report.timezone}（UTC${report.utc_offset}）`}自然日累计市电输入估算。电池放电能量不加入；连续确认的电池供电时段，市电输入记为 0。未覆盖时段保留空缺，不补算。</p>
      <p>今日只统计截至当前的已记录部分；所选月合计包含该月已记录区间。完整记录日均只使用已结束、全天缺口不超过 5 秒的日期，当前日与部分记录日不加入日均。记录启用前的时段也计入覆盖率的缺口。</p>
      <p>不同设备、数据来源或校准依据分别记录，显示为「分段估算」；各段按当时生效的校准累计，不用新系数重算旧记录。统计从启用后开始积累，不回填旧历史。</p>
    </details>
  </article>;
}
