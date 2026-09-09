import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, Check, FlaskConical, Play, RotateCcw } from 'lucide-react';
import type { CalibrationConfig, CalibrationVoltage, LiveView } from './types';
import { applySuggestion, coefficientSource, formatCoefficient } from './calibrationDraft';
import type { AssistantSuggestion, CalibrationDraft, CoefficientKey } from './calibrationDraft';
import { advanceSampling, calculateBase, calculateCharge, createSamplingState } from './calibrationSampler';
import type { SamplingFrame, SamplingState } from './calibrationSampler';

type VoltageProps = {
  id: string;
  voltage: CalibrationVoltage | null;
  confirmed: boolean;
  inputVoltage: number | null | undefined;
  disabled: boolean;
  onChange: (voltage: CalibrationVoltage) => void;
  onConfirm: () => void;
};

export function VoltageSelector({ id, voltage, confirmed, inputVoltage, disabled, onChange, onConfirm }: VoltageProps) {
  return <div className="calibration-voltage">
    <label htmlFor={id}>适配器额定电压</label>
    <div className="calibration-voltage-controls">
      <select id={id} value={voltage ?? ''} disabled={disabled} onChange={event => onChange(Number(event.target.value) as CalibrationVoltage)}>
        <option value="" disabled>选择 12 / 19 / 20 V</option>
        <option value="12">12 V</option><option value="19">19 V</option><option value="20">20 V</option>
      </select>
      <button type="button" className="calibration-secondary" disabled={disabled || voltage === null || confirmed} onClick={onConfirm}>{confirmed ? <><Check size={14} />档位已确认</> : '确认此档位'}</button>
    </div>
    <p className="muted">{typeof inputVoltage === 'number' && Number.isFinite(inputVoltage) ? `当前适配器输入 ${inputVoltage.toFixed(2)} V。` : '当前没有可用的适配器输入电压。'}{confirmed && voltage !== null ? `采样范围 ${voltage - 1}–${voltage + 1} V。` : '预选仅参考适配器输入，请对照铭牌确认或纠正后继续。'}</p>
  </div>;
}

