import { useEffect, useRef, useState } from 'react';
import { ChevronDown, Gauge, RotateCcw } from 'lucide-react';
import { startVisiblePolling } from './readPolling';
import { capacityComparisonValues, capacityDate, capacityIndexLabel, capacityNumber, capacityPresentation, parseBatteryCapacityReport } from './batteryCapacityDisplay';
import type { BatteryCapacityReport } from './batteryCapacityDisplay';
import './battery-capacity.css';

async function capacityRequest(signal: AbortSignal, body?: { expected_epoch_id: string | number | null }): Promise<BatteryCapacityReport> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) abort();
  const timeout = setTimeout(abort, 8000);
  try {
    const response = await fetch(body ? '/api/battery-capacity/reset' : '/api/battery-capacity', {
      method: body ? 'POST' : 'GET', cache: 'no-store', mode: 'same-origin', redirect: 'error', signal: controller.signal,
      ...(body ? { headers: { 'content-type': 'application/json', 'x-ups-capacity': '1' }, body: JSON.stringify(body) } : {}),
    });
    if (!response.ok) throw new Error(response.status === 409 ? 'reference_changed' : response.status === 503 ? 'save_unavailable' : 'request_failed');
    const report = parseBatteryCapacityReport(await response.json());
    if (!report) throw new Error('invalid_report');
    return report;
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener('abort', abort);
  }
}

