import assert from 'node:assert/strict';
import { test } from 'node:test';
import { collectorAdminKeyReady, collectorInstallBody, collectorReleaseTarget, collectorReleaseUrl,
  collectorUpdateHttpError, collectorUpdateRequest, collectorVersion, sameCollectorTarget, collectorRollbackBody,
  collectorUpdateBusy, collectorStageLabel, collectorUpdateError, collectorOperationResult, parseCollectorUpdateStatus } from '../src/collectorUpdate.ts';

const target = { version: '0.9.0', release_id: 12345, sha256: 'a'.repeat(64) };
const key = 's'.repeat(43);
const status = {
  schema:1, installed:true, availability:'ready', updater_version:'0.9.0', updater_schema:1,
  current:{version:'0.8.0',revision:'b'.repeat(40),source_sha256:'c'.repeat(64)},
  latest:{...target,tag:'v0.9.0',size:12345,published_at:null,notes:'Release notes',url:'https://github.com/BSakura-Miku/ugreen-ups-panel/releases/tag/v0.9.0'},
  checked_at:100,update_available:true,auth_required:true,rollback:{available:false,version:null,reason:'no_previous'},operation:null,
};
const operation = { id:'synthetic-operation',action:'install',stage:'downloading',busy:true,started_at:100,updated_at:102,
  finished_at:null,from_version:'0.8.0',to_version:'0.9.0',outcome:null,error:null };

test('only stable numeric collector versions can form fixed release links', () => {
  for (const value of ['0.9.0', '12.0.123']) assert.equal(collectorVersion(value), value);
  for (const value of [null, undefined, '', 'v0.9.0', 'latest', '0.9.0-rc1', '01.9.0', 'https://example.com', '../0.9.0', '0.9.0?key=test']) {
    assert.equal(collectorVersion(value), null);
    assert.equal(collectorReleaseUrl(value), null);
  }
  assert.equal(collectorReleaseUrl('0.9.0'), 'https://github.com/BSakura-Miku/ugreen-ups-panel/releases/tag/v0.9.0');
});

test('install targets require an exact release identifier and lowercase SHA-256', () => {
  assert.deepEqual(collectorReleaseTarget(target), target);
  for (const value of [null, {}, { ...target, release_id: 0 }, { ...target, release_id: '12345' },
    { ...target, release_id: Number.MAX_SAFE_INTEGER + 1 }, { ...target, sha256: 'A'.repeat(64) },
    { ...target, sha256: 'a'.repeat(63) }, { ...target, version: 'latest' }]) assert.equal(collectorReleaseTarget(value), null);
  const source = { ...target, admin_key: 'not-to-be-copied', archive_url: 'https://example.com' };
  assert.deepEqual(collectorInstallBody(source), target);
  assert.equal(source.admin_key, 'not-to-be-copied');
});

test('a changed version, release ID or checksum invalidates an already reviewed target', () => {
  assert.equal(sameCollectorTarget(target, { ...target }), true);
  for (const value of [null, { ...target, version: '0.9.1' }, { ...target, release_id: 12346 }, { ...target, sha256: 'b'.repeat(64) }]) {
    assert.equal(sameCollectorTarget(target, value), false);
  }
});

test('manual admin keys go only to fixed POST headers, with explicit CSRF header and same-origin credentials', () => {
  const secret = key;
  for (const action of ['check', 'install', 'rollback']) {
    const request = collectorUpdateRequest(action, ` ${secret} `, action === 'install' ? target : action === 'rollback' ? {version:'0.8.0',current_version:'0.9.0'} : {});
    assert.equal(request.url, `/api/collector-update/${action}`);
    assert.equal(request.init.method, 'POST');
    assert.equal(request.init.credentials, 'same-origin');
    assert.equal(request.init.mode, 'same-origin');
    assert.equal(request.init.redirect, 'error');
    assert.equal(request.init.headers['X-UPS-Update'], '1');
    assert.equal(request.init.headers['X-UPS-Update-Key'], secret);
    assert.ok(!request.url.includes(secret) && !request.init.body.includes(secret));
  }
  assert.throws(() => collectorUpdateRequest('https://example.com', secret), /不支持/);
});

