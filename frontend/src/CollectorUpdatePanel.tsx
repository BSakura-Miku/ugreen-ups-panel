import React, { useEffect, useRef, useState } from 'react';
import { AlertTriangle, ArrowDownToLine, ArrowUpRight, CheckCircle2, Eye, EyeOff, KeyRound, RefreshCw, RotateCcw, X } from 'lucide-react';
import type { CollectorUpdateStatus, DiagnosticBuild, DiagnosticCheck } from './types';
import { diagnosticTime, diagnosticVersion } from './diagnosticDisplay';
import { COLLECTOR_UPDATE_DOCS, collectorAdminKeyReady, collectorOperationResult, collectorReleaseTarget, collectorReleaseUrl,
  collectorRollbackBody, collectorStageLabel, collectorUpdateBusy, collectorUpdateError, collectorUpdateRequest,
  collectorHealthChecks, collectorPreflightIssue, parseCollectorUpdateStatus, sameCollectorTarget } from './collectorUpdate';
import type { CollectorReleaseTarget, CollectorRollbackTarget, CollectorUpdateAction } from './collectorUpdate';

type Intent = { action: 'install'; target: CollectorReleaseTarget; fromVersion: string }
  | { action: 'rollback'; target: CollectorRollbackTarget };
type Props = { fallbackCurrent?: DiagnosticBuild | null; diagnosticsFresh?: boolean; captureFresh?: boolean; checks?: DiagnosticCheck[] };
const checkLabels: Record<DiagnosticCheck['status'], string> = { ok: '已确认', waiting: '等待', warning: '需关注', unknown: '未知', error: '异常' };

class UpdateBoundary extends React.Component<{ children: React.ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <article className="panel update-panel" role="status"><h3>采集器更新</h3><p className="update-note">更新信息暂时无法显示，其他诊断功能仍可使用。</p><a className="update-link" href={COLLECTOR_UPDATE_DOCS} target="_blank" rel="noreferrer">查看安装与恢复说明 <ArrowUpRight size={14}/></a></article> : this.props.children;
  }
}

