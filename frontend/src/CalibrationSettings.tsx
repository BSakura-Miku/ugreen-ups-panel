import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Check, RefreshCw, SlidersHorizontal } from 'lucide-react';
import type { CalibrationCoefficients, CalibrationConfig, CalibrationProfile, CalibrationState } from './types';

export function calibrationLabel(profile?: string) {
  return profile === 'custom' ? '自定义 · 未独立验证' : profile === 'local-19v-v1' ? '开发样机 · 19 V v1' : profile === 'none' ? '未启用' : '等待采集器确认';
}

const fields = [
  { key: 'base_gain', label: '交流基底', range: '大于 0，且不超过 10' },
  { key: 'charge_gain', label: '回充补偿', range: '0 至 10' },
  { key: 'battery_gain', label: '电池放电', range: '大于 0，且不超过 10' },
] as const;
type CoefficientValues = Record<keyof CalibrationCoefficients, string>;
type Draft = { profile: CalibrationProfile; values: CoefficientValues; displayValues: CoefficientValues; revision: string };
function formatCoefficient(value: number): string {
  const rounded = Number(value.toFixed(4));
  return rounded === 0 && value !== 0 ? value.toExponential(3).replace(/\.?0+e/, 'e') : String(rounded);
}
function draftFrom(config: CalibrationConfig, defaults: CalibrationCoefficients): Draft {
  const values = config.coefficients || defaults;
  return {
    profile: config.profile,
    // Keep the full value until the user edits this field; display rounding must not change a saved coefficient.
    values: { base_gain: String(values.base_gain), charge_gain: String(values.charge_gain), battery_gain: String(values.battery_gain) },
    displayValues: { base_gain: formatCoefficient(values.base_gain), charge_gain: formatCoefficient(values.charge_gain), battery_gain: formatCoefficient(values.battery_gain) },
    revision: config.revision,
  };
}

class CalibrationRequestError extends Error {
  constructor(message: string, readonly status?: number) { super(message); }
}
async function requestCalibration(parentSignal: AbortSignal, init?: RequestInit): Promise<CalibrationState> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  let timedOut = false;
  parentSignal.addEventListener('abort', abort, { once: true });
  if (parentSignal.aborted) abort();
  const timer = setTimeout(() => { timedOut = true; abort(); }, 8000);
  try {
    const response = await fetch('/api/calibration', { ...init, signal: controller.signal, cache: 'no-store' });
    const body = await response.json().catch(() => null);
    if (!response.ok) throw new CalibrationRequestError(typeof body?.detail === 'string' ? body.detail : `配置请求失败（HTTP ${response.status}）`, response.status);
    if (!body || body.schema !== 1 || !body.desired || !body.defaults) throw new CalibrationRequestError('配置响应无效，请稍后重试。');
    return body;
  } catch (error) {
    if (timedOut) throw new CalibrationRequestError(init?.method === 'PUT' ? '请求超时，保存结果尚未确认。请重新载入核对。' : '读取配置超时，将自动重试。');
    if (!parentSignal.aborted && !(error instanceof CalibrationRequestError)) throw new CalibrationRequestError(init?.method === 'PUT' ? '连接中断，保存结果尚未确认。请重新载入核对。' : '配置连接中断，将自动重试。');
    throw error;
  } finally {
    clearTimeout(timer);
    parentSignal.removeEventListener('abort', abort);
  }
}