const readableTime = (timestamp: number) => new Date(timestamp * 1000).toLocaleTimeString('zh-CN', { hour12: false });
const showRaw = (value: number) => Number(value.toFixed(3)).toString();
const readWatts = (input: string) => /^[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(input.trim()) ? Number(input) : NaN;

function SamplingProgress({ state }: { state: SamplingState | null }) {
  if (!state) return null;
  return <div className={`calibration-sampling ${state.phase}`}>
    <div className="calibration-progress-label"><span>{state.phase === 'complete' ? '采样完成 · 窗口已冻结' : state.phase === 'rejected' ? '本次采样未通过' : '正在采样'}</span><strong>{Math.min(30, Math.floor(state.elapsed))} / 30 秒 · {state.count} 条</strong></div>
    <progress max={30} value={Math.min(30, state.elapsed)} aria-label={`${state.mode === 'online' ? '交流基底' : '回充补偿'}采样进度`} />
    {state.reason && <p className={state.phase === 'rejected' ? 'calibration-result-error' : 'muted'} role="status">{state.reason}</p>}
    {state.window && <div className="calibration-window">
      <p>{readableTime(state.window.first)}–{readableTime(state.window.last)} · {state.window.count} 条独立采样</p>
      <p>平均基底读数 {showRaw(state.window.meanBase)} · 波动 {(state.window.baseSpread * 100).toFixed(1)}%</p>
      {state.window.meanCharge !== null && <p>平均回充原始功率 {showRaw(state.window.meanCharge)} W · 波动 {((state.window.chargeSpread ?? 0) * 100).toFixed(1)}%</p>}
      <p>数值已冻结；调整同期插座功率只重算系数。</p>
    </div>}
  </div>;
}

type Props = {
  live: LiveView | null;
  liveFresh: boolean;
  liveAge: number | null;
  enabled: boolean;
  disabledReason?: string;
  supportsV2: boolean;
  draft: CalibrationDraft;
  active: CalibrationConfig | null;
  onVoltageChange: (voltage: CalibrationVoltage) => void;
  onConfirmVoltage: () => void;
  onApply: (suggestion: AssistantSuggestion) => void;
};

export default function CalibrationAssistant({ live, liveFresh, liveAge, enabled, supportsV2, disabledReason, draft, active, onVoltageChange, onConfirmVoltage, onApply }: Props) {
  const [baseSampling, setBaseSampling] = useState<SamplingState | null>(null);
  const [chargeSampling, setChargeSampling] = useState<SamplingState | null>(null);
  const [baseWatts, setBaseWatts] = useState('');
  const [chargeWatts, setChargeWatts] = useState('');
  const [chargeBaseToken, setChargeBaseToken] = useState<string | null>(null);
  const [existingBaseConfirmed, setExistingBaseConfirmed] = useState(false);
  const [contextNotice, setContextNotice] = useState('');
  const priorContext = useRef<string | null>(null);
  const priorBase = useRef<string | null>(null);
  const hasAssistantWork = useRef(false);
  hasAssistantWork.current = !!baseSampling || !!chargeSampling || !!baseWatts || !!chargeWatts || existingBaseConfirmed;
  const sample = live?.sample;
  const streamFresh = !!sample && liveFresh && liveAge !== null && liveAge <= 5;
  const ready = enabled && streamFresh && draft.voltageConfirmed && draft.voltage !== null;
  const deviceIdentity = JSON.stringify(live?.device ?? null);
  const contextKey = JSON.stringify([deviceIdentity, live?.source, sample?.calibration_revision, sample?.decoder_version, sample?.formula_version]);
  const frame = useMemo<SamplingFrame>(() => ({
    timestamp: sample?.timestamp ?? NaN,
    mode: sample?.mode ?? 'offline',
    inputVoltage: sample?.adapter_input_voltage_v ?? null,
    baseRaw: sample?.raw_fields?.byte_28 ?? null,
    chargePower: sample?.battery_charge_power_candidate_w ?? null,
    device: deviceIdentity,
    source: live?.source ?? '',
    revision: sample?.calibration_revision ?? '',
    decoderVersion: sample?.decoder_version,
    formulaVersion: sample?.formula_version,
    fresh: ready,
  }), [sample, deviceIdentity, live?.source, ready]);

  useEffect(() => {
    if (!streamFresh) return;
    if (priorContext.current !== null && priorContext.current !== contextKey) {
      setBaseSampling(null); setChargeSampling(null); setBaseWatts(''); setChargeWatts(''); setExistingBaseConfirmed(false);
      setContextNotice(hasAssistantWork.current ? '设备、数据来源或计算配置发生变化，先前采样与建议已清除，请重新校准。' : '');
    }
    priorContext.current = contextKey;
  }, [contextKey, streamFresh]);

  useEffect(() => {
    setBaseSampling(current => current ? advanceSampling(current, frame) : current);
    setChargeSampling(current => current ? advanceSampling(current, frame) : current);
  }, [frame, baseSampling?.phase, chargeSampling?.phase]);

  const existingBase = active?.schema === 2 && active.ac_voltage_nominal_v === draft.voltage ? active.coefficients.base_gain : null;
  const baseResult = baseSampling?.window ? calculateBase(baseSampling.window, readWatts(baseWatts)) : null;
  const selectedBase = existingBaseConfirmed ? existingBase : baseResult?.value ?? null;
  const baseToken = selectedBase === null ? null : `${existingBaseConfirmed ? 'existing' : 'measured'}:${selectedBase}`;
  useEffect(() => {
    if (priorBase.current !== baseToken) {
      setChargeSampling(null); setChargeWatts('');
      priorBase.current = baseToken;
    }
  }, [baseToken]);
  const chargeResult = selectedBase !== null && chargeBaseToken === baseToken && chargeSampling?.window ? calculateCharge(chargeSampling.window, readWatts(chargeWatts), selectedBase) : null;
  const suggestion: AssistantSuggestion | null = selectedBase !== null && draft.voltage !== null
    ? { voltage: draft.voltage, base: selectedBase, charge: chargeResult?.value ?? null, includeCharge: chargeResult?.value !== null && chargeResult?.value !== undefined }
    : null;
  const preview = suggestion ? applySuggestion(draft, suggestion) : null;
  const rows: { key: CoefficientKey; label: string }[] = [{ key: 'base_gain', label: '交流基底 a' }, { key: 'charge_gain', label: '回充补偿 b' }, { key: 'battery_gain', label: '电池放电' }];

  function startBase() {
    if (!ready || draft.voltage === null) return;
    setBaseSampling(createSamplingState('online', draft.voltage)); setBaseWatts(''); setExistingBaseConfirmed(false);
    setChargeSampling(null); setChargeWatts(''); setContextNotice('');
  }
  function startCharge() {
    if (!ready || draft.voltage === null || selectedBase === null) return;
    setChargeSampling(createSamplingState('charging', draft.voltage)); setChargeBaseToken(baseToken); setChargeWatts(''); setContextNotice('');
  }

  return <article className="panel calibration-assistant">
    <div className="panel-heading"><div><h3><FlaskConical size={18} />交流功率校准助手</h3><p>用插座功率计分两步估算系数，也可以只完成第一步。</p></div><span className="calibration-badge">手动核对后保存</span></div>
    {!supportsV2 ? <p className="notice" role="status">当前采集器不支持分步校准。请先更新宿主机采集器；下方仍可填写旧版的三个校准系数。</p> : <>
      <p className="calibration-assistant-intro">只填写接在适配器前的智能插座或交流数显功率计读数（W）。让负载保持稳定，观察并记下每次 30 秒采样同期的功率，采样结束后再填写。采样至少需要 12 条独立读数，相关原始读数的波动不能超过均值的 10%。</p>
      <VoltageSelector id="assistant-voltage" voltage={draft.voltage} confirmed={draft.voltageConfirmed} inputVoltage={streamFresh ? sample?.adapter_input_voltage_v : null} disabled={!enabled} onChange={onVoltageChange} onConfirm={onConfirmVoltage} />
      {!enabled && <p className="notice" role="status">{disabledReason || '校准配置尚未就绪，请查看下方检查结果。'}</p>}
      {enabled && !streamFresh && <p className="notice" role="status">实时采样已中断或超过 5 秒未更新。正在采集的窗口会重置，恢复后可继续；已完成的窗口保持冻结。</p>}
      {contextNotice && <p className="notice" role="status">{contextNotice}</p>}
      <div className="calibration-steps">
        <section className="calibration-step" aria-labelledby="calibration-step-base">
          <div className="calibration-step-title"><span>1</span><div><h4 id="calibration-step-base">交流基底 a</h4><p>外部供电，电池未充电</p></div></div>
          <p className="calibration-step-description">等待充电停止并保持负载稳定，记录这次采样期间的插座功率。</p>
          <button type="button" className="calibration-secondary" disabled={!ready} onClick={startBase}>{baseSampling ? <RotateCcw size={14} /> : <Play size={14} />}{baseSampling ? '重新采样 30 秒' : '开始 30 秒采样'}</button>
          <SamplingProgress state={baseSampling} />
          <div className="calibration-meter-field"><label htmlFor="calibration-base-watts">同期稳定的插座功率（W）</label><input id="calibration-base-watts" type="text" inputMode="decimal" autoComplete="off" placeholder="完成采样后填写" disabled={!ready || !baseSampling?.window} value={baseWatts} onChange={event => setBaseWatts(event.target.value)} aria-describedby="calibration-base-formula" /></div>
          <p id="calibration-base-formula" className="calibration-step-formula">a = 插座功率 ÷ 本次平均基底读数</p>
          {baseWatts && baseResult?.error && <p className="calibration-result-error" role="status">{baseResult.error}</p>}
          {baseResult?.value !== null && baseResult?.value !== undefined && <p className="calibration-calculated">建议 a <strong title={String(baseResult.value)}>{formatCoefficient(baseResult.value)}</strong></p>}
          {existingBase !== null && <div className="calibration-existing-base"><p>当前已生效的 {draft.voltage} V 配置已有基底系数 <strong title={String(existingBase)}>{formatCoefficient(existingBase)}</strong>。若继续沿用，可确认后直接进入第二步。</p><button type="button" className="calibration-secondary" disabled={!ready || existingBaseConfirmed} onClick={() => { setExistingBaseConfirmed(true); setBaseSampling(null); setBaseWatts(''); }}>{existingBaseConfirmed ? <><Check size={14} />已确认使用现有基底</> : '确认使用现有基底'}</button></div>}
          {active?.schema === 1 && active.coefficients && <p className="muted">旧配置没有已确认的电压档位，请重新完成第一步，或在下方手动填写交流系数。</p>}
        </section>
        <section className="calibration-step" aria-labelledby="calibration-step-charge">
          <div className="calibration-step-title"><span>2</span><div><h4 id="calibration-step-charge">回充补偿 b <small>可选</small></h4><p>外部供电，电池正在充电</p></div></div>
          <p className="calibration-step-description">保留第一步系数，等待稳定回充。平均回充原始功率至少需要 2 W。</p>
          <button type="button" className="calibration-secondary" disabled={!ready || selectedBase === null} onClick={startCharge}>{chargeSampling ? <RotateCcw size={14} /> : <Play size={14} />}{chargeSampling ? '重新采样 30 秒' : '开始 30 秒采样'}</button>
          {selectedBase === null ? <p className="muted">先完成第一步，或确认沿用同档位的现有基底系数。</p> : <p className="muted">本步使用 a = <span title={String(selectedBase)}>{formatCoefficient(selectedBase)}</span>；a 改变后，本步采样与建议会清除。</p>}
          <SamplingProgress state={chargeSampling} />
          <div className="calibration-meter-field"><label htmlFor="calibration-charge-watts">本次充电同期的插座功率（W）</label><input id="calibration-charge-watts" type="text" inputMode="decimal" autoComplete="off" placeholder="完成采样后填写" disabled={!ready || !chargeSampling?.window} value={chargeWatts} onChange={event => setChargeWatts(event.target.value)} aria-describedby="calibration-charge-formula" /></div>
          <p id="calibration-charge-formula" className="calibration-step-formula">b =（插座功率 − a × 本次平均基底读数）÷ 本次平均回充原始功率</p>
          {chargeWatts && chargeResult?.error && <p className="calibration-result-error" role="status">{chargeResult.error}</p>}
          {chargeResult?.value !== null && chargeResult?.value !== undefined && <p className="calibration-calculated">建议 b <strong title={String(chargeResult.value)}>{formatCoefficient(chargeResult.value)}</strong></p>}
        </section>
      </div>
      {preview && suggestion && <div className="calibration-preview">
        <h4>填入前预览 · {suggestion.voltage} V</h4>
        <p className="muted">下面比较当前草稿与即将填入的数值。未改动的已有系数保留完整精度；电池放电系数需另行校准。</p>
        <div className="calibration-preview-rows">{rows.map(row => <div key={row.key}><span>{row.label}</span><div className="calibration-preview-change"><span title={draft.values[row.key]}>{draft.values[row.key] ? formatCoefficient(Number(draft.values[row.key])) : '未校准'}</span><ArrowDown size={13} aria-label="变为" /><strong title={preview.values[row.key]}>{preview.values[row.key] ? formatCoefficient(Number(preview.values[row.key])) : '未校准'}</strong></div><small>{coefficientSource(preview, row.key)}</small></div>)}</div>
        {!suggestion.includeCharge && <p className="muted">回充暂未产生有效建议，本次只填入交流基底。同档位的现有回充系数继续保留；尚未配置时保持留空。</p>}
        <button type="button" className="calibration-primary" disabled={!ready} onClick={() => onApply(suggestion)}>填入自定义配置</button><p className="muted">填入后请核对下方表单，再点击“保存配置”使其生效。</p>
      </div>}
      <p className="calibration-assistant-footnote">采样中若供电状态、设备、数据来源或计算配置变化，或采样倒序、间隔超过 5 秒，将从头计时。助手不会自动保存系数。</p>
    </>}
  </article>;
}
