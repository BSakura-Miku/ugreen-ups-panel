import React, { lazy, Suspense, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Activity, ArrowDownToLine, BatteryCharging, Cable, ChevronRight, CircleHelp, Cpu, Database, PlugZap, Radio, ShieldCheck, Zap } from 'lucide-react';
import type { History } from './types';
import logo from './assets/us3000-logo.png';
import './style.css';
const Chart = lazy(() => import('./HistoryChart'));

type Sample = { calibration_profile?: string; battery_energy_estimate_w?: number | null; battery_estimate_quality?: string; ac_input_estimate_w?: number | null; ac_estimate_quality?: string; battery_charge_current_candidate_a?: number | null; battery_discharge_current_candidate_a?: number | null; battery_charge_power_candidate_w?: number | null; battery_discharge_power_candidate_w?: number | null; timestamp: number; mode: string; soc: number; input_voltage: number | null; output_voltage: number | null; current: number | null; current_kind: string; power_w: number | null; dc_power_estimate_w?: number | null; battery_voltage: number; cells: number[]; cell_delta_mv: number; runtime_sec: number | null; load_percent: number | null; warnings: string[]; raw_fields?: { be_u16: Record<string, number>; byte_26?: number; byte_27?: number; byte_28: number; frame_hex: string } };
type View = { fresh: boolean; source: string; age_sec: number | null; sample: Sample | null; device?: { serial: string }; nut?: { fresh: boolean; values?: Record<string, string> }; diagnostics?: { frames?: number; dropped?: number; rejected?: number; error?: string }; storage_error?: string | null };
type Event = { timestamp: number; kind: string; detail: string };
const modes: Record<string, string> = { online: '外部供电', charging: '电池充电中', battery: '电池供电', unknown: '状态待确认' };
const num = (n: number | null | undefined, digits = 1) => typeof n === 'number' && Number.isFinite(n) ? n.toFixed(digits) : '—';
const timeLabel = (n: number) => new Date(n * 1000).toLocaleTimeString('zh-CN', { hour12: false });

class ChartBoundary extends React.Component<{ children: React.ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed
      ? <div className="chart-empty" role="status"><span>趋势图暂时无法加载，实时数据仍可查看。</span><button className="reload-chart" onClick={() => location.reload()}>重新加载页面</button></div>
      : this.props.children;
  }
}

