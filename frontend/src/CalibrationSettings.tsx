import { useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { Check, RefreshCw, SlidersHorizontal } from 'lucide-react';
import type { CalibrationProfile, CalibrationState, CalibrationVoltage, LiveView } from './types';
import { applySuggestion, calibrationPayload, chooseVoltage, coefficientSource, confirmVoltage, createDraft, editCoefficient, formatCoefficient, presetDraft } from './calibrationDraft';
import type { AssistantSuggestion, CalibrationDraft, CoefficientKey } from './calibrationDraft';
import { suggestVoltage } from './calibrationSampler';
import CalibrationAssistant, { VoltageSelector } from './CalibrationAssistant';

export function calibrationLabel(profile?: string) {
  return profile === 'custom' ? '自定义 · 未独立验证' : profile === 'local-19v-v1' ? '开发样机 · 19 V v1' : profile === 'none' ? '未启用' : '等待采集器确认';
}

const fields = [
  { key: 'base_gain', label: '交流基底', range: '大于 0，且不超过 10' },
  { key: 'charge_gain', label: '回充补偿', range: '0 至 10' },
  { key: 'battery_gain', label: '电池放电', range: '大于 0，且不超过 10' },
] as const;

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
    const headers = new Headers(init?.headers);
    headers.set('X-UPS-Calibration-Version', '2');
    const response = await fetch('/api/calibration', { ...init, headers, signal: controller.signal, cache: 'no-store' });
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

export default function CalibrationSettings({ live, liveFresh, liveAge }: { live: LiveView | null; liveFresh: boolean; liveAge: number | null }) {
  const [data, setData] = useState<CalibrationState | null>(null);
  const [draft, setDraft] = useState<CalibrationDraft | null>(null);
  const [dirty, setDirty] = useState(false);
  const [assistantReset, setAssistantReset] = useState(0);
  const [fillNotice, setFillNotice] = useState('');
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
          if (!initialized.current) { setDraft(createDraft(next.desired)); initialized.current = true; }
        }
      } catch (error) {
        if (!stopped && version === requestVersion.current) setPollError(error instanceof Error ? error.message : '读取配置失败，将自动重试。');
      } finally { if (!stopped) timer = setTimeout(update, 2000); }
    }
    update();
    const tick = setInterval(() => setClock(performance.now()), 1000);
    return () => { stopped = true; lifetime.current.abort(); clearTimeout(timer); clearInterval(tick); };
  }, []);

  const supportsV2 = !!data?.supported_config_schemas?.includes(2);
  useEffect(() => {
    if (!supportsV2 || !liveFresh || draft?.voltage !== null) return;
    const voltage = suggestVoltage(live?.sample?.adapter_input_voltage_v);
    if (voltage !== null) setDraft(current => current?.voltage === null ? { ...current, voltage } : current);
  }, [supportsV2, liveFresh, live?.sample?.adapter_input_voltage_v, draft?.voltage]);

  function edit(next: CalibrationDraft) { setDraft(next); setDirty(true); setActionError(''); setFillNotice(''); }
  function selectVoltage(voltage: CalibrationVoltage) {
    if (!draft) return;
    edit(chooseVoltage(draft, voltage)); setAssistantReset(value => value + 1);
  }
  function acceptVoltage() {
    if (!draft) return;
    edit(confirmVoltage(draft)); setAssistantReset(value => value + 1);
  }
  function changeCoefficient(key: CoefficientKey, input: string) {
    if (!draft) return;
    if (key === 'base_gain' && Number(input) !== Number(draft.values.base_gain)) setAssistantReset(value => value + 1);
    edit(editCoefficient(draft, key, input));
  }
  function fillAssistant(suggestion: AssistantSuggestion) {
    if (!draft || !supportsV2 || !data?.collector_ready || busyRef.current || conflict || data.desired.revision !== draft.revision || pollError || receivedAt === null || performance.now() - receivedAt > 10000 || !liveFresh || liveAge === null || liveAge > 5) return;
    edit(applySuggestion(draft, suggestion));
    setFillNotice('已填入下方待保存的自定义配置，请核对后点击“保存配置”。');
    document.getElementById('calibration-editor-heading')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function reload() {
    if (busyRef.current) return;
    busyRef.current = true; requestVersion.current++; setBusy('reload'); setActionError('');
    try {
      const next = await requestCalibration(lifetime.current.signal);
      if (lifetime.current.signal.aborted) return;
      setData(next); setReceivedAt(performance.now()); setDraft(createDraft(next.desired)); initialized.current = true;
      setDirty(false); setConflict(false); setPollError(''); setSavedRevision(null); setAssistantReset(value => value + 1); setFillNotice('');
    } catch (error) { if (!lifetime.current.signal.aborted) { const message = error instanceof Error ? error.message : '重新载入失败。'; setActionError(message); setPollError(message); } }
    finally { busyRef.current = false; if (!lifetime.current.signal.aborted) setBusy(null); }
  }

  function reset() {
    if (!data || busyRef.current) return;
    setDraft(createDraft(data.desired)); setDirty(false); setActionError(''); setConflict(false); setAssistantReset(value => value + 1); setFillNotice('');
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!draft || !data?.collector_ready || busyRef.current || conflict || data.desired.revision !== draft.revision || pollError || receivedAt === null || performance.now() - receivedAt > 10000) return;
    const result = calibrationPayload(draft, supportsV2);
    if (!result.payload) {
      setActionError(result.error || '请检查校准配置。');
      document.getElementById(result.field === 'voltage' ? 'manual-voltage' : `calibration-${result.field}`)?.focus();
      return;
    }
    busyRef.current = true; requestVersion.current++; setBusy('save'); setActionError(''); setSavedRevision(null);
    try {
      const next = await requestCalibration(lifetime.current.signal, {
        method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-UPS-Calibration': '1' },
        body: JSON.stringify(result.payload),
      });
      if (lifetime.current.signal.aborted) return;
      setData(next); setReceivedAt(performance.now()); setDraft(createDraft(next.desired)); setDirty(false); setConflict(false); setPollError('');
      setSavedRevision(next.desired.revision); setFillNotice(''); setAssistantReset(value => value + 1);
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
  const assistantEnabled = collectorReady && supportsV2 && !busy && !conflict && !changedElsewhere;
  const waiting = !!data && (data.pending || (!!savedRevision && data.active?.revision !== savedRevision && !replaced));
  return <section className="calibration-page" aria-label="功率校准设置">
    <article className="panel calibration-active">
      <div className="panel-heading"><div><h3>{collectorReady ? '当前已生效' : data ? '最近一次采集器报告' : '采集器报告'}</h3><p>这里显示采集器报告的实际配置。</p></div><span className="calibration-badge">{active ? calibrationLabel(active.profile) : '尚未确认'}</span></div>
      {active?.coefficients ? <><dl className="calibration-values">{fields.map(field => <div key={field.key}><dt>{field.label}</dt><dd title={active.coefficients![field.key] === null ? '未校准' : String(active.coefficients![field.key])}>{formatCoefficient(active.coefficients![field.key])}</dd></div>)}</dl><p className="muted">系数最多显示 4 位小数，计算保留完整精度。</p></> : <p className="muted">{active?.profile === 'none' ? '功率估算未启用，当前没有生效的校准系数。' : '等待采集器返回实际配置与系数。'}</p>}
      {active?.profile !== 'none' && active && <p className="muted">{active.schema === 2 ? `适配器档位 ${active.ac_voltage_nominal_v} V · 交流采样范围 ${active.ac_voltage_nominal_v - 1}–${active.ac_voltage_nominal_v + 1} V` : '旧配置 · 交流范围 18–20 V；原有参数及行为保持不变。'}</p>}
      {active?.profile === 'custom' && <p className="calibration-caution">自定义系数未经过独立验证，估算值仅供参考。</p>}
      {data && !reportFresh && <p className="notice" role="status">配置连接中断或报告已过期，暂不能确认当前配置或保存。上方保留最近一次报告，正在编辑的内容不会丢失。</p>}
      {data && reportFresh && !data.collector_ready && <p className="notice" role="status">当前无法确认采集器可用，暂不能保存。请更新宿主机采集器；已更新时请等待 UPS 连接恢复。{active ? ' 上方为最近一次报告的配置。' : ''}</p>}
      {data?.error && <p className="notice" role="status">{data.error}</p>}
      <div className="calibration-feedback" role="status" aria-live="polite">
        {busy === 'save' ? '正在保存配置…' : replaced ? '保存后配置又发生变化，请查看当前配置并重新载入。' : confirmed ? <span className="calibration-success"><Check size={16} />采集器已确认，新配置已生效。</span> : waiting ? `配置已保存，等待采集器生效（${calibrationLabel(data?.desired.profile)}）…` : data ? '' : '正在读取配置…'}
      </div>
    </article>

    {draft && data && <CalibrationAssistant key={assistantReset} live={live} liveFresh={liveFresh} liveAge={liveAge} enabled={assistantEnabled} supportsV2={supportsV2} draft={draft} active={active || null} onVoltageChange={selectVoltage} onConfirmVoltage={acceptVoltage} onApply={fillAssistant} />}

    <article className="panel calibration-editor">
      <div className="panel-heading"><div><h3 id="calibration-editor-heading"><SlidersHorizontal size={17} />待保存配置</h3><p>修改只影响功率估算，不改变原始电流或 NAS 的 UPS 保护。</p></div>{dirty && <span className="calibration-draft">有未保存修改</span>}</div>
      {pollError && <p className="notice" role="status">{pollError}</p>}
      {actionError && <p className="notice" role="alert">{actionError}</p>}
      {fillNotice && <p className="calibration-fill-notice" role="status">{fillNotice}</p>}
      {draft && data ? <form onSubmit={save} noValidate>
        <fieldset disabled={!!busy} className="calibration-fields">
          <label htmlFor="calibration-profile">校准方式</label>
          <select id="calibration-profile" value={draft.profile} onChange={event => { edit({ ...draft, profile: event.target.value as CalibrationProfile }); setAssistantReset(value => value + 1); }}>
            <option value="none">未启用</option><option value="local-19v-v1">开发样机 · 19 V v1</option><option value="custom">自定义系数</option>
          </select>
          <p className="muted">{draft.profile === 'none' ? '保存后停止功率估算，电量、电压和设备电流继续显示。' : draft.profile === 'custom' ? '自定义系数未独立验证。下方为待保存数值，保存并经采集器确认后才会生效。' : '使用开发样机的 19 V 经验参数；相同型号也可能存在差异。保存并经采集器确认后才会生效。'}</p>
          {draft.profile === 'custom' && (supportsV2 ? <VoltageSelector id="manual-voltage" voltage={draft.voltage} confirmed={draft.voltageConfirmed} inputVoltage={liveFresh ? live?.sample?.adapter_input_voltage_v : null} disabled={!assistantEnabled} onChange={selectVoltage} onConfirm={acceptVoltage} /> : <p className="muted">当前采集器仅支持旧配置：范围 18–20 V，三个系数均必填。更新宿主机采集器后可使用电压档位与分步校准。</p>)}
          {draft.profile !== 'none' && <div className="calibration-inputs">{fields.map(field => <div className="calibration-field" key={field.key}>
            <label htmlFor={`calibration-${field.key}`}>{field.label}{draft.profile === 'custom' && supportsV2 && <span className="muted">{field.key === 'base_gain' ? ' · 必填' : ' · 可留空'}</span>}</label>
            <input id={`calibration-${field.key}`} type="text" inputMode="decimal" autoComplete="off" spellCheck={false} aria-describedby={`calibration-${field.key}-hint`} readOnly={draft.profile !== 'custom' || (supportsV2 && !draft.voltageConfirmed)} placeholder={field.key === 'base_gain' ? '填写或由助手计算' : supportsV2 ? '留空表示未校准' : '旧配置需要填写'} value={draft.profile === 'local-19v-v1' ? formatCoefficient(data.defaults[field.key]) : draft.displayValues[field.key]} onChange={event => changeCoefficient(field.key, event.target.value)} />
            <span id={`calibration-${field.key}-hint`} className="muted">{field.range}{supportsV2 && draft.profile === 'custom' && field.key !== 'base_gain' ? '；未校准可留空' : ''}</span>
            <span className="calibration-source">{draft.profile === 'local-19v-v1' ? '开发样机 19 V 预设值' : coefficientSource(draft, field.key)}</span>
          </div>)}</div>}
          <div className="calibration-actions"><button type="button" className="calibration-secondary" onClick={() => { edit(presetDraft(draft, data.defaults, supportsV2)); setAssistantReset(value => value + 1); }}>使用 19 V 样机值填充</button><span className="muted">仅填入表单，需要另行保存。</span></div>
        </fieldset>
        {(conflict || changedElsewhere) && <p className="notice" role="status">配置已发生变化，当前输入已保留。请重新载入最新配置后再调整。</p>}
        <div className="calibration-actions calibration-save-actions">
          <button className="calibration-primary" type="submit" disabled={!dirty || !!busy || !collectorReady || conflict || changedElsewhere || (draft.profile === 'custom' && draft.schema === 2 && !supportsV2)}>{busy === 'save' ? '正在保存…' : '保存配置'}</button>
          <button className="calibration-secondary" type="button" disabled={!dirty || !!busy} onClick={reset}>重置未保存内容</button>
          <button className="calibration-secondary" type="button" disabled={!!busy} onClick={reload}><RefreshCw size={14} />{busy === 'reload' ? '正在载入…' : '重新载入'}</button>
        </div>
      </form> : <button className="calibration-secondary" type="button" onClick={reload} disabled={!!busy}>重新载入</button>}
    </article>

    <details className="panel calibration-formulas"><summary>系数如何参与估算</summary>
      <p>交流输入功率 ≈ 交流基底系数 × 设备基底读数 + 回充补偿系数 × 电池充电功率原值</p>
      <p>电池放电功率 ≈ 电池放电系数 × 电池电压 × 设备放电电流</p>
      <p className="muted">两项估算都采用约 8 秒平滑。自定义交流估算按所选 12 / 19 / 20 V 档位、额定值 ±1 V 范围工作；旧配置仍使用 18–20 V。未充电时回充项为 0；回充或电池放电系数留空时，对应估算显示尚未校准。切换配置或供电状态后，需要等待读数稳定。</p>
      <p className="muted">交流输入包含适配器损耗与回充，电池放电对应电池端；两者都不等于 NAS 输出功率。系数是经验参数，并非转换效率。</p>
    </details>
  </section>;
}