test('missing or control-character keys fail locally without echoing the supplied key', () => {
  for (const invalid of ['', '   ', 'a'.repeat(42), 'a'.repeat(44), `${key}\n`, 'secret\rvalue', 'secret\0value', 'a'.repeat(513)]) {
    assert.equal(collectorAdminKeyReady(invalid), false);
    assert.throws(() => collectorUpdateRequest('check', invalid), error => error.message === '请输入有效的管理密钥。');
  }
  assert.equal(collectorAdminKeyReady(key), true);
});

test('HTTP errors use fixed messages rather than server bodies or secret-bearing exceptions', () => {
  assert.match(collectorUpdateHttpError(403), /密钥/);
  assert.match(collectorUpdateHttpError(409), /状态已改变/);
  assert.match(collectorUpdateHttpError(503), /服务/);
  assert.equal(collectorUpdateHttpError(500), collectorUpdateHttpError(599));
  for (const unknown of ['constructor','__proto__','key-is-private',null]) {
    assert.equal(collectorUpdateError(unknown), collectorUpdateHttpError(0));
    assert.equal(collectorStageLabel(unknown), '更新状态待确认');
  }
});

test('rollback requests bind both the reviewed destination and current version', () => {
  const rollback = {version:'0.8.0',current_version:'0.9.0'};
  assert.deepEqual(collectorRollbackBody({...rollback,secret:key}),rollback);
  assert.equal(collectorRollbackBody({...rollback,current_version:'0.8.0'}),null);
  assert.equal(collectorRollbackBody({version:'0.8.0'}),null);
  assert.throws(()=>collectorUpdateRequest('rollback',key,{}),/目标已失效/);
  assert.equal(collectorUpdateRequest('check',key,{secret:key}).init.body,'{}');
  assert.deepEqual(JSON.parse(collectorUpdateRequest('install',key,{...target,secret:key}).init.body),target);
});

test('status parsing copies only public fields and ignores backend secrets or raw exception messages', () => {
  const raw = {...status,admin_key:key,operation:{...operation,error:{code:'install_failed',message:key}},
    latest:{...status.latest,url:'https://example.com/steal',notes:'<script>untrusted plain text</script>'.repeat(100)}};
  const parsed = parseCollectorUpdateStatus(raw);
  assert.ok(parsed);
  assert.equal(JSON.stringify(parsed).includes(key),false);
  assert.equal(parsed.operation.error.message,'');
  assert.equal(parsed.latest.url,collectorReleaseUrl('0.9.0'));
  assert.equal(parsed.latest.notes.length,2000);
  assert.deepEqual(parseCollectorUpdateStatus(status),status);
  assert.equal(raw.admin_key,key);
});

test('missing or incompatible status contracts cannot enable operations', () => {
  for(const invalid of [null,[],{}, {...status,schema:2}, {...status,auth_required:false},
    {...status,availability:'made-up'}, {...status,checked_at:Infinity},
    {...status,latest:{...status.latest,sha256:'bad'}}, {...status,operation:{...operation,stage:'made-up'}}]) {
    assert.equal(parseCollectorUpdateStatus(invalid),null);
  }
  const offline={...status,installed:false,availability:'not_installed',current:null,latest:null,checked_at:null,update_available:false};
  assert.ok(parseCollectorUpdateStatus(offline));
});

test('persisted active stages stay busy even after a page refresh or an inconsistent false flag', () => {
  assert.equal(collectorUpdateBusy(null),false);
  assert.equal(collectorUpdateBusy(status),false);
  for(const stage of ['checking','downloading','verifying','installing','restarting','validating','rolling_back']) {
    assert.equal(collectorUpdateBusy({...status,operation:{...operation,stage,busy:false}}),true);
  }
  assert.equal(collectorUpdateBusy({...status,operation:{...operation,stage:'succeeded',busy:false}}),false);
});

test('an unsuccessful update with restored or manual-required outcome never claims update success', () => {
  assert.match(collectorOperationResult({...operation,stage:'failed',busy:false,outcome:'restored'}),/未完成.*已恢复原/);
  assert.match(collectorOperationResult({...operation,stage:'failed',busy:false,outcome:'manual_required'}),/手动处理/);
  assert.equal(collectorOperationResult({...operation,stage:'interrupted',busy:false,outcome:'manual_required'}),'需要核对采集器状态，并按说明手动处理。');
  assert.match(collectorOperationResult({...operation,stage:'succeeded',busy:false,outcome:'rolled_back'}),/已回退/);
  assert.match(collectorOperationResult({...operation,stage:'succeeded',busy:false,outcome:'updated'}),/已更新/);
});