export default function BatteryCapacityCard() {
  const [report, setReport] = useState<BatteryCapacityReport | null>(null);
  const [readError, setReadError] = useState(false);
  const [resetIntent, setResetIntent] = useState<{ epochId: string | number | null } | null>(null);
  const [resetPending, setResetPending] = useState(false);
  const [resetMessage, setResetMessage] = useState('');
  const [refresh, setRefresh] = useState(0);
  const epoch = useRef(0);
  const mutation = useRef<AbortController | null>(null);

  useEffect(() => () => { epoch.current++; mutation.current?.abort(); }, []);
  useEffect(() => startVisiblePolling(async signal => {
    if (mutation.current) return;
    const generation = epoch.current;
    try {
      const next = await capacityRequest(signal);
      if (!signal.aborted && generation === epoch.current) { setReport(next); setReadError(false); }
    } catch {
      if (!signal.aborted && generation === epoch.current) { setReadError(true); setResetIntent(null); }
    }
  }, () => 10000), [refresh]);

  async function resetReference() {
    if (!resetReady || mutation.current || !report || !resetIntent) return;
    const controller = new AbortController();
    mutation.current = controller;
    epoch.current++;
    setResetPending(true); setResetMessage('');
    try {
      const next = await capacityRequest(controller.signal, { expected_epoch_id: resetIntent.epochId });
      if (!controller.signal.aborted) {
        setReport(next); setReadError(false); setResetIntent(null);
        setResetMessage('新的参考起点已保存，等待后续 90%→80% 的完整记录。');
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        setResetIntent(null);
        setResetMessage(error instanceof Error && error.message === 'reference_changed'
          ? '参考状态已变化或当前采样不可用，正在刷新；本次未重试重建。'
          : error instanceof Error && error.message === 'save_unavailable'
            ? '新参考暂时无法保存，原参考仍保留。'
            : '未能确认重建结果，正在重新读取参考状态；本次不会自动重试。');
      }
    } finally {
      if (mutation.current === controller) mutation.current = null;
      if (!controller.signal.aborted) { setResetPending(false); setRefresh(value => value + 1); }
    }
  }

  const visibleReport = readError ? null : report;
  const view = capacityPresentation(visibleReport, readError);
  const comparison = capacityComparisonValues(visibleReport);
  const blocked = !visibleReport || ['not_configured', 'incompatible'].includes(visibleReport.status) || visibleReport.reason === 'invalid_basis';
  const baseline = blocked ? null : visibleReport.baseline;
  const progress = view.showProgress ? visibleReport?.progress : null;
  const resetReady = !!visibleReport?.current_basis && visibleReport.capture_fresh && visibleReport.reason !== 'stale' && !resetPending;
  const observationLabel = view.historical ? '上次观测' : view.isBaseline ? '基线记录' : '最近比较';

  return <article className={`panel battery-capacity capacity-tone-${view.tone}`} aria-labelledby="battery-capacity-heading">
    <div className="panel-heading capacity-heading"><div><h3 id="battery-capacity-heading"><Gauge size={18}/>相对容量参考</h3>
      <p>{visibleReport?.epoch ? <>参考起点 · {capacityDate(visibleReport.epoch.activated_at)}</> : readError ? '等待参考服务恢复' : '当前校准阶段 · 同区间能量比较'}</p>
    </div><span className={`capacity-state ${view.tone}`}>{view.label}</span></div>
    <div className="capacity-body">
      <div className="capacity-index"><span>{view.historical && view.index !== null ? '上次观测指数' : '相对参考指数'}</span>
        <strong>{capacityIndexLabel(view.index)}{view.index !== null && <small>%</small>}</strong>
        <p>{view.isBaseline ? '本次校准基线 = 100%' : view.index !== null ? '相对本次校准基线' : '等待有效参考记录'}</p>
      </div>
      <div className="capacity-observation"><div className="capacity-section-label"><h4>90% → 80% 区间观察</h4><span>匹配放电 <strong>{view.sampleCount} / 3</strong></span></div>
        <progress value={progress?.completed_pp ?? 0} max={10} aria-label="本轮 90% 至 80% 连续放电观察进度" />
        <div className="capacity-progress-label">{progress ? <><span>已记录 {capacityNumber(progress.completed_pp, 0)} / 10 个百分点</span><span>当前 {capacityNumber(progress.current_soc, 0)}%</span></>
          : <span>{readError ? '暂时无法读取观察进度' : view.historical ? '采集离线 · 连续观察暂停' : blocked ? '等待比较条件就绪' : baseline ? '等待下一次完整区间记录' : '等待完整区间记录'}</span>}</div>
        <p className="capacity-advice">{view.advice}</p>
      </div>
      <dl className="capacity-values"><div><dt>固定基线</dt><dd><strong>{capacityNumber(baseline?.estimate_wh, 2)} <small>Wh</small></strong><span>平均负载 {capacityNumber(baseline?.avg_power_w)} W</span></dd></div>
        <div><dt>{comparison ? `最近 ${comparison.count} 次中位数` : '匹配记录中位数'}</dt><dd><strong>{capacityNumber(comparison?.estimateWh, 2)} <small>Wh</small></strong><span>平均负载中位数 {capacityNumber(comparison?.avgPowerW)} W</span></dd></div>
      </dl>
    </div>
    <div className="capacity-timestamps"><span>{view.observationTimestamp ? <>{observationLabel} · {capacityDate(view.observationTimestamp)}</> : readError ? '参考记录 · 暂不可用' : '完整区间 · 尚未记录'}</span>
      {visibleReport?.sample_timestamp && <span>{view.historical ? '最后采样' : '采样截至'} · {capacityDate(visibleReport.sample_timestamp)}</span>}</div>
    <details className="capacity-details"><summary>参考方式与记录说明 <ChevronDown size={14}/></summary>
      <p>参考起点固定在当前校准阶段。之后首个合格的 90%→80% 连续放电区间建立基线，定义为 100%；后续记录不会滚动替换它。</p>
      <p>仅比较相同电量区间、相同设备与电池放电校准依据，平均负载与基线相差不超过 ±{capacityNumber((visibleReport?.criteria.load_tolerance ?? .1) * 100, 0)}% 的独立放电周期。最近最多 3 次匹配记录的能量中位数除以基线能量，得到相对参考指数；不足 3 次为初步观察。记录按自然发生的供电过程积累。</p>
      <p>采样中断或电量异常会中止本轮观察；负载波动过大、未完整覆盖区间的记录不参与比较。当前无法获知电池温度，温度变化与电量估算偏差仍可能影响结果。这个指数反映相对本次校准阶段的变化，不是出厂健康度（SOH）。</p>
      {visibleReport?.status === 'incompatible' && visibleReport.baseline && <p className="capacity-history-note">原参考摘要：{capacityDate(visibleReport.baseline.end_ts)}，90%→80% 记录 {capacityNumber(visibleReport.baseline.estimate_wh, 2)} Wh，平均负载 {capacityNumber(visibleReport.baseline.avg_power_w)} W。它与当前依据不兼容，未用于主指数。</p>}
      <div className="capacity-reset-area">
        {!resetIntent ? <><button className="capacity-reset-button" type="button" disabled={!resetReady} onClick={() => { setResetIntent({ epochId: visibleReport?.epoch?.id ?? null }); setResetMessage(''); }}><RotateCcw size={13}/>重新建立参考</button><span>更换电池或校准依据后，可设定新的参考起点。</span></>
          : <div className="capacity-reset-confirmation"><p>将当前参考归档保留，并从现在重新收集 90%→80% 的完整记录。新基线建立前，主指数显示为「—」。</p><div><button type="button" className="capacity-reset-confirm" disabled={!resetReady} onClick={() => void resetReference()}>{resetPending ? '正在保存…' : '确认重新建立'}</button><button type="button" className="capacity-reset-cancel" disabled={resetPending} onClick={() => setResetIntent(null)}>取消</button></div></div>}
      </div>
      {resetMessage && <p className="capacity-reset-message" role="status">{resetMessage}</p>}
    </details>
  </article>;
}