export default function CalibrationSettings() {
  const [data, setData] = useState<CalibrationState | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<'save' | 'reload' | null>(null);
  const [pollError, setPollError] = useState('');
  const [actionError, setActionError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [savedRevision, setSavedRevision] = useState<string | null>(null);
  const [receivedAt, setReceivedAt] = useState<number | null>(null);
  const [clock, setClock] = useState(() => performance.now());
  const lifetime = useRef(new AbortController());
  const initialized = useRef(false);
  const busyRef = useRef(false);
  const requestVersion = useRef(0);

  useEffect(() => {
    lifetime.current = new AbortController();
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    async function update() {
      const version = requestVersion.current;
      try {
        if (busyRef.current) return;
        const next = await requestCalibration(lifetime.current.signal);
        if (!stopped && version === requestVersion.current) {
          setData(next); setPollError(''); setReceivedAt(performance.now());
          // Polling updates the live report, never a form the user is editing.
          if (!initialized.current) { setDraft(draftFrom(next.desired, next.defaults)); initialized.current = true; }
        }
      } catch (error) {
        if (!stopped && version === requestVersion.current) setPollError(error instanceof Error ? error.message : '读取配置失败，将自动重试。');
      } finally { if (!stopped) timer = setTimeout(update, 2000); }
    }
    update();
    const tick = setInterval(() => setClock(performance.now()), 1000);
    return () => { stopped = true; lifetime.current.abort(); clearTimeout(timer); clearInterval(tick); };
  }, []);

  function edit(next: Draft) { setDraft(next); setDirty(true); setActionError(''); }

  async function reload() {
    if (busyRef.current) return;
    busyRef.current = true; requestVersion.current++; setBusy('reload'); setActionError('');
    try {
      const next = await requestCalibration(lifetime.current.signal);
      if (lifetime.current.signal.aborted) return;
      setData(next); setReceivedAt(performance.now()); setDraft(draftFrom(next.desired, next.defaults)); initialized.current = true;
      setDirty(false); setConflict(false); setPollError(''); setSavedRevision(null);
    } catch (error) { if (!lifetime.current.signal.aborted) { const message = error instanceof Error ? error.message : '重新载入失败。'; setActionError(message); setPollError(message); } }
    finally { busyRef.current = false; if (!lifetime.current.signal.aborted) setBusy(null); }
  }

  function reset() {
    if (!data || busyRef.current) return;
    setDraft(draftFrom(data.desired, data.defaults)); setDirty(false); setActionError(''); setConflict(false);
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!draft || !data?.collector_ready || busyRef.current || conflict || pollError || receivedAt === null || performance.now() - receivedAt > 10000) return;
    let coefficients: CalibrationCoefficients | null = null;
    if (draft.profile === 'custom') {
      coefficients = { base_gain: 0, charge_gain: 0, battery_gain: 0 };
      for (const field of fields) {
        const input = draft.values[field.key].trim();
        const value = Number(input);
        if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(input) || !Number.isFinite(value) || value > 10 || (field.key === 'charge_gain' ? value < 0 : value <= 0)) {
          setActionError(`${field.label}系数需为${field.range}的数字。`);
          document.getElementById(`calibration-${field.key}`)?.focus();
          return;
        }
        coefficients[field.key] = value;
      }
    }
    busyRef.current = true; requestVersion.current++; setBusy('save'); setActionError(''); setSavedRevision(null);
    try {
      const next = await requestCalibration(lifetime.current.signal, {
        method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-UPS-Calibration': '1' },
        body: JSON.stringify({ profile: draft.profile, coefficients, expected_revision: draft.revision }),
      });
      if (lifetime.current.signal.aborted) return;
      setData(next); setDraft(draftFrom(next.desired, next.defaults)); setDirty(false); setConflict(false); setPollError('');
      setSavedRevision(next.desired.revision);
    } catch (error) {
      if (!lifetime.current.signal.aborted) {
        setActionError(error instanceof Error ? error.message : '保存结果尚未确认，请重新载入核对。');
        if (error instanceof CalibrationRequestError && error.status === 409) setConflict(true);
      }
    } finally { busyRef.current = false; if (!lifetime.current.signal.aborted) setBusy(null); }
  }

  const active = data?.active;
  const reportFresh = !pollError && receivedAt !== null && Math.max(0, clock - receivedAt) <= 10000;
  const collectorReady = reportFresh && !!data?.collector_ready;
  const changedElsewhere = !!data && !!draft && data.desired.revision !== draft.revision;
  const confirmed = !!savedRevision && collectorReady && data?.active?.revision === savedRevision && data.desired.revision === savedRevision;
  const replaced = !!savedRevision && !!data && data.desired.revision !== savedRevision;
  const waiting = !!data && (data.pending || (!!savedRevision && data.active?.revision !== savedRevision && !replaced));
  return <section className="calibration-page" aria-label="功率校准设置">
    <article className="panel calibration-active">
      <div className="panel-heading"><div><h3>{collectorReady ? '当前已生效' : data ? '最近一次采集器报告' : '采集器报告'}</h3><p>这里显示采集器报告的实际配置。</p></div><span className="calibration-badge">{active ? calibrationLabel(active.profile) : '尚未确认'}</span></div>
      {active?.coefficients ? <><dl className="calibration-values">{fields.map(field => <div key={field.key}><dt>{field.label}</dt><dd title={String(active.coefficients![field.key])}>{formatCoefficient(active.coefficients![field.key])}</dd></div>)}</dl><p className="muted">系数最多显示 4 位小数，计算保留完整精度。</p></> : <p className="muted">{active?.profile === 'none' ? '功率估算未启用，当前没有生效的校准系数。' : '等待采集器返回实际配置与系数。'}</p>}
      {active?.profile === 'custom' && <p className="calibration-caution">自定义系数未经过独立验证，估算值仅供参考。</p>}
      {data && !reportFresh && <p className="notice" role="status">配置连接中断或报告已过期，暂不能确认当前配置或保存。上方保留最近一次报告，正在编辑的内容不会丢失。</p>}
      {data && reportFresh && !data.collector_ready && <p className="notice" role="status">当前无法确认采集器可用，暂不能保存。请更新宿主机采集器；已更新时请等待 UPS 连接恢复。{active ? ' 上方为最近一次报告的配置。' : ''}</p>}
      {data?.error && <p className="notice" role="status">{data.error}</p>}
      <div className="calibration-feedback" role="status" aria-live="polite">
        {busy === 'save' ? '正在保存配置…' : replaced ? '保存后配置又发生变化，请查看当前配置并重新载入。' : confirmed ? <span className="calibration-success"><Check size={16} />采集器已确认，新配置已生效。</span> : waiting ? `配置已保存，等待采集器生效（${calibrationLabel(data?.desired.profile)}）…` : data ? '' : '正在读取配置…'}
      </div>
    </article>

    <article className="panel calibration-editor">
      <div className="panel-heading"><div><h3><SlidersHorizontal size={17} />调整配置</h3><p>修改只影响功率估算，不改变原始电流或 NAS 的 UPS 保护。</p></div>{dirty && <span className="calibration-draft">有未保存修改</span>}</div>
      {pollError && <p className="notice" role="status">{pollError}</p>}
      {actionError && <p className="notice" role="alert">{actionError}</p>}
      {draft && data ? <form onSubmit={save} noValidate>
        <fieldset disabled={!!busy} className="calibration-fields">
          <label htmlFor="calibration-profile">校准方式</label>
          <select id="calibration-profile" value={draft.profile} onChange={event => edit({ ...draft, profile: event.target.value as CalibrationProfile })}>
            <option value="none">未启用</option><option value="local-19v-v1">开发样机 · 19 V v1</option><option value="custom">自定义系数</option>
          </select>
          <p className="muted">{draft.profile === 'none' ? '保存后停止功率估算，电量、电压和设备电流继续显示。' : draft.profile === 'custom' ? '自定义系数未独立验证。下方为待保存数值，保存并经采集器确认后才会生效。' : '使用开发样机的经验参数；相同型号也可能存在差异。保存并经采集器确认后才会生效。'}</p>
          {draft.profile !== 'none' && <div className="calibration-inputs">{fields.map(field => <div className="calibration-field" key={field.key}>
            <label htmlFor={`calibration-${field.key}`}>{field.label}</label>
            <input id={`calibration-${field.key}`} type="text" inputMode="decimal" autoComplete="off" spellCheck={false} aria-describedby={`calibration-${field.key}-hint`} readOnly={draft.profile !== 'custom'} value={draft.profile === 'local-19v-v1' ? formatCoefficient(data.defaults[field.key]) : draft.displayValues[field.key]} onChange={event => edit({ ...draft, values: { ...draft.values, [field.key]: event.target.value }, displayValues: { ...draft.displayValues, [field.key]: event.target.value } })} />
            <span id={`calibration-${field.key}-hint`} className="muted">{field.range}</span>
          </div>)}</div>}
          <div className="calibration-actions"><button type="button" className="calibration-secondary" onClick={() => edit(draftFrom({ ...data.desired, profile: 'custom', coefficients: data.defaults, revision: draft.revision }, data.defaults))}>使用样机值填充</button><span className="muted">仅填入表单，需要另行保存。</span></div>
        </fieldset>
        {(conflict || changedElsewhere) && <p className="notice" role="status">配置已发生变化，当前输入已保留。请重新载入最新配置后再调整。</p>}
        <div className="calibration-actions calibration-save-actions">
          <button className="calibration-primary" type="submit" disabled={!dirty || !!busy || !collectorReady || conflict || changedElsewhere}>{busy === 'save' ? '正在保存…' : '保存配置'}</button>
          <button className="calibration-secondary" type="button" disabled={!dirty || !!busy} onClick={reset}>重置未保存内容</button>
          <button className="calibration-secondary" type="button" disabled={!!busy} onClick={reload}><RefreshCw size={14} />{busy === 'reload' ? '正在载入…' : '重新载入'}</button>
        </div>
      </form> : <button className="calibration-secondary" type="button" onClick={reload} disabled={!!busy}>重新载入</button>}
    </article>

    <article className="panel calibration-formulas"><h3>系数如何参与估算</h3>
      <p>交流输入功率 ≈ 交流基底系数 × 设备基底读数 + 回充补偿系数 × 电池充电功率原值</p>
      <p>电池放电功率 ≈ 电池放电系数 × 电池电压 × 设备放电电流</p>
      <p className="muted">两项估算都采用约 8 秒平滑。交流估算仅在外部供电、适配器输入为 18–20 V 时提供；未充电时回充项为 0。电池放电估算仅在电池供电时提供。切换配置或供电状态后，需要等待读数稳定。</p>
      <p className="muted">交流输入包含适配器损耗与回充，电池放电对应电池端；两者都不等于 NAS 输出功率。系数是经验参数，并非转换效率。</p>
    </article>
  </section>;
}