async function fetchJson<T>(url: string, parentSignal: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  parentSignal.addEventListener('abort', abort, { once: true });
  if (parentSignal.aborted) abort();
  const timeout = setTimeout(abort, 8000);
  try {
    const response = await fetch(url, { signal: controller.signal, cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
    parentSignal.removeEventListener('abort', abort);
  }
}

function powerNote(sample: Sample | null | undefined, kind: 'ac' | 'battery') {
  if (!sample) return '等待实时数据';
  const quality = kind === 'ac' ? sample.ac_estimate_quality : sample.battery_estimate_quality;
  if (quality === 'not_configured') return '尚未配置校准 · 查看数据说明';
  if (kind === 'ac' && sample.mode === 'battery') return '电池供电期间不提供此估算';
  if (kind === 'battery' && ['online', 'charging'].includes(sample.mode)) return '外部供电中 · 电池未放电';
  if (quality === 'warming_up') return '供电状态已切换 · 正在稳定读数';
  if (quality === 'unsupported_voltage') return '当前适配器电压不适用已配置的模型';
  if (quality === 'extrapolated') return '超出校准范围 · 仅供参考';
  if (!quality || quality === 'unavailable') return '当前状态暂无可用估算';
  return kind === 'ac' ? '含适配器损耗与回充 · 8 秒平滑' : '电池端 · 标称容量校准 · 8 秒平滑';
}

function Metric({ icon, label, value, unit, note }: { icon: React.ReactNode; label: string; value: string; unit: string; note: string }) {
  return <article className="metric"><div className="metric-label">{icon}{label}</div><div className="metric-value">{value}<span>{unit}</span></div><div className="muted">{note}</div></article>;
}

function App() {
  const [view, setView] = useState<View | null>(null);
  const [receivedAt, setReceivedAt] = useState(Date.now());
  const [historyLoading, setHistoryLoading] = useState(true);
  const [apiError, setApiError] = useState(false);
  const [historyError, setHistoryError] = useState(false);
  const [eventsError, setEventsError] = useState(false);
  const [history, setHistory] = useState<History>({ points: [], resolution_sec: 10 });
  const [events, setEvents] = useState<Event[]>([]);
  const [hours, setHours] = useState(24);
  const [metric, setMetric] = useState('ac_input_estimate_w');
  const [tab, setTab] = useState(['hardware', 'diagnostics'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'overview');
  useEffect(() => { const onHash = () => { const hash = location.hash.slice(1); if (hash !== 'main-content') setTab(['hardware', 'diagnostics'].includes(hash) ? hash : 'overview'); }; window.addEventListener('hashchange', onHash); return () => window.removeEventListener('hashchange', onHash); }, []);
  useEffect(() => { window.scrollTo(0, 0); }, [tab]);
  function navigate(next: string) { location.hash = next; setTab(next); }
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    let done = false; const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    async function update() {
      try { const body = await fetchJson<View>('/api/live', controller.signal); if (!done) { setView(body); setReceivedAt(Date.now()); setApiError(false); } }
      catch { if (!done) setApiError(true); }
      finally { if (!done) timer = setTimeout(update, 2000); }
    }
    update(); const tick = setInterval(() => setClock(Date.now()), 1000);
    return () => { done = true; controller.abort(); clearTimeout(timer); clearInterval(tick); };
  }, []);
  useEffect(() => {
    let done = false; const controller = new AbortController(); let timer: ReturnType<typeof setTimeout>;
    setHistory({ points: [], resolution_sec: 10 });
    setHistoryLoading(true); setHistoryError(false);
    async function update() {
      await Promise.all([
        fetchJson<History>(`/api/history?hours=${hours}`, controller.signal).then(x => { if (!done) { setHistory(x); setHistoryError(false); } }).catch(() => { if (!done) setHistoryError(true); }).finally(() => { if (!done) setHistoryLoading(false); }),
        fetchJson<Event[]>('/api/events', controller.signal).then(x => { if (!done) { setEvents(x); setEventsError(false); } }).catch(() => { if (!done) setEventsError(true); })
      ]);
      if (!done) timer = setTimeout(update, 10000);
    }
    update(); return () => { done = true; controller.abort(); clearTimeout(timer); };
  }, [hours]);
  const s = view?.sample;
  // Base freshness on the server's sample age so client clock skew cannot hide valid data.
  const age = s && view?.age_sec != null ? Math.max(0, view.age_sec + Math.max(0, clock - receivedAt) / 1000) : null;
  const fresh = !apiError && view?.fresh && age !== null && age <= 10;
  const isBattery = s?.mode === 'battery';
  const currentSample = fresh ? s : null;
  const calibrated = s?.calibration_profile === 'local-19v-v1';
  const nut = view?.nut;
  const unit = metric.endsWith('_w') ? 'W' : metric === 'soc' ? '%' : ['cells', 'adapter_input_voltage_v', 'ups_output_voltage_v'].includes(metric) ? 'V' : 'mV';
  return <div className="shell">
    <a className="skip-link" href="#main-content">跳到主要内容</a><aside className="sidebar"><a className="brand" href="#overview" aria-label="US3000 电力概览"><img className="brand-icon" src={logo} alt="" width="44" height="44" /><span className="brand-name">US3000<span className="brand-sub">电力监控</span></span></a>
      <div className="nav-caption">工作空间</div><nav aria-label="主导航">
        <button aria-current={tab === 'overview' ? 'page' : undefined} className={tab === 'overview' ? 'selected' : ''} onClick={() => navigate('overview')}><Activity size={18} />电力概览<ChevronRight size={15} /></button>
        <button aria-current={tab === 'diagnostics' ? 'page' : undefined} className={tab === 'diagnostics' ? 'selected' : ''} onClick={() => navigate('diagnostics')}><Cpu size={18} />数据说明</button>
        <button aria-current={tab === 'hardware' ? 'page' : undefined} className={tab === 'hardware' ? 'selected' : ''} onClick={() => navigate('hardware')}><Database size={18} />硬件档案</button>
      </nav>
      <div className="sidebar-bottom"><ShieldCheck size={20} /><strong>只读监控</strong><p>采集与面板独立运行<br/>UPS 保护由 NAS 管理</p><div className="device-tag">UGREEN · US3000</div></div>
    </aside>
    <main id="main-content" tabIndex={-1}><header><div className="breadcrumb">设备监控 <ChevronRight size={14} /><span>US3000</span></div><div className="header-status"><span className={`dot ${fresh ? 'green' : 'amber'}`} />{fresh ? '实时采集' : apiError ? '连接中断' : view ? '等待数据' : '连接中'}<span className="header-clock">{new Date(clock).toLocaleTimeString('zh-CN', { hour12: false })}</span></div></header>
      <div className="page-heading"><div><div className="eyebrow">UGREEN US3000</div><h1>{tab === 'overview' ? '电力概览' : tab === 'hardware' ? '硬件档案' : '数据说明'}</h1><p>关注每一次供电，了解每一节电芯。</p></div><div className="serial">USB 直连<span>{view?.device?.serial || 'US3000'}</span></div></div>
      {view?.source === 'replay' && <div className="notice">演示数据 · 用于功能预览，不代表 NAS 当前状态。</div>}
      {!fresh && <div className="notice" role="status">{apiError ? '面板连接中断。' : '正在等待有效的 UPS 数据。'}{s ? ` 最近一次数据：${timeLabel(s.timestamp)}。实时数值已隐藏。` : ' 连接后将自动显示。'}</div>}
      {view?.storage_error && <div className="notice">{view.storage_error}，实时数据仍可查看。</div>}
      {tab === 'overview' ? <>
        <section className="hero-grid"><article className={`supply-card ${isBattery ? 'on-battery' : ''}`}><div className="section-kicker"><PlugZap size={17} />供电状态<span className="live-pill">{fresh ? 'LIVE' : 'OFFLINE'}</span></div><h2>{fresh && s ? modes[s.mode] || modes.unknown : '等待有效数据'}</h2><p>{fresh ? s?.mode === 'online' ? '市电供电，电池待命。' : s?.mode === 'battery' ? 'UPS 正在使用电池供电。' : s?.mode === 'charging' ? '外部电源正在为 NAS 供电，并为电池回充。' : '正在读取设备工作状态。' : '最后一次读数不会作为实时状态展示。'}</p><div className={`power-path ${fresh && isBattery ? 'battery-path' : !fresh ? 'inactive-path' : ''}`}><span><Cable size={26} />电源适配器</span><i /><span className="path-ups"><BatteryCharging size={29} />US3000</span><i /><span><Database size={25} />NAS</span></div><div className="supply-footer"><Radio size={14} />{fresh ? '约 2 秒刷新' : '等待采集器'}<span>{age !== null ? `${Math.floor(age)} 秒前更新` : '尚无读数'}</span></div></article>
        <article className="charge-card"><div className="section-kicker">电池电量<BatteryCharging size={18} /></div><div className="charge-number">{num(currentSample?.soc, 0)}<span>%</span></div><div className="battery-track">{Array.from({ length: 20 }, (_, i) => <i key={i} className={currentSample && currentSample.soc > i * 5 ? 'filled' : ''} />)}</div><div className="charge-bottom"><span>电池组电压<strong>{num(currentSample?.battery_voltage, 3)} V</strong></span><span>额定电池能量<strong>43.2 Wh</strong></span></div></article></section>
        <section className="power-metrics" aria-label="功率估算">
          <Metric icon={<Zap size={17} />} label="交流输入功率 · 估算" value={num(currentSample?.ac_input_estimate_w)} unit="W" note={powerNote(currentSample, 'ac')} />
          <Metric icon={<BatteryCharging size={17} />} label="电池放电功率 · 估算" value={num(currentSample?.battery_energy_estimate_w)} unit="W" note={powerNote(currentSample, 'battery')} />
        </section>
        <p className="power-caption">交流输入与电池放电分别代表两个测量位置，均非 NAS 输出功率。<a href="#diagnostics">了解估算方式 ↗</a></p>
        <section className="metrics">
          <Metric icon={<Cable size={17} />} label="适配器输入电压" value={num(currentSample?.input_voltage, 3)} unit="V" note="适配器直流侧" />
          <Metric icon={<Cable size={17} />} label="UPS 输出电压" value={num(currentSample?.output_voltage, 3)} unit="V" note="市电直通 / 电池稳压" />
          <Metric icon={<Activity size={17} />} label="电芯压差" value={num(currentSample?.cell_delta_mv, 0)} unit="mV" note="最高与最低电芯电压之差" />
        </section>
        <section className="panel battery-flow"><div className="panel-heading"><div><h3>电池能量流 <span className="muted">估算</span></h3><p>{!currentSample ? '等待有效数据' : currentSample.mode === 'charging' ? '正在充电' : currentSample.mode === 'battery' ? '正在放电' : '电池待命'}</p></div></div><div className="battery-flow-values"><span>充电电流<strong>{num(currentSample?.battery_charge_current_candidate_a, 3)} A</strong></span><span>充电功率<strong>{num(currentSample?.battery_charge_power_candidate_w)} W</strong></span><span>放电电流<strong>{num(currentSample?.battery_discharge_current_candidate_a, 3)} A</strong></span></div><p className="muted">电流采用设备换算值，尚无独立精度验证；放电功率的校准不改变这里的电流。<a href="#diagnostics">查看数据说明 ↗</a></p></section>
        <section className="detail-grid"><article className="panel trend"><div className="panel-heading"><div><h3>运行趋势 <span>{unit}</span></h3><p>{metric === 'ac_input_estimate_w' ? '交流输入估算 · 电池供电期间留空' : metric === 'battery_energy_estimate_w' ? '电池端估算 · 外部供电期间留空' : '历史趋势 · 中断时段留空'}</p></div><div className="range" aria-label="时间范围">{[[1, '1 小时'], [24, '24 小时'], [168, '7 天']].map(([v, t]) => <button key={v} aria-pressed={hours === v} className={hours === v ? 'active' : ''} onClick={() => setHours(Number(v))}>{t}</button>)}</div></div><div className="chart-controls"><select aria-label="趋势指标" value={metric} onChange={e => setMetric(e.target.value)}><option value="ac_input_estimate_w">交流输入功率 · 估算</option><option value="adapter_input_voltage_v">适配器输入电压</option><option value="ups_output_voltage_v">UPS 输出电压</option><option value="battery_charge_power_candidate_w">电池充电功率 · 估算</option><option value="battery_energy_estimate_w">电池放电功率 · 容量估算</option><option value="soc">电量</option><option value="cells">四节电芯电压</option><option value="cell_delta_mv">电芯压差</option></select><a href={`/api/export.csv?hours=${hours}`}><ArrowDownToLine size={14} />导出 CSV</a></div>{historyLoading ? <div className="chart-empty" role="status">正在加载历史记录…</div> : historyError ? <div className="chart-empty">历史查询暂不可用，将自动重试。</div> : history.points.some(p => metric === 'cells' ? p.values.cell_1 != null : p.values[metric] != null) ? <ChartBoundary><Suspense fallback={<div className="chart-empty" role="status">正在加载趋势图…</div>}><Chart history={history} metric={metric} unit={unit} /></Suspense></ChartBoundary> : <div className="chart-empty"><Activity size={28} /><span>暂无该指标记录</span><small>有可用读数时将自动记录，可切换其他指标查看。</small></div>}</article>
        <article className="panel cells"><div className="panel-heading"><div><h3>电芯状态</h3><p>4 节串联 · 电压分布</p></div><BatteryCharging size={19} /></div><div className="delta"><strong>{num(currentSample?.cell_delta_mv, 0)}<span>mV</span></strong><span>最大压差</span></div><div className="cell-list">{[0, 1, 2, 3].map(i => <div key={i} className="cell-row"><span>电芯 0{i + 1}</span><div className="cell-track"><i style={{ width: currentSample ? `${Math.max(0, Math.min(100, (currentSample.cells[i] - 2.5) / 1.8 * 100))}%` : '0%' }} /></div><strong>{num(currentSample?.cells[i], 3)} <small>V</small></strong></div>)}</div><div className="cell-note"><CircleHelp size={14} />压差反映电芯均衡程度，不等于电池容量。</div></article></section>
        <article className="panel events"><div className="panel-heading"><h3>状态记录</h3><span className="muted">最近事件</span></div>{eventsError ? <p className="muted">事件查询暂不可用。</p> : events.length ? events.slice(0, 5).map((e, i) => <div className="event-row" key={i}><span className={`dot ${e.detail === 'offline' ? 'amber' : 'green'}`} /><span>{e.detail === 'offline' ? '采集数据离线' : `${e.kind === 'power' ? '供电状态' : '连接恢复'} · ${modes[e.detail.split(':')[1]] || '状态更新'}`}</span><time>{new Date(e.timestamp * 1000).toLocaleString('zh-CN', { hour12: false })}</time></div>) : <p className="muted">暂无状态变化记录。</p>}</article>
      </> : tab === 'hardware' ? <section className="hardware-page">
        <article className="hardware-intro"><div><div className="eyebrow">DC UPS · US3000</div><h2>小体积，持续供电。</h2><p>直流供电结构，让 NAS 在市电中断时继续运行。电池管理与电源转换分别承担储能监测和供电任务。</p></div><div className="hardware-rating"><strong>120<span>W</span></strong><span>额定最大输出</span></div></article>
        <div className="spec-strip"><div><strong>43.2 Wh</strong><span>整机标称电池能量</span></div><div><strong>4S · 3 Ah</strong><span>四节串联电池组</span></div><div><strong>12 V / 10 A</strong><span>额定电池输出</span></div><div><strong>约 439 g</strong><span>拆解样机重量</span></div></div>
        <article className="panel"><h3>供电结构</h3><div className="topology-row"><span>19 V 适配器</span><ChevronRight/><span>US3000 直通</span><ChevronRight/><span>NAS</span></div><div className="topology-row"><span>四串锂电池</span><ChevronRight/><span>12 V 稳压输出</span><ChevronRight/><span>NAS</span></div><p className="muted">以上电压路径来自本机切换记录；额定输出规格与市电直通电压分别列示。</p></article>
        <article className="panel"><h3>核心器件</h3><div className="chip-list">{[['GD32F303RCT6', '整机控制', 'GigaDevice · Cortex-M4'], ['CBM8580KV6NT', '电池管理', 'Chipsea · 多串锂电池监测与保护'], ['TPS55289', '升降压转换', 'Texas Instruments · 同步升降压'], ['SC8002', '降压控制', 'Southchip · 同步降压控制器'], ['LM74610-Q1', '理想二极管控制', 'Texas Instruments · 电源路径控制']].map(([name,role,detail]) => <div key={name}><Cpu size={20}/><strong>{name}</strong><span>{role}</span><small>{detail}</small></div>)}</div></article>
        <article className="panel"><h3>电池与结构</h3><dl>{[['电芯', 'SunPower INR18650-3000 × 4'], ['单节标称', '3.7 V · 3000 mAh'], ['整机电池标称', '14.4 V · 3000 mAh'], ['输入规格', '12 V / 10 A · 19 V / 7.9 A · 20 V / 7 A'], ['结构', '控制板与功率板分层，带独立温度探头']].map(([k,v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl><p className="muted">硬件资料来自拆解样机及产品规格，不代表已读取本机芯片型号。电芯标称电压合计与整机标称口径不同；43.2 Wh 不等同于实测可用能量。</p><div className="source-links"><a href="https://www.chongdiantou.com/archives/1749216234051.html" target="_blank" rel="noreferrer">充电头网拆解 ↗</a><a href="https://ai.ugreen.com/products/ugreen-nas-backup-power-120w-12000mah" target="_blank" rel="noreferrer">UGREEN 产品规格 ↗</a><a href="https://www.ti.com/product/TPS55289" target="_blank" rel="noreferrer">TI 器件资料 ↗</a></div></article>
      </section> : <section className="diagnostic-grid"><article className="panel"><h3>数据来源与精度</h3><p>实时数据来自本机 UPS USB 遥测。电量、电压为设备报告值；功率和电池电流属于估算值。</p><h3>交流输入功率</h3><p>{calibrated ? '已启用本机 19 V 适配器校准，包含回充补偿，采用 8 秒平滑。' : '当前尚未启用适用于此设备的功率校准。电量、电压和设备电流仍正常显示；功率主卡在配置校准后提供估算。'}面板运行不读取智能插座。</p><p>开源安装默认不套用其他设备的校准系数。部署者需依据安装指南选用与设备、适配器和接线匹配的校准配置。</p>{calibrated && <p>此配置的非充电独立样本平均绝对误差约 1.4 W；充电时段后半段验证约 1.1 W，最大约 4.6 W。验证范围约 58–83 W，不代表全量程精度。更换适配器、固件或接线后需要重新校准。</p>}<p>超出已验证负载或充电电流范围时标注“仅供参考”；电池供电时不提供交流估算。电池侧功率与电流未经过独立直流仪表校准，不能视作 NAS 输出功率。</p><h3>电池供电功率</h3><p>电池放电功率表示电池端释放能量的速率，包含 UPS 转换损耗，与 NAS 输出功率不同。已提供的本机配置使用 43.2 Wh 标称能量及长放电电量趋势校准，并作 8 秒平滑；假设实际容量接近标称、SOC 近似能量比例。容量衰减可能使估算偏高，尚无独立仪表精度验证。</p><h3>历史与状态</h3><p>状态切换和数据中断分别记录。不同计算版本的历史分开存储；无数据时留空。设备未提供可用的续航与负载率，因此不展示。</p></article>
      <article className="panel"><h3>连接与记录</h3><dl>{[['校准配置', calibrated ? '本机 19 V · v1' : s ? '未配置' : '等待数据'], ['采集连接', fresh ? '正常' : '等待恢复'], ['历史存储', view ? view.storage_error || '正常' : '等待数据'], ['有效报文', view?.diagnostics?.frames ?? '—'], ['丢弃报文', view?.diagnostics?.dropped ?? '—'], ['系统 UPS 服务', nut?.fresh ? '已连接' : '暂不可用']].map(([k,v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl><p className="muted">监控采用只读采集。关机与电源保护由 NAS 系统管理。</p><details><summary>高级诊断</summary><p>输出电流原读数：{num(currentSample?.current,3)} A；输出功率原计算：{num(currentSample?.dc_power_estimate_w)} W。两者未完成测点及比例校准，不作为主要功耗指标。</p><p>充电电流使用字节 29–30，放电电流使用 31–32，按原值 ÷ 1000 计算；电池功率使用电池组电压。字节 20 不解释为总输入电流。</p><p>本机校准配置：交流模型为 1.182379 × byte28；回充时加 1.284315 × 电池充电功率原计算，再作 8 秒平均。电池放电模型为电池电压 × 字节 31–32 换算电流 × 1.209113。系数为本机经验参数，不是转换效率。</p><dl>{Object.entries(currentSample?.raw_fields?.be_u16 || {}).map(([k,v]) => <div key={k}><dt>字节 {k}–{Number(k)+1}</dt><dd>{v}</dd></div>)}</dl><h3>系统接口原值</h3><dl>{Object.entries(nut?.values || {}).map(([k,v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl></details>{s?.warnings?.map(w => <p className="notice" key={w}>{w}</p>)}</article></section>}
      <footer><span><ShieldCheck size={14} />面板不执行关机或 UPS 控制</span><span>US3000 Monitor · v0.3.0</span></footer>
    </main>
  </div>;
}
createRoot(document.getElementById('root')!).render(<App />);
