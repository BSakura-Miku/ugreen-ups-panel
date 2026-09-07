import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ArrowDownToLine, Cable, Cpu, Info, Radio } from 'lucide-react';
import type { DiagnosticBuild, DiagnosticCheck, DiagnosticView, RawObservationPoint } from './types';
import { buildRawTrace, diagnosticCount, diagnosticTime, diagnosticVersion, rawChannelLabel, runtimeObservation } from './diagnosticDisplay';
import CollectorUpdatePanel from './CollectorUpdatePanel';

const modes: Record<string, string> = { online: '外部供电', charging: '充电', battery: '电池供电', unknown: '工况未知' };
const checkLabels: Record<DiagnosticCheck['status'], string> = { ok: '已确认', waiting: '等待', warning: '需关注', unknown: '未知', error: '异常' };
const counterLabels: Record<string, string> = {
  frames: '有效报文', dropped: '丢弃报文', rejected: '拒绝报文', errors: '错误次数', usb_events: 'USB 事件',
  target_events: '目标设备事件', not_completion: '非完成事件', not_interrupt_in: '非中断输入',
  invalid_status: '传输状态异常', missing_payload: '缺失载荷', invalid_length: '报文长度异常',
  invalid_report_id: '报告标识异常', accepted_reports: '接收的私有报告', invalid_sample: '无效样本', stale_sample: '失鲜样本',
};
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
const elapsedLabel = (value: unknown) => finite(value) && value >= 0 ? `${value.toFixed(1)} 秒前` : '未知';
const thresholdLabel = (value: unknown, unit: string) => finite(value) && value >= 0 ? `${value} ${unit}` : '未知';

class DiagnosticsBoundary extends React.Component<{ children: React.ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed
      ? <article className="panel diag-error" role="status"><h3>诊断信息暂不可用</h3><p>当前诊断数据无法显示，请重新进入此页或刷新页面。其他监控功能仍可使用。</p></article>
      : this.props.children;
  }
}

function BuildValue({ build }: { build: DiagnosticBuild | null | undefined }) {
  return <><span>{diagnosticVersion(build?.version)}</span>{(build?.revision || build?.source_sha256) && <details className="diag-details build-details"><summary>构建详情</summary>{build.revision && <small>修订 {build.revision}</small>}{build.source_sha256 && <small>源码指纹 {build.source_sha256}</small>}</details>}</>;
}

function RawTrend({ points }: { points: RawObservationPoint[] }) {
  const container = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const resize = () => setWidth(Math.max(280, Math.min(640, element.clientWidth)));
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    resize();
    return () => observer.disconnect();
  }, []);
  const trace = useMemo(() => buildRawTrace(points.map(point => ({
    timestamp: point.timestamp, mode: point.mode, channelA: point.byte_26, channelB: point.byte_27,
    segment: `${point.device_alias}:${point.source}:${point.segment}`,
  })), width), [points, width]);
  const ticks = trace ? [trace.low, (trace.low + trace.high) / 2, trace.high] : [];
  return <div ref={container} className="diag-trend-container">{!trace ? <div className="diag-trend-empty">尚无可绘制的原始通道观测。</div> : <>
    <div className="diag-trend-legend"><span className="diag-channel-a">A · 字节 26</span><span className="diag-channel-b">B · 字节 27</span><span>无单位原值</span></div>
    <svg className="diag-trend" viewBox={`0 0 ${trace.width} ${trace.height}`} role="img" aria-label={`原始通道阶梯趋势，${diagnosticTime(trace.first)} 至 ${diagnosticTime(trace.last)}。采样缺口与工况切换处断线。`}>
      {ticks.map((value, index) => {
        const y = trace.bottom - index / 2 * (trace.bottom - trace.top);
        return <g key={index}><line className="diag-grid-line" x1={trace.left} x2={trace.right} y1={y} y2={y}/><text className="diag-axis-label" x={trace.left - 9} y={y + 4} textAnchor="end">{Number(value.toFixed(1))}</text></g>;
      })}
      {trace.markers.map((marker, index) => <line className="diag-mode-marker" key={index} x1={marker.x} x2={marker.x} y1={trace.top} y2={trace.bottom}><title>{diagnosticTime(marker.timestamp)} · {modes[marker.mode] || modes.unknown}</title></line>)}
      {trace.paths.map((path, index) => <path className={`diag-trace-${path.channel.toLowerCase()}`} key={index} d={path.path} fill="none" vectorEffect="non-scaling-stroke"/>)}
      {trace.dots.map(dot => <circle className={`diag-dot-${dot.channel.toLowerCase()}`} key={dot.channel} cx={dot.x} cy={dot.y} r={3}><title>{dot.channel} 原值 {dot.value} · {diagnosticTime(dot.timestamp)}</title></circle>)}
      <text className="diag-axis-label" x={trace.left} y={trace.height - 8}>{diagnosticTime(trace.first)}</text>
      <text className="diag-axis-label" x={trace.right} y={trace.height - 8} textAnchor="end">{diagnosticTime(trace.last)}</text>
    </svg>
    {!!trace.markers.length && <p className="diag-note">工况切换：{trace.markers.slice(-6).map(marker => `${diagnosticTime(marker.timestamp)} ${modes[marker.mode] || modes.unknown}`).join('；')}{trace.markers.length > 6 ? '（仅列最近 6 次）' : ''}。</p>}
  </>}</div>;
}

