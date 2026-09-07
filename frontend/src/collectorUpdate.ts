import type { CollectorUpdateStage, CollectorUpdateStatus } from './types';

export type CollectorUpdateAction = 'check' | 'install' | 'rollback';
export type CollectorReleaseTarget = { version: string; release_id: number; sha256: string };
export type CollectorRollbackTarget = { version: string; current_version: string };
export const COLLECTOR_UPDATE_DOCS = 'https://github.com/BSakura-Miku/ugreen-ups-panel/blob/main/docs/collector-update.md';

const isObject = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);

export function collectorVersion(value: unknown): string | null {
  return typeof value === 'string' && /^(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})\.(?:0|[1-9]\d{0,5})$/.test(value) ? value : null;
}

export function collectorReleaseTarget(value: unknown): CollectorReleaseTarget | null {
  if (!isObject(value)) return null;
  const version = collectorVersion(value.version);
  return version && typeof value.release_id === 'number' && Number.isSafeInteger(value.release_id) && value.release_id > 0
    && typeof value.sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.sha256)
    ? { version, release_id: value.release_id, sha256: value.sha256 } : null;
}

export function sameCollectorTarget(left: CollectorReleaseTarget | null, right: CollectorReleaseTarget | null): boolean {
  return !!left && !!right && left.version === right.version && left.release_id === right.release_id && left.sha256 === right.sha256;
}

export function collectorReleaseUrl(version: unknown): string | null {
  const valid = collectorVersion(version);
  return valid ? `https://github.com/BSakura-Miku/ugreen-ups-panel/releases/tag/v${valid}` : null;
}

export function collectorAdminKeyReady(value: string): boolean {
  return typeof value === 'string' && value.length <= 128 && !/[\r\n\u0000-\u001f\u007f]/.test(value)
    && /^[A-Za-z0-9_-]{43}$/.test(value.trim());
}

// Fixed same-origin paths only. The manually entered key travels in the request
// header, never a URL/body, storage entry, download or diagnostic message.
export function collectorUpdateRequest(action: CollectorUpdateAction, key: string, body: Record<string, unknown> = {}) {
  if (!['check', 'install', 'rollback'].includes(action)) throw new Error('不支持的采集器操作。');
  if (!collectorAdminKeyReady(key)) throw new Error('请输入有效的管理密钥。');
  const payload = action === 'check' ? {} : action === 'install' ? collectorReleaseTarget(body) : collectorRollbackBody(body);
  if (!payload) throw new Error('更新目标已失效，请重新检查。');
  return {
    url: `/api/collector-update/${action}`,
    init: {
      method: 'POST', credentials: 'same-origin', mode: 'same-origin', redirect: 'error', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-UPS-Update': '1', 'X-UPS-Update-Key': key.trim() },
      body: JSON.stringify(payload),
    } satisfies RequestInit,
  };
}

export function collectorInstallBody(value: unknown): CollectorReleaseTarget | null {
  return collectorReleaseTarget(value);
}

export function collectorRollbackBody(value: unknown): CollectorRollbackTarget | null {
  if (!isObject(value)) return null;
  const version = collectorVersion(value.version), current = collectorVersion(value.current_version);
  return version && current && version !== current ? { version, current_version: current } : null;
}

const busyStages = new Set<CollectorUpdateStage>(['checking', 'downloading', 'verifying', 'installing', 'restarting', 'validating', 'rolling_back']);
export function collectorUpdateBusy(status: CollectorUpdateStatus | null): boolean {
  return status?.operation?.busy === true || !!status?.operation && busyStages.has(status.operation.stage);
}

export function collectorStageLabel(stage: unknown): string {
  const labels: Record<string, string> = { checking: '正在检查稳定发行版', downloading: '正在下载采集器', verifying: '正在校验安装包',
    installing: '正在安装采集器', restarting: '正在重启采集服务', validating: '正在确认遥测恢复', rolling_back: '正在恢复采集器版本',
    succeeded: '操作已完成', failed: '操作未完成', interrupted: '操作曾被中断' };
  return typeof stage === 'string' && Object.hasOwn(labels, stage) ? labels[stage] : '更新状态待确认';
}