function CollectorUpdateContent({ fallbackCurrent, diagnosticsFresh = false, captureFresh = false, checks }: Props) {
  const [status, setStatus] = useState<CollectorUpdateStatus | null>(null);
  const [readError, setReadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [adminKey, setAdminKey] = useState('');
  const [controlsOpen, setControlsOpen] = useState(false);
  const [keyVisible, setKeyVisible] = useState(false);
  const [intent, setIntent] = useState<Intent | null>(null);
  const [pending, setPending] = useState<CollectorUpdateAction | null>(null);
  const [awaitingStatus, setAwaitingStatus] = useState(false);
  const [pollGeneration, setPollGeneration] = useState(0);
  const [receivedAt, setReceivedAt] = useState(0);
  const [clock, setClock] = useState(Date.now());
  const mutationLock = useRef(false);
  const mutationEpoch = useRef(0);
  const mounted = useRef(true);
  const mutationController = useRef<AbortController | null>(null);

  useEffect(() => {
    mounted.current = true;
    const timer = setInterval(() => setClock(Date.now()), 1000);
    return () => { mounted.current = false; clearInterval(timer); mutationController.current?.abort(); };
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let knownBusy = collectorUpdateBusy(status);
    const poll = async () => {
      const epoch = mutationEpoch.current;
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 8000);
      let next: CollectorUpdateStatus | null = null;
      try {
        // GET never carries the management key and never initiates a check.
        const response = await fetch('/api/collector-update', { signal: controller.signal, cache: 'no-store', mode: 'same-origin', redirect: 'error' });
        if (!response.ok) throw new Error('status_unavailable');
        next = parseCollectorUpdateStatus(await response.json());
        if (!next) throw new Error('invalid_status');
        if (!stopped && epoch === mutationEpoch.current) {
          knownBusy = collectorUpdateBusy(next);
          setStatus(next); setReadError(''); setReceivedAt(Date.now()); setClock(Date.now()); setAwaitingStatus(false);
        }
      } catch {
        if (!stopped && epoch === mutationEpoch.current) setReadError('暂时无法读取更新服务状态，正在重试。');
      } finally {
        clearTimeout(timeout);
        if (!stopped) timer = setTimeout(poll, knownBusy ? 2000 : 10000);
      }
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); controller?.abort(); };
  }, [pollGeneration]);

  const busy = collectorUpdateBusy(status);
  const currentVersion = status?.current?.version || null;
  const target = collectorReleaseTarget(status?.latest);
  const rollback = status?.rollback.available ? collectorRollbackBody({ version: status.rollback.version, current_version: currentVersion }) : null;
  const recent = !!status && !readError && clock - receivedAt <= (busy ? 10000 : 20000);
  const ready = recent && status?.installed === true && status.availability === 'ready';
  const locked = !!pending || busy || awaitingStatus || !ready;
  const preflightIssue = collectorPreflightIssue(status);
  const changeLocked = locked || !!preflightIssue;
  const keyReady = collectorAdminKeyReady(adminKey);
  const installable = !!target && !!currentVersion && status?.update_available === true;
  const intentCurrent = intent?.action === 'install'
    ? installable && sameCollectorTarget(intent.target, target) && intent.fromVersion === currentVersion
    : intent?.action === 'rollback' ? !!rollback && intent.target.version === rollback.version && intent.target.current_version === rollback.current_version : false;
  const operation = status?.operation;
  const runtimeVersion = fallbackCurrent?.version || status?.runtime?.version || null;
  const runtimeFresh = diagnosticsFresh && captureFresh;
  const healthChecks = collectorHealthChecks(checks, diagnosticsFresh, captureFresh);
  const recoveryWarning = operation?.outcome === 'restored' || operation?.outcome === 'manual_required'
    || operation?.stage === 'failed' || operation?.stage === 'interrupted';
  const serviceNotice = status?.availability === 'not_installed'
    ? '尚未安装采集器更新服务。先在 NAS 上完成一次安装，即可从这里检查、更新和回退采集器。'
    : status?.availability === 'unreachable' ? '更新服务当前无法连接，请按说明检查 NAS 上的服务。'
    : status?.availability === 'incompatible' ? '更新服务与此面板不兼容，请按说明升级更新服务。' : '';

  const controlsAvailable = status?.installed === true && status.availability === 'ready';
  useEffect(() => {
    if (!controlsAvailable) { setControlsOpen(false); setAdminKey(''); setKeyVisible(false); setIntent(null); }
  }, [controlsAvailable]);

  async function submit(action: CollectorUpdateAction, reviewed?: Intent) {
    if (mutationLock.current || locked || !keyReady) return;
    if (action !== 'check' && changeLocked) return;
    let body: Record<string, unknown> = {};
    if (action === 'install') {
      if (reviewed?.action !== 'install' || !intentCurrent || !sameCollectorTarget(reviewed.target, target)) return;
      body = reviewed.target;
    } else if (action === 'rollback') {
      if (reviewed?.action !== 'rollback' || !intentCurrent || !rollback) return;
      body = reviewed.target;
    }
    mutationLock.current = true;
    mutationEpoch.current += 1;
    setPending(action); setActionError(''); setIntent(null); setAwaitingStatus(true);
    const controller = new AbortController();
    mutationController.current = controller;
    const timeout = setTimeout(() => controller.abort(), 12000);
    try {
      const request = collectorUpdateRequest(action, adminKey, body);
      const response = await fetch(request.url, { ...request.init, signal: controller.signal });
      let data: unknown = null;
      try { data = await response.json(); } catch { /* Never display an unstructured response body. */ }
      if (!response.ok) {
        const detail = data && typeof data === 'object' && 'detail' in data ? data.detail : null;
        const code = detail && typeof detail === 'object' && 'code' in detail ? detail.code : null;
        if (mounted.current) setActionError(collectorUpdateError(code, response.status));
      } else {
        const next = parseCollectorUpdateStatus(data);
        if (mounted.current && next) { setStatus(next); setReceivedAt(Date.now()); }
        else if (mounted.current) setActionError('请求已返回，正在重新读取后台状态。请勿重复提交。');
      }
    } catch {
      if (mounted.current) setActionError('提交结果暂未确认，正在重新读取后台状态。请勿重复提交。');
    } finally {
      clearTimeout(timeout); mutationController.current = null; mutationLock.current = false;
      if (mounted.current) {
        mutationEpoch.current += 1;
        setPending(null); setAwaitingStatus(true); setPollGeneration(value => value + 1);
      }
    }
  }

  const selectInstall = () => {
    if (changeLocked || !target || !currentVersion || !installable) return;
    setActionError(''); setIntent({ action: 'install', target: { ...target }, fromVersion: currentVersion });
  };
  const selectRollback = () => {
    if (changeLocked || !rollback) return;
    setActionError(''); setIntent({ action: 'rollback', target: { ...rollback } });
  };

  return <article className="panel update-panel" aria-labelledby="collector-update-heading">
    <div className="update-heading"><h3 id="collector-update-heading"><ArrowDownToLine size={18}/>采集器更新</h3><span className="update-service-status">{pending || busy ? '操作进行中' : !status ? '读取状态中' : status.availability === 'ready' && recent ? '手动更新' : '暂不可用'}</span></div>
    <p className="update-note">检查与安装由你手动发起。这里只更新 NAS 上的采集器；面板镜像仍需通过 Docker 更新。</p>
    <dl className="update-versions"><div><dt>已安装采集器源码</dt><dd>{diagnosticVersion(currentVersion)}</dd></div><div><dt>{runtimeFresh ? '实际运行版本' : '最近报告版本'}</dt><dd>{diagnosticVersion(runtimeVersion)}</dd></div><div><dt>最新稳定版</dt><dd>{status?.latest?.version || '尚未检查'}</dd></div><div><dt>宿主更新服务</dt><dd>{diagnosticVersion(status?.updater_version)}</dd></div></dl>
    {currentVersion && runtimeVersion && currentVersion !== runtimeVersion && <p className="update-feedback" role="status">已安装源码与采集器运行报告的版本不同，请核对服务实际使用的源码目录和进程。</p>}
    {preflightIssue && <p className="update-feedback" role="status">更新预检未通过：{preflightIssue}</p>}
    {!preflightIssue && status?.installed && <p className="update-note">{status.preflight?.ready ? '当前配置预检已通过。' : '宿主更新服务尚未提供完整配置预检，请按说明升级更新服务以启用检查。'}{status.source_status === 'unverified' ? ' 此安装没有可核验的发行源码指纹。' : status.source_status === 'verified' ? ' 已安装源码指纹已核验。' : ''}</p>}
    <p className="update-last-check">{status?.checked_at ? `上次检查 ${diagnosticTime(status.checked_at, true)}` : '尚未检查发行版。'}{status?.latest && !status.update_available && status.current ? ' 当前没有可安装的新版本。' : ''}</p>
    {serviceNotice && <div className="update-service-note" role="status"><p>{serviceNotice}</p><a className="update-link" href={COLLECTOR_UPDATE_DOCS} target="_blank" rel="noreferrer">NAS 一次性安装说明 <ArrowUpRight size={14}/></a></div>}
    {readError && <p className="update-feedback" role="status">{readError} 其他诊断信息仍可查看。</p>}
    {!readError && status && !recent && <p className="update-feedback" role="status">更新状态尚未及时刷新，操作按钮已暂停。</p>}

    {(pending || operation) && <div className={`update-operation ${recoveryWarning ? 'update-operation-warning' : ''}`} role="status" aria-live="polite">
      <strong>{recoveryWarning && <AlertTriangle size={18}/>} {pending ? pending === 'check' ? '正在提交检查请求' : pending === 'install' ? '正在提交安装请求' : '正在提交回退请求' : busy ? collectorStageLabel(operation?.stage) : collectorOperationResult(operation || null)}</strong>
      {(pending || busy) && <progress aria-label="采集器操作正在进行"/>}
      {operation?.to_version && <p>目标版本 {operation.to_version}{operation.from_version ? ` · 原版本 ${operation.from_version}` : ''}</p>}
      {operation?.error && <p>{collectorUpdateError(operation.error.code)}</p>}
      {operation?.updated_at && <small>状态更新于 {diagnosticTime(operation.updated_at, true)}</small>}
      {(pending || busy) && <p>进度由 NAS 保存，刷新页面后可继续查看。采集恢复期间，实时遥测可能短暂不可用。</p>}
      {operation?.outcome === 'manual_required' && <a className="update-link" href={COLLECTOR_UPDATE_DOCS} target="_blank" rel="noreferrer">查看手动恢复说明 <ArrowUpRight size={14}/></a>}
    </div>}
    {(operation?.outcome === 'updated' || operation?.outcome === 'rolled_back') && !busy && <section className="update-health" aria-label="更新后的当前运行检查"><h4>当前运行检查</h4><p className="update-note">安装结果保留在上方。这里分别确认当前的采集、校准和历史记录状态。</p><ul className="diag-checks">{healthChecks.map(check => <li key={check.id}><div><strong>{check.label}</strong><span className={`diag-status diag-status-${check.status}`}>{checkLabels[check.status]}</span></div><p>{check.detail}</p></li>)}</ul></section>}

    {actionError && <p className="update-feedback" role="status">{actionError}</p>}
    {awaitingStatus && !pending && !readError && <p className="update-note" role="status">正在确认后台状态…</p>}
    {controlsAvailable && <button type="button" className="update-button update-controls-toggle" aria-expanded={controlsOpen} aria-controls="collector-update-controls" onClick={() => { setControlsOpen(value => !value); setAdminKey(''); setKeyVisible(false); setIntent(null); }}>{controlsOpen ? '收起更新管理' : '检查与管理更新'}</button>}
    {controlsOpen && controlsAvailable && <div id="collector-update-controls">
      <div className="update-key-field"><label htmlFor="collector-update-admin-key"><KeyRound size={15}/>管理密钥</label><div className="update-key-input"><input id="collector-update-admin-key" type={keyVisible ? 'text' : 'password'} value={adminKey} autoComplete="off" autoCapitalize="none" spellCheck={false} maxLength={128} aria-describedby="collector-update-key-note" placeholder="手动粘贴 NAS 管理密钥" onChange={event => { setAdminKey(event.target.value); setActionError(''); }}/><button type="button" className="update-icon-button" aria-label={keyVisible ? '隐藏管理密钥' : '显示管理密钥'} aria-pressed={keyVisible} onClick={() => setKeyVisible(value => !value)}>{keyVisible ? <EyeOff size={17}/> : <Eye size={17}/>}</button><button type="button" className="update-icon-button" aria-label="清除管理密钥" disabled={!adminKey} onClick={() => { setAdminKey(''); setKeyVisible(false); }}>{<X size={17}/>}</button></div><p id="collector-update-key-note" className="update-note">密钥仅在此页面内存中保留，收起、刷新或离开诊断页后清除。检查版本也需要密钥。</p><details className="update-key-help"><summary>如何取得管理密钥</summary><p className="update-note">在 NAS 终端手动执行以下命令，再将输出粘贴到上方：</p><code>sudo cat /etc/ugreen-ups-updater/key</code><p className="update-note">请勿将命令输出放进截图、日志或分享文件。</p></details></div>
      <div className="update-actions"><button type="button" className="update-button" disabled={locked || !keyReady} onClick={() => void submit('check')}><RefreshCw size={15}/>检查更新</button>{installable && <button type="button" className="update-button update-button-primary" disabled={changeLocked} onClick={selectInstall}><ArrowDownToLine size={15}/>查看并更新</button>}</div>
      {adminKey && !keyReady && <p className="update-feedback">请输入 NAS 生成的完整 43 位管理密钥。</p>}
      {intent && <div className="update-confirmation"><h4>{intent.action === 'install' ? `更新至 ${intent.target.version}` : `回退至 ${intent.target.version}`}</h4><p>{intent.action === 'install' ? `当前版本 ${intent.fromVersion}。` : `当前版本 ${intent.target.current_version}。`}采集器会短暂重启，恢复后继续记录。历史数据、校准设置和 NAS 原有 UPS 保护保持不变。</p>{intent.action === 'install' && <details className="update-digest"><summary>安装包校验信息</summary><code>SHA-256 {intent.target.sha256}</code></details>}{!intentCurrent && <p className="update-feedback">后台版本信息已改变，请取消后重新选择目标。</p>}<div className="update-actions"><button type="button" className="update-button update-button-primary" disabled={changeLocked || !keyReady || !intentCurrent} onClick={() => void submit(intent.action, intent)}><CheckCircle2 size={15}/>{intent.action === 'install' ? '确认安装此版本' : '确认回退此版本'}</button><button type="button" className="update-button" onClick={() => setIntent(null)}>取消</button></div></div>}
      <details className="update-rollback"><summary><RotateCcw size={14}/>回退到上一版本</summary>{rollback ? <><p className="update-note">可回退至 {rollback.version}。将使用 NAS 已保留并确认兼容的版本。</p><button type="button" className="update-button" disabled={changeLocked} onClick={selectRollback}>查看回退目标</button></> : <p className="update-note">{status.rollback.reason === 'legacy_backup' ? '保留的是旧版安装备份，需按说明手动回退。' : status.rollback.reason === 'incompatible' ? '上一版本与当前更新服务不兼容，暂不支持自动回退。' : '当前没有已确认可自动回退的版本。'}</p>}</details>
    </div>}
    {status?.latest && <details className="update-release-notes"><summary>发行说明 · {status.latest.version}</summary>{status.latest.notes ? <p>{status.latest.notes}</p> : <p>此发行版未提供说明文字。</p>}<a className="update-link" href={collectorReleaseUrl(status.latest.version)!} target="_blank" rel="noreferrer">在 GitHub 查看完整发行说明 <ArrowUpRight size={14}/></a></details>}
    {status?.installed && <a className="update-link update-doc-link" href={COLLECTOR_UPDATE_DOCS} target="_blank" rel="noreferrer">安装与恢复说明 <ArrowUpRight size={14}/></a>}
  </article>;
}

export default function CollectorUpdatePanel(props: Props) {
  return <UpdateBoundary><CollectorUpdateContent {...props}/></UpdateBoundary>;
}