function DiagnosticsContent() {
  const [minutes, setMinutes] = useState<15 | 60>(60);
  const [data, setData] = useState<DiagnosticView | null>(null);
  const [receivedAt, setReceivedAt] = useState(0);
  const [clock, setClock] = useState(Date.now());
  const [error, setError] = useState('');
  const [downloadError, setDownloadError] = useState('');
  const [downloading, setDownloading] = useState<'json' | 'csv' | null>(null);
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    setData(null);
    setError('');
    const poll = async () => {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 8000);
      try {
        const response = await fetch(`/api/diagnostics?minutes=${minutes}`, { signal: controller.signal, cache: 'no-store' });
        if (!response.ok) throw new Error(response.status === 404 ? '此面板尚未提供诊断接口，请升级面板后重试。' : '诊断查询暂不可用，正在自动重试。');
        const next = await response.json() as DiagnosticView;
        if (next?.schema !== 1 || !next.versions || !next.connection || !next.system || next.observation?.schema !== 1
          || !Array.isArray(next.connection.checks) || !Array.isArray(next.observation.points)) throw new Error('诊断数据格式暂不兼容，请检查面板版本。');
        if (!stopped) { setData(next); setReceivedAt(Date.now()); setClock(Date.now()); setError(''); }
      } catch (cause) {
        if (!stopped) setError(cause instanceof Error && cause.name !== 'AbortError' ? cause.message : '诊断查询超时，正在自动重试。');
      } finally {
        clearTimeout(timeout);
        if (!stopped) timer = setTimeout(poll, 2500);
      }
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); controller?.abort(); };
  }, [minutes]);

  const sinceResponse = Math.max(0, (clock - receivedAt) / 1000);
  const responseFresh = !!data && !error && sinceResponse <= 10;
  const observation = data?.observation;
  const latest = observation?.latest;
  const sampleAge = data && finite(latest?.timestamp) && finite(data.generated_at) ? Math.max(0, data.generated_at - latest.timestamp) + sinceResponse : Infinity;
  const rawFresh = responseFresh && data?.capture_fresh === true && latest?.fresh === true && sampleAge <= 10;
  const connection = data?.connection;
  const system = data?.system;
  const systemFresh = responseFresh && system?.available === true && system.fresh === true
    && finite(connection?.nut_query_age_sec) && connection.nut_query_age_sec + sinceResponse <= 45;
  const versions = data?.versions;
  const counters = Object.entries(connection?.counters || {});
  const points = observation?.points || [];
  const systemValues = Object.entries(system?.values || {});

  async function download(format: 'json' | 'csv') {
    setDownloading(format);
    setDownloadError('');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(`/api/diagnostics/export.${format}?minutes=${minutes}`, { cache: 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error('诊断文件暂时无法下载，请稍后重试。');
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = `us3000-diagnostics-${minutes}min-${new Date().toISOString().replace(/[:.]/g, '-')}.${format}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch {
      setDownloadError('诊断文件暂时无法下载，请稍后重试。');
    } finally { clearTimeout(timeout); setDownloading(null); }
  }

  return <div className="diag-content">
    <div className="diag-intro"><p>查看采集链路、系统上报与原始观测，便于核对版本和排查连接。</p><span>{data ? `最近查询 ${diagnosticTime(data.generated_at)}` : '正在读取诊断信息…'}</span></div>
    {error && <div className="notice diag-error" role="status">{error} 当前诊断读数已隐藏。</div>}
    {!error && data && !responseFresh && <div className="notice" role="status">诊断查询未及时更新，当前读数已隐藏。</div>}
    <div className="diag-top-grid">
      <div className="update-version-stack"><article className="panel diag-card" aria-labelledby="diag-versions-heading">
        <h3 id="diag-versions-heading"><Cpu size={18}/>版本信息</h3>
        <dl className="diag-values">
          <div><dt>面板</dt><dd><BuildValue build={versions?.panel}/></dd></div>
          <div><dt>采集器</dt><dd><BuildValue build={versions?.collector}/></dd></div>
          <div><dt>NUT 驱动</dt><dd>{diagnosticVersion(versions?.nut_driver)}</dd></div>
          <div><dt>NUT 版本</dt><dd>{diagnosticVersion(versions?.nut_version)}</dd></div>
          <div><dt>NUT 子驱动</dt><dd>{diagnosticVersion(versions?.nut_subdriver)}</dd></div>
          <div><dt>UPS 固件</dt><dd>{diagnosticVersion(versions?.ups_firmware)}</dd></div>
          <div><dt>USB 描述符版本</dt><dd>{diagnosticVersion(versions?.usb_device_version)}</dd></div>
          <div><dt>解码版本</dt><dd>{diagnosticCount(versions?.decoder_version)}</dd></div>
        </dl>
        <p className="diag-note">USB 描述符版本与 UPS 固件版本是不同字段；例如 1.00 不能据此解释成固件 V3.3。旧采集器未提供的信息显示为未知。</p>
      </article><CollectorUpdatePanel fallbackCurrent={versions?.collector}/></div>
      <article className="panel diag-card" aria-labelledby="diag-connection-heading">
        <h3 id="diag-connection-heading"><Cable size={18}/>连接检查</h3>
        <p className="diag-note">逐项显示已观测到的连接条件；检查通过不代表电池或设备健康。</p>
        {connection?.checks.length ? <ul className="diag-checks">{connection.checks.map((check, index) => {
          const status = responseFresh && check.status in checkLabels ? check.status : 'unknown';
          return <li key={`${check.id}-${index}`}><details className="diag-check-detail"><summary><strong>{check.label}</strong><span className={`diag-status diag-status-${status}`}>{checkLabels[status]}</span></summary><p>{responseFresh ? check.detail : '等待更新诊断结果。'}</p></details>{(status === 'warning' || status === 'error') && <p className="diag-check-warning">{check.detail}</p>}</li>;
        })}</ul> : <p className="diag-empty">等待采集器连接信息。</p>}
        <dl className="diag-values diag-connection-meta">
          <div><dt>USB 数据时间</dt><dd>{responseFresh && finite(connection?.usb_age_sec) ? elapsedLabel(connection.usb_age_sec + sinceResponse) : '未知'}</dd></div>
          <div><dt>系统查询时间</dt><dd>{responseFresh && finite(connection?.nut_query_age_sec) ? elapsedLabel(connection.nut_query_age_sec + sinceResponse) : '未知'}</dd></div>
          <div><dt>系统与 USB 身份</dt><dd>{connection?.association === 'matched' ? '已匹配' : connection?.association === 'different' ? '不一致' : '尚未核验'}</dd></div>
          <div><dt>系统 pollonly</dt><dd>{connection?.pollonly === true ? '已报告启用' : connection?.pollonly === false ? '已报告关闭' : '未知'}</dd></div>
        </dl>
        {!!counters.length && <details className="diag-details"><summary>采集计数</summary><dl className="diag-values">{counters.map(([key, value]) => <div key={key}><dt>{counterLabels[key] || key}</dt><dd>{diagnosticCount(value)}</dd></div>)}</dl></details>}
      </article>
    </div>
    <article className="panel diag-card" aria-labelledby="diag-system-heading">
      <div className="diag-heading"><h3 id="diag-system-heading"><Radio size={18}/>NAS 系统上报</h3><span className={`diag-status ${systemFresh ? 'diag-status-info' : 'diag-status-unknown'}`}>{systemFresh ? '查询有效 · 独立来源' : '当前系统信息不可用'}</span></div>
      <p className="diag-note">以下来自系统 UPS 服务。与 USB 遥测是否属于同一台设备，以连接检查的身份结果为准。</p>
      <div className="diag-system-grid">
        <div><span className="diag-label">系统状态原值</span><strong className="diag-system-value">{systemFresh ? system?.status_raw || '未提供' : '—'}</strong>
          {systemFresh && !!system?.notices?.length && <div className="diag-notices">{system.notices.map((notice, index) => <span key={`${notice.code}-${index}`} className={`diag-status ${notice.level === 'warning' ? 'diag-status-warning' : 'diag-status-info'}`}>{notice.label}</span>)}</div>}
          <p className="diag-note">{systemFresh ? '系统状态按上报显示，不据此推断电芯温度或健康度。' : '等待有效的系统查询，不以旧状态判断当前情况。'}</p>
        </div>
        <div><span className="diag-label">系统续航原值</span><strong className="diag-system-value">{systemFresh ? system?.runtime?.raw ?? '未提供' : '—'}</strong><p className="diag-note">{systemFresh ? runtimeObservation(system?.runtime?.raw) : '当前未显示续航原值。'}{systemFresh && system?.runtime?.label ? ` ${system.runtime.label}` : ''}</p></div>
        <div><span className="diag-label">系统阈值</span><dl className="diag-values diag-thresholds"><div><dt>低电量</dt><dd>{systemFresh ? thresholdLabel(system?.thresholds?.charge_low, '%') : '—'}</dd></div><div><dt>低续航</dt><dd>{systemFresh ? thresholdLabel(system?.thresholds?.runtime_low_sec, '秒') : '—'}</dd></div></dl><p className="diag-note">仅展示系统报告的配置，不代表 NAS 最终关机策略。</p></div>
      </div>
      <div className="diag-alarm"><span>系统告警原文</span><p>{systemFresh ? system?.alarm_text || '当前查询未提供告警原文。' : '当前系统查询不可用。'}</p></div>
      {!!systemValues.length && <details className="diag-details"><summary>查看系统字段原值{!systemFresh ? '（最近一次查询，非当前值）' : ''}</summary><dl className="diag-values diag-system-raw">{systemValues.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl></details>}
    </article>
    <details className="panel diag-card diag-observation" aria-labelledby="diag-observation-heading">
      <summary><span id="diag-observation-heading"><Activity size={18}/>原始通道短时观测</span><span className="diag-summary-note">疑似温度，未验证</span></summary>
      <p className="diag-note">通道 A / B 分别是从 0 开始计数的单字节 26 / 27。物理量、单位和测点均未确认，不能当作电池温度、板温或温度探头。</p>
      <div className="diag-observation-head"><div className="diag-channel-values"><div><span>A · 字节 26</span><strong>{rawChannelLabel(latest?.byte_26, rawFresh)}</strong><small>无单位原值</small></div><div><span>B · 字节 27</span><strong>{rawChannelLabel(latest?.byte_27, rawFresh)}</strong><small>无单位原值</small></div></div><div className="range diag-range" aria-label="原始观测时间范围">{([15, 60] as const).map(value => <button key={value} aria-pressed={minutes === value} className={minutes === value ? 'active' : ''} onClick={() => setMinutes(value)}>{value} 分钟</button>)}</div></div>
      <p className="diag-note">{rawFresh ? `${modes[latest?.mode || 'unknown'] || modes.unknown} · 最新读数 ${diagnosticTime(latest?.timestamp)}${latest?.observed === false ? ' · 正等待进入观测窗口' : ''}` : '当前原始通道数据不可用或已失鲜；下方仅保留已有观测。'}</p>
      <RawTrend points={points}/>
      <div className="diag-observation-stats"><span>观测样本 <strong>{diagnosticCount(observation?.count)}</strong></span><span>采样缺口 <strong>{diagnosticCount(observation?.gap_count)}</strong></span><span>冲突 / 拒绝 <strong>{diagnosticCount(observation?.conflict_count)} / {diagnosticCount(observation?.rejected_count)}</strong></span></div>
      <p className="diag-note">{observation?.count ? `实际记录 ${diagnosticTime(observation.first_timestamp, true)} 至 ${diagnosticTime(observation.last_timestamp, true)}。` : '尚无已记录样本。'}当前显示最近 {minutes} 分钟；后台最多保留 60 分钟、4096 点内存观测，重启后重新开始，无需浏览器持续打开。阶梯线不平滑，采样缺口、身份变化与工况切换处断开，不补齐缺失时段。{observation?.truncated ? '当前窗口已受样本上限限制，仅包含保留的部分。' : ''}</p>
    </details>
    <article className="panel diag-card diag-export" aria-labelledby="diag-export-heading">
      <h3 id="diag-export-heading"><ArrowDownToLine size={18}/>下载诊断记录</h3>
      <p className="diag-note">下载最近 {minutes} 分钟内实际保留的观测。JSON 包含版本、连接检查和允许导出的系统字段；CSV 提供同一时间范围的观测明细。文件只下载到本机。</p>
      <details className="diag-details"><summary><Info size={15}/>查看导出范围</summary><p className="diag-note">包含采样时间、随机设备别名、已知工况和状态代码、字节 26 / 27 / 28、相关电压、电流、电量与计算版本。仅使用后端固定白名单。</p><p className="diag-note">不包含设备序列号、地址、USB 路径、NUT 目标、自由告警文字或完整原始报文。下载不会上传数据，也不会控制 UPS。</p></details>
      <div className="diag-export-actions">{(['json', 'csv'] as const).map(format => <button key={format} className="diag-button" disabled={!data || downloading !== null} onClick={() => void download(format)}><ArrowDownToLine size={16}/>{downloading === format ? '正在下载…' : `下载 ${format.toUpperCase()}`}</button>)}</div>
      {downloadError && <p className="diag-download-error" role="status">{downloadError}</p>}
    </article>
  </div>;
}

export default function DiagnosticsPanel() {
  return <DiagnosticsBoundary><DiagnosticsContent/></DiagnosticsBoundary>;
}