export function collectorUpdateError(code: unknown, status = 0): string {
  const messages: Record<string, string> = {
    unauthorized: '管理密钥未通过验证，请检查后重新输入。', busy: '已有操作正在进行，请等待完成。',
    rate_limited: '操作过于频繁，请稍后再试。', service_unavailable: '更新服务暂不可用，请检查 NAS 上的更新服务。',
    incompatible_service: '更新服务版本与此面板不兼容，请按说明升级更新服务。',
    check_required: '请先检查最新稳定发行版。', stale_release: '发行信息已改变，请重新检查并确认目标版本。',
    no_update: '当前没有可安装的新版本。', collector_unavailable: '当前采集器状态未确认，暂时不能更新。',
    rollback_unavailable: '当前没有可自动回退的版本，请查看安装说明。', stale_current: '当前版本已改变，请重新确认回退目标。',
    network_error: '获取发行信息或安装包失败，请检查 NAS 网络后重试。', release_unavailable: '暂时无法获取稳定发行版，请稍后重试。',
    invalid_release: '发行信息未通过验证，暂不提供安装。', missing_digest: '发行版缺少校验信息，暂不提供安装。',
    digest_mismatch: '安装包校验值不符，已停止安装。', invalid_package: '安装包未通过检查，已停止安装。',
    incompatible_package: '此安装包与当前更新服务不兼容。', install_failed: '采集器安装未完成，请查看恢复结果。',
    rollback_failed: '采集器回退未完成，请查看恢复结果。', recovery_failed: '自动恢复未完成，请按说明手动处理。',
    interrupted: '上一次操作被中断，请先核对当前采集器状态。', invalid_request: '此更新请求暂不可用，请重新检查发行版本。',
    internal_error: '更新操作未完成，请检查 NAS 上的更新服务。',
    response_too_large: '发行服务器响应超过允许大小，请稍后重新检查。',
    untrusted_url: '发行下载地址未通过来源验证，已停止操作。', invalid_response: '发行服务器响应无效，请稍后重新检查。',
    no_stable_release: '暂未找到正式发行版本。', asset_missing: '此发行版尚未提供采集器更新包。',
    invalid_digest: '更新包缺少有效的 SHA-256 校验信息，暂不提供安装。',
    checksum_mismatch: '安装包校验值不符，已停止安装。', release_changed: '发行附件已改变，请重新检查并确认目标版本。',
    unsafe_destination: '更新暂存目录不符合要求，请检查 NAS 上的更新服务。',
    package_write_failed: '无法写入更新暂存目录，请检查 NAS 磁盘空间。',
  };
  return typeof code === 'string' && Object.hasOwn(messages, code) ? messages[code] : collectorUpdateHttpError(status);
}

export function collectorOperationResult(operation: CollectorUpdateStatus['operation']): string {
  if (!operation) return '';
  if (operation.outcome === 'restored') return '本次操作未完成，已恢复原采集器版本。';
  if (operation.outcome === 'manual_required') return '需要核对采集器状态，并按说明手动处理。';
  if (operation.outcome === 'updated') return '采集器已更新，遥测恢复检查已通过。';
  if (operation.outcome === 'rolled_back') return '采集器已回退，遥测恢复检查已通过。';
  if (operation.outcome === 'checked') return '稳定发行版检查已完成。';
  return collectorStageLabel(operation.stage);
}

// Copy only public status fields. Unknown response fields and raw error messages
// are deliberately excluded, including any accidental backend secret fields.
export function parseCollectorUpdateStatus(value: unknown): CollectorUpdateStatus | null {
  if (!isObject(value) || value.schema !== 1 || value.updater_schema !== 1 || value.auth_required !== true
    || typeof value.installed !== 'boolean' || !['ready', 'not_installed', 'unreachable', 'incompatible'].includes(String(value.availability))
    || typeof value.update_available !== 'boolean' || !isObject(value.rollback)) return null;
  const nullableTime = (item: unknown) => item === null || typeof item === 'number' && Number.isFinite(item) && item >= 0;
  const safeTime = (item: unknown) => typeof item === 'number' && Number.isFinite(item) && item >= 0;
  const rollback = value.rollback;
  if (typeof rollback.available !== 'boolean' || !['available', 'no_previous', 'legacy_backup', 'incompatible', 'unknown'].includes(String(rollback.reason))
    || !nullableTime(value.checked_at)) return null;
  let current: CollectorUpdateStatus['current'] = null;
  if (value.current !== null) {
    if (!isObject(value.current) || !collectorVersion(value.current.version)) return null;
    current = { version: value.current.version as string,
      revision: typeof value.current.revision === 'string' && /^[a-f0-9]{7,40}$/.test(value.current.revision) ? value.current.revision : null,
      source_sha256: typeof value.current.source_sha256 === 'string' && /^[a-f0-9]{64}$/.test(value.current.source_sha256) ? value.current.source_sha256 : null };
  }
  let latest: CollectorUpdateStatus['latest'] = null;
  if (value.latest !== null) {
    const target = collectorReleaseTarget(value.latest);
    if (!isObject(value.latest) || !target || value.latest.tag !== `v${target.version}`
      || !Number.isSafeInteger(value.latest.size) || (value.latest.size as number) <= 0) return null;
    latest = { ...target, tag: `v${target.version}`, size: value.latest.size as number,
      published_at: typeof value.latest.published_at === 'string' ? value.latest.published_at.slice(0, 80) : null,
      notes: typeof value.latest.notes === 'string' ? value.latest.notes.slice(0, 2000) : '', url: collectorReleaseUrl(target.version)! };
  }
  let operation: CollectorUpdateStatus['operation'] = null;
  if (value.operation !== null) {
    const item = value.operation;
    if (!isObject(item) || typeof item.id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(item.id)
      || !['check', 'install', 'rollback'].includes(String(item.action))
      || ![...busyStages, 'succeeded', 'failed', 'interrupted'].includes(String(item.stage))
      || typeof item.busy !== 'boolean' || !safeTime(item.started_at) || !safeTime(item.updated_at) || !nullableTime(item.finished_at)
      || ![null, 'updated', 'rolled_back', 'restored', 'manual_required', 'checked'].includes(item.outcome as null | string)) return null;
    operation = { id: item.id, action: item.action as CollectorUpdateAction, stage: item.stage as CollectorUpdateStage,
      busy: item.busy, started_at: item.started_at as number, updated_at: item.updated_at as number,
      finished_at: item.finished_at as number | null, from_version: collectorVersion(item.from_version), to_version: collectorVersion(item.to_version),
      outcome: item.outcome as NonNullable<CollectorUpdateStatus['operation']>['outcome'],
      error: isObject(item.error) && typeof item.error.code === 'string' ? { code: item.error.code.slice(0, 80), message: '' } : null };
  }
  return { schema: 1, installed: value.installed, availability: value.availability as CollectorUpdateStatus['availability'],
    updater_version: collectorVersion(value.updater_version), updater_schema: 1, current, latest,
    checked_at: value.checked_at as number | null, update_available: value.update_available, auth_required: true,
    rollback: { available: rollback.available, version: collectorVersion(rollback.version), reason: rollback.reason as CollectorUpdateStatus['rollback']['reason'] }, operation };
}

export function collectorUpdateHttpError(status: number): string {
  if (status === 401 || status === 403) return '管理密钥未通过验证，请检查后重新输入。';
  if (status === 404 || status === 503) return '更新服务暂不可用，请检查 NAS 上的更新服务是否已安装并运行。';
  if (status === 409) return '当前更新状态已改变，请等待状态刷新后重新选择操作。';
  if (status === 400 || status === 422) return '此更新请求暂不可用，请重新检查发行版本。';
  if (status === 429) return '操作过于频繁，请稍后再试。';
  return '操作未能完成，请查看最新状态后再试。';
}
