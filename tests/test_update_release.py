import copy
from dataclasses import replace
import gzip
import hashlib
from http.client import BadStatusLine
import io
import json
import os
from pathlib import Path
import runpy
import tarfile
from urllib.request import Request
from urllib.error import HTTPError

import pytest

from ups_panel import update_release as release

ROOT = Path(__file__).resolve().parents[1]
PACKAGER = runpy.run_path(str(ROOT / 'scripts/package-collector.py'))
REVISION = 'a' * 40
VERSION = '0.9.0'
TAG = 'v' + VERSION
ASSET_NAME = 'collector-' + TAG + '.tar.gz'
ASSET_URL = release.WEB_ROOT + '/releases/download/' + TAG + '/' + ASSET_NAME
BY_ID_URL = release.API_ROOT + '/releases/42'


class Response(io.BytesIO):
    def __init__(self, body, url, headers=None, status=200):
        super().__init__(body)
        self.url, self.headers, self.status = url, headers or {}, status

    def geturl(self):
        return self.url

    def getcode(self):
        return self.status


class Opener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        expected_url, payload = self.responses.pop(0)
        assert request.full_url == expected_url
        assert 0 < timeout <= 15
        assert not any(key.lower() in {'authorization', 'proxy-authorization', 'cookie'}
                       for key in request.headers)
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, Response):
            return payload
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
        return Response(payload, expected_url)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    package = root / 'ups_panel'
    package.mkdir(parents=True)
    (package / '__init__.py').write_bytes(b'raise AssertionError("downloaded package was imported")\n')
    (package / 'build_info.py').write_bytes(b'VERSION = "0.9.0"\nraise AssertionError("version was executed")\n')
    (package / 'collector.py').write_bytes('# passive collector — UTF-8\n'.encode())
    (root / 'LICENSE').write_bytes((ROOT / 'LICENSE').read_bytes())
    return root


@pytest.fixture
def package(source):
    return PACKAGER['make_package'](source, REVISION)


def asset(data, name=ASSET_NAME, asset_id=99, digest=True):
    result = {'id': asset_id, 'name': name, 'size': len(data), 'state': 'uploaded',
              'url': release.API_ROOT + '/releases/assets/' + str(asset_id),
              'browser_download_url': release.WEB_ROOT + '/releases/download/' + TAG + '/' + name}
    if digest:
        result['digest'] = 'sha256:' + hashlib.sha256(data).hexdigest()
    return result


def listing(data):
    return {'id': 42, 'tag_name': TAG, 'draft': False, 'prerelease': False,
            'html_url': release.WEB_ROOT + '/releases/tag/' + TAG,
            'published_at': '2026-09-07T12:00:00Z', 'body': 'Collector release',
            'assets': [asset(data)]}


def tar_bytes(entries, *, format=tarfile.USTAR_FORMAT):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=format) as archive:
        for info, content in entries:
            if isinstance(info, str):
                info = tarfile.TarInfo(info)
                info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(output.getvalue(), mtime=0)


def files_entries(data):
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        return [(member.name, archive.extractfile(member).read()) for member in archive if member.isfile()]


def changed_metadata(data, changes=None, *, raw=None):
    entries = files_entries(data)
    for index, (name, content) in enumerate(entries):
        if name == 'collector-release.json':
            metadata = json.loads(content)
            metadata.update(changes or {})
            entries[index] = (name, raw if raw is not None else json.dumps(metadata).encode())
    return tar_bytes(entries)


def test_latest_and_download_refresh_asset_identity_with_no_auth(package):
    data, metadata = package
    value = listing(data)
    opener = Opener([(release.LATEST_URL, value), (BY_ID_URL, value), (ASSET_URL, data)])
    client = release.ReleaseClient(opener)
    latest = client.latest()
    assert latest.as_dict()['release_id'] == 42
    assert latest.as_dict()['asset_id'] == 99
    assert latest.version == VERSION and latest.tag == TAG
    assert latest.published_at == '2026-09-07T12:00:00Z'
    assert client.download(latest) == data
    assert not opener.responses
    assert release.inspect_package(data, expected_version=latest.version).metadata == metadata


def test_manifest_fallback_uses_fixed_asset_and_verifies_manifest_digest(package):
    data, _ = package
    manifest = (hashlib.sha256(data).hexdigest() + '  ' + ASSET_NAME + '\n').encode()
    value = listing(data)
    value['assets'][0].pop('digest')
    value['assets'].append(asset(manifest, 'SHA256SUMS', 100))
    manifest_url = release.WEB_ROOT + '/releases/download/' + TAG + '/SHA256SUMS'
    opener = Opener([(release.LATEST_URL, value), (manifest_url, manifest),
                     (BY_ID_URL, value), (manifest_url, manifest), (ASSET_URL, data)])
    client = release.ReleaseClient(opener)
    latest = client.latest()
    assert latest.sha256 == hashlib.sha256(data).hexdigest()
    assert client.download(latest) == data


@pytest.mark.parametrize('digest', ['', False, 4, 'sha512:' + 'a' * 64, 'sha256:' + 'A' * 64])
def test_present_but_invalid_api_digest_never_falls_back(package, digest):
    value = listing(package[0])
    value['assets'][0]['digest'] = digest
    opener = Opener([(release.LATEST_URL, value)])
    with pytest.raises(release.ReleaseError, match='^invalid_digest$'):
        release.ReleaseClient(opener).latest()
    assert len(opener.requests) == 1


@pytest.mark.parametrize('field,value', [
    ('draft', True), ('prerelease', True), ('draft', 0), ('prerelease', None),
    ('tag_name', 'v0.9.0-rc.1'), ('tag_name', '0.9.0'), ('tag_name', 'v00.9.0'),
    ('tag_name', 'v0.8.0'), ('id', True), ('id', 0), ('id', 42.0),
    ('published_at', '2026-99-07T12:00:00Z'), ('published_at', 'x' * 100),
    ('html_url', 'https://github.com/attacker/ugreen-ups-panel/releases/tag/v0.9.0'),
    ('body', []), ('assets', []),
])
def test_invalid_release_fields(package, field, value):
    document = listing(package[0])
    document[field] = value
    with pytest.raises(release.ReleaseError):
        release.ReleaseClient(Opener([(release.LATEST_URL, document)])).latest()


@pytest.mark.parametrize('field,value', [
    ('id', True), ('size', True), ('size', 1.5), ('size', 0),
    ('size', release.MAX_ARCHIVE_BYTES + 1), ('state', 'new'),
    ('url', 'https://api.github.com/repos/attacker/repo/releases/assets/99'),
    ('browser_download_url', 'https://github.com/attacker/repo/releases/download/v0.9.0/collector-v0.9.0.tar.gz'),
    ('browser_download_url', ASSET_URL + '?token=PRIVATE-TOKEN'),
])
def test_invalid_asset_identity_and_size(package, field, value):
    document = listing(package[0])
    document['assets'][0][field] = value
    with pytest.raises(release.ReleaseError):
        release.ReleaseClient(Opener([(release.LATEST_URL, document)])).latest()


def test_duplicate_matching_assets_are_rejected(package):
    value = listing(package[0])
    value['assets'].append(dict(value['assets'][0]))
    with pytest.raises(release.ReleaseError, match='^asset_missing$'):
        release.ReleaseClient(Opener([(release.LATEST_URL, value)])).latest()


def test_release_notes_are_bounded_and_controls_removed(package):
    value = listing(package[0])
    value['body'] = '\x00\x01' + '文' * 4000
    result = release.ReleaseClient(Opener([(release.LATEST_URL, value)])).latest()
    assert result.notes == '文' * 2000


@pytest.mark.parametrize('change', ['digest', 'id', 'size', 'deleted_asset'])
def test_download_rechecks_and_rejects_changed_asset(package, change):
    data, _ = package
    original = listing(data)
    changed = copy.deepcopy(original)
    if change == 'digest':
        changed['assets'][0]['digest'] = 'sha256:' + '0' * 64
    elif change == 'id':
        changed['assets'][0]['id'] = 100
        changed['assets'][0]['url'] = release.API_ROOT + '/releases/assets/100'
    elif change == 'size':
        changed['assets'][0]['size'] += 1
    else:
        changed['assets'] = []
    opener = Opener([(release.LATEST_URL, original), (BY_ID_URL, changed)])
    client = release.ReleaseClient(opener)
    latest = client.latest()
    with pytest.raises(release.ReleaseError, match='^(release_changed|asset_missing)$'):
        client.download(latest)
    assert len(opener.requests) == 2


def test_download_checks_exact_bytes_even_when_size_matches(package):
    data, _ = package
    value = listing(data)
    opener = Opener([(release.LATEST_URL, value), (BY_ID_URL, value),
                     (ASSET_URL, b'x' * len(data))])
    client = release.ReleaseClient(opener)
    with pytest.raises(release.ReleaseError, match='^checksum_mismatch$'):
        client.download(client.latest())


def test_caller_cannot_replace_download_url(package):
    data, _ = package
    value = listing(data)
    opener = Opener([(release.LATEST_URL, value), (BY_ID_URL, value)])
    client = release.ReleaseClient(opener)
    selected = replace(client.latest(), asset_url='https://attacker.invalid/PRIVATE-TOKEN')
    with pytest.raises(release.ReleaseError, match='^release_changed$'):
        client.download(selected)
    assert len(opener.requests) == 2


@pytest.mark.parametrize('url', [
    'http://api.github.com/repos/BSakura-Miku/ugreen-ups-panel/releases/latest',
    'https://api.github.com.evil.invalid/repos/BSakura-Miku/ugreen-ups-panel/releases/latest',
    'https://user:PRIVATE-TOKEN@github.com/BSakura-Miku/ugreen-ups-panel/releases/download/v0.9.0/SHA256SUMS',
    'https://127.0.0.1/metadata', 'file:///etc/passwd', 'ftp://github.com/file',
    'https://github.com:444/BSakura-Miku/ugreen-ups-panel/releases/download/v0.9.0/SHA256SUMS',
    ASSET_URL + '#fragment', ASSET_URL + '?token=secret',
    ASSET_URL.replace('/collector-', '/%2e%2e/collector-'),
    'https://raw.githubusercontent.com/attacker/repo/main/install.py',
    'https://objects.githubusercontent.com/not-a-release-asset',
    'https://github.com\\@attacker.invalid/file',
])
def test_redirect_rejects_untrusted_destinations_without_credential_echo(url):
    handler = release._Redirects()
    request = Request(ASSET_URL, headers={'Authorization': 'PRIVATE-TOKEN', 'Cookie': 'PRIVATE-COOKIE'})
    with pytest.raises(release.ReleaseError, match='^untrusted_url$') as error:
        handler.redirect_request(request, None, 302, '', {}, url)
    assert 'PRIVATE' not in str(error.value)


@pytest.mark.parametrize('host', ['release-assets.githubusercontent.com', 'objects.githubusercontent.com'])
def test_redirect_accepts_only_known_asset_cdn_and_replaces_all_headers(host):
    target = 'https://' + host + '/github-production-release-asset/123/abcd-ef?sig=PRIVATE-SIGNATURE'
    request = Request(ASSET_URL, headers={'Authorization': 'PRIVATE-TOKEN', 'Cookie': 'PRIVATE-COOKIE'})
    redirect = release._Redirects().redirect_request(request, None, 302, '', {}, target)
    assert redirect.full_url == target
    assert not any(k.lower() in {'authorization', 'cookie'} for k in redirect.headers)
    with pytest.raises(release.ReleaseError, match='^untrusted_url$'):
        release._trusted_url(target, initial=True)


def test_final_response_url_is_checked_even_with_injected_opener(package):
    response = Response(json.dumps(listing(package[0])).encode(), 'http://attacker.invalid')
    with pytest.raises(release.ReleaseError, match='^untrusted_url$'):
        release.ReleaseClient(Opener([(release.LATEST_URL, response)])).latest()


@pytest.mark.parametrize('body,headers,code', [
    (b'{' + b' ' * release.MAX_JSON_BYTES, {}, 'response_too_large'),
    (b'{}', {'Content-Length': str(release.MAX_JSON_BYTES + 1)}, 'response_too_large'),
    (b'{}', {'Content-Length': '2.0'}, 'invalid_response'),
    (b'{}', {'Content-Length': '3'}, 'invalid_response'),
    (b'{}', {'Content-Encoding': 'gzip'}, 'invalid_response'),
    (b'{"id":1,"id":2}', {}, 'invalid_response'),
    (b'{"id":NaN}', {}, 'invalid_response'),
    (b'{"id":"\xff"}', {}, 'invalid_response'),
])
def test_response_and_json_bounds(body, headers, code):
    response = Response(body, release.LATEST_URL, headers)
    with pytest.raises(release.ReleaseError, match='^' + code + '$'):
        release.ReleaseClient(Opener([(release.LATEST_URL, response)])).latest()


def test_network_exceptions_do_not_expose_urls_or_credentials():
    opener = Opener([(release.LATEST_URL, OSError('https://PRIVATE-TOKEN@private-host/secret'))])
    with pytest.raises(release.ReleaseError, match='^network_error$') as error:
        release.ReleaseClient(opener).latest()
    assert 'PRIVATE' not in str(error.value)
    assert error.value.__cause__ is None


def test_malformed_http_status_line_is_sanitized():
    opener = Opener([(release.LATEST_URL, BadStatusLine('PRIVATE-TOKEN response'))])
    with pytest.raises(release.ReleaseError, match='^network_error$'):
        release.ReleaseClient(opener).latest()


def test_latest_not_found_is_a_fixed_no_stable_release_error():
    error = HTTPError(release.LATEST_URL, 404, 'PRIVATE server message', {}, None)
    with pytest.raises(release.ReleaseError, match='^no_stable_release$'):
        release.ReleaseClient(Opener([(release.LATEST_URL, error)])).latest()


def test_manifest_api_digest_is_mandatory_when_present(package):
    data, _ = package
    manifest = (hashlib.sha256(data).hexdigest() + '  ' + ASSET_NAME + '\n').encode()
    value = listing(data)
    value['assets'][0]['digest'] = None
    checksum_asset = asset(manifest, 'SHA256SUMS', 100)
    checksum_asset['digest'] = 'sha256:' + '0' * 64
    value['assets'].append(checksum_asset)
    url = checksum_asset['browser_download_url']
    with pytest.raises(release.ReleaseError, match='^checksum_mismatch$'):
        release.ReleaseClient(Opener([(release.LATEST_URL, value), (url, manifest)])).latest()


def test_by_id_requires_exact_repository_release_id(package):
    value = listing(package[0])
    value['id'] = 43
    with pytest.raises(release.ReleaseError, match='^invalid_release$'):
        release.ReleaseClient(Opener([(BY_ID_URL, value)])).by_id(42)


@pytest.mark.parametrize('value', [True, 0, -1, 42.0, '42', 2 ** 63])
def test_by_id_does_not_accept_coercible_or_unbounded_ids(value):
    opener = Opener([])
    with pytest.raises(release.ReleaseError, match='^invalid_release$'):
        release.ReleaseClient(opener).by_id(value)
    assert not opener.requests


@pytest.mark.parametrize('timeout', [True, 0, -1, 16, float('inf'), float('nan'), '15'])
def test_timeout_is_finite_positive_and_at_most_fifteen(timeout):
    with pytest.raises(release.ReleaseError, match='^invalid_response$'):
        release.ReleaseClient(Opener([]), timeout=timeout)


def test_safe_proxy_configuration_without_auth(monkeypatch):
    monkeypatch.setattr(release, 'getproxies', lambda: {'https': 'http://127.0.0.1:7890', 'no': 'localhost'})
    assert release._proxy_handler().proxies == {'https': 'http://127.0.0.1:7890', 'no': 'localhost'}
    monkeypatch.setattr(release, 'getproxies', lambda: {'https': 'http://PRIVATE-USER:PRIVATE-PASSWORD@localhost:7890'})
    with pytest.raises(release.ReleaseError, match='^network_error$'):
        release._proxy_handler()


@pytest.mark.parametrize('manifest', [
    b'x' * 64 + b'  collector-v0.9.0.tar.gz\n',
    b'a' * 64 + b'  ../collector-v0.9.0.tar.gz\n',
    b'a' * 64 + b' *collector-v0.9.0.tar.gz\n',
    (b'a' * 64 + b'  collector-v0.9.0.tar.gz\n') * 2,
    b'a' * 64 + b'  collector-v0.9.1.tar.gz\n', b'\xff', b'',
])
def test_checksum_manifest_is_strict(manifest):
    with pytest.raises(release.ReleaseError, match='^invalid_digest$'):
        release.ReleaseClient._manifest(manifest, ASSET_NAME)


def test_package_is_reproducible_and_matches_source_fingerprint(source, tmp_path):
    first, meta = PACKAGER['make_package'](source, REVISION)
    for path in (source / 'ups_panel').glob('*.py'):
        os.utime(path, (12345678, 98765432))
        path.chmod(0o600)
    second, second_meta = PACKAGER['make_package'](source, REVISION)
    assert first == second and meta == second_meta
    files = {str(path.relative_to(source)): path.read_bytes() for path in (source / 'ups_panel').glob('*.py')}
    digest = hashlib.sha256()
    for path in sorted((source / 'ups_panel').glob('*.py')):
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    assert meta['source_sha256'] == digest.hexdigest() == release.source_fingerprint(files)
    result = PACKAGER['write_package'](source, tmp_path / 'output', REVISION)
    assert Path(result['archive']).read_bytes() == first
    assert Path(result['checksums']).read_text() == hashlib.sha256(first).hexdigest() + '  ' + ASSET_NAME + '\n'
    with pytest.raises(ValueError, match='already exist'):
        PACKAGER['write_package'](source, tmp_path / 'output', REVISION)


def test_extract_creates_only_code_and_metadata_without_importing(package, tmp_path):
    data, metadata = package
    destination = tmp_path / 'verified-release'
    assert release.extract_package(data, destination, expected_version=VERSION, expected_revision=REVISION,
                                   expected_source_sha256=metadata['source_sha256']) == metadata
    assert sorted(str(path.relative_to(destination)) for path in destination.rglob('*')) == [
        'LICENSE', 'collector-release.json', 'ups_panel', 'ups_panel/__init__.py',
        'ups_panel/build_info.py', 'ups_panel/collector.py']
    assert json.loads((destination / 'collector-release.json').read_text()) == metadata
    assert (destination / 'LICENSE').read_bytes() == (ROOT / 'LICENSE').read_bytes()
    assert all(path.stat().st_mode & 0o777 == 0o644 for path in destination.rglob('*') if path.is_file())


@pytest.mark.parametrize('kind', ['existing', 'symlink', 'parent_symlink', 'writable_parent', 'relative'])
def test_extract_rejects_unsafe_destination_and_preserves_existing_files(package, tmp_path, kind):
    data, _ = package
    protected = tmp_path / 'protected'
    protected.mkdir()
    sentinel = protected / 'keep.txt'
    sentinel.write_text('keep')
    if kind == 'existing':
        destination = protected
    elif kind == 'symlink':
        destination = tmp_path / 'link'
        destination.symlink_to(protected, target_is_directory=True)
    elif kind == 'parent_symlink':
        parent = tmp_path / 'link'
        parent.symlink_to(protected, target_is_directory=True)
        destination = parent / 'new'
    elif kind == 'writable_parent':
        protected.chmod(0o777)
        destination = protected / 'new'
    else:
        destination = Path('relative-new-release')
    with pytest.raises(release.ReleaseError, match='^unsafe_destination$'):
        release.extract_package(data, destination)
    assert sentinel.read_text() == 'keep'


def test_partial_extraction_is_removed_on_write_failure(package, tmp_path, monkeypatch):
    data, _ = package
    actual = release.os.fsync
    calls = []
    def fail(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError('PRIVATE storage failure')
        return actual(fd)
    monkeypatch.setattr(release.os, 'fsync', fail)
    destination = tmp_path / 'failed-release'
    with pytest.raises(release.ReleaseError, match='^package_write_failed$'):
        release.extract_package(data, destination)
    assert not destination.exists()


@pytest.mark.parametrize('path', [
    '/etc/passwd', '../escape.py', 'ups_panel/../../escape.py', 'ups_panel//bad.py',
    'ups_panel/./bad.py', 'ups_panel/subdir/bad.py', 'ups_panel\\bad.py',
    'ups_panel/bad.py/', 'ups_panel/évil.py', 'scripts/install-collector.sh',
    'host/etc/ugreen-ups-panel.env', 'ups_panel/.hidden.py', 'ups_panel/README.md',
])
def test_archive_path_and_file_allowlist(package, path, tmp_path):
    malicious = tar_bytes(files_entries(package[0]) + [(path, b'bad')])
    destination = tmp_path / 'never-created'
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.extract_package(malicious, destination)
    assert not destination.exists()


@pytest.mark.parametrize('typecode', [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE,
                                     tarfile.FIFOTYPE, tarfile.GNUTYPE_SPARSE, tarfile.XHDTYPE, tarfile.XGLTYPE])
def test_archive_rejects_links_devices_sparse_and_pax(package, typecode):
    info = tarfile.TarInfo('ups_panel/extra.py')
    info.type = typecode
    if typecode in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
        info.linkname = '/etc/passwd'
    malicious = tar_bytes(files_entries(package[0]) + [(info, b'')])
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.inspect_package(malicious)


def test_real_pax_path_override_is_not_hidden_by_tarfile(package):
    info = tarfile.TarInfo('ups_panel/extra.py')
    info.pax_headers = {'path': '../outside.py'}
    malicious = tar_bytes(files_entries(package[0]) + [(info, b'')], format=tarfile.PAX_FORMAT)
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.inspect_package(malicious)


@pytest.mark.parametrize('case', ['duplicate', 'filecount', 'file_size', 'expanded_size', 'gzip_bomb',
                                  'zip', 'truncated', 'trailer', 'concatenated', 'checksum', 'gnu', 'special_mode'])
def test_archive_resource_bounds_and_format_rejections(package, case):
    data, _ = package
    entries = files_entries(data)
    if case == 'duplicate':
        malicious = tar_bytes(entries + [entries[0]])
    elif case == 'filecount':
        malicious = tar_bytes(entries + [('ups_panel/file_' + str(i) + '.py', b'') for i in range(release.MAX_MEMBERS)])
    elif case == 'file_size':
        malicious = tar_bytes(entries + [('ups_panel/large.py', b'x' * (release.MAX_SOURCE_BYTES + 1))])
    elif case == 'expanded_size':
        malicious = tar_bytes([('ups_panel/file_' + str(i) + '.py', b'x' * release.MAX_SOURCE_BYTES) for i in range(9)])
    elif case == 'gzip_bomb':
        malicious = gzip.compress(b'\0' * (release.MAX_TAR_BYTES + 1))
    elif case == 'zip':
        malicious = b'PK\x03\x04zip archive'
    elif case == 'truncated':
        malicious = data[:-10]
    elif case == 'trailer':
        malicious = gzip.compress(gzip.decompress(data)[:-1] + b'x')
    elif case == 'concatenated':
        malicious = data + data
    elif case == 'checksum':
        raw = bytearray(gzip.decompress(data))
        raw[0] ^= 1
        malicious = gzip.compress(raw)
    elif case == 'gnu':
        malicious = tar_bytes(entries, format=tarfile.GNU_FORMAT)
    else:
        info = tarfile.TarInfo('ups_panel/extra.py')
        info.mode = 0o4755
        malicious = tar_bytes(entries + [(info, b'')])
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.inspect_package(malicious)


def test_archive_compressed_size_is_bounded_before_decompression():
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.inspect_package(b'x' * (release.MAX_ARCHIVE_BYTES + 1))


@pytest.mark.parametrize('field,value,code', [
    ('schema', True, 'invalid_package'), ('schema', 1.0, 'invalid_package'),
    ('minimum_updater_schema', True, 'invalid_package'),
    ('minimum_updater_schema', 2, 'incompatible_package'),
    ('calibration_schemas', [True, 2], 'invalid_package'),
    ('calibration_schemas', [1, 1, 2], 'invalid_package'),
    ('calibration_schemas', [1], 'incompatible_package'),
    ('revision', 'a' * 39, 'invalid_package'), ('revision', 'A' * 40, 'invalid_package'),
    ('source_sha256', '0' * 64, 'invalid_package'),
    ('version', '0.9.1', 'invalid_package'), ('version', '0.9.0-beta', 'invalid_package'),
    ('unknown', 'ignored?', 'invalid_package'),
])
def test_metadata_schema_and_identity_are_strict(package, field, value, code):
    malicious = changed_metadata(package[0], {field: value})
    with pytest.raises(release.ReleaseError, match='^' + code + '$'):
        release.inspect_package(malicious)


def test_duplicate_metadata_json_keys_are_rejected(package):
    malicious = changed_metadata(package[0], raw=b'{"schema":1,"schema":1}')
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.inspect_package(malicious)


@pytest.mark.parametrize('expected', [{'expected_version': '0.9.1'}, {'expected_revision': 'b' * 40},
                                     {'expected_source_sha256': '0' * 64}, {'expected_version': True}])
def test_verified_package_must_match_selected_release_identity(package, expected):
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.inspect_package(package[0], **expected)


@pytest.mark.parametrize('content', [b'\xff', b'valid\0invalid'])
def test_python_sources_must_be_strict_utf8_without_nul(package, content):
    entries = files_entries(package[0]) + [('ups_panel/new_file.py', content)]
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.inspect_package(tar_bytes(entries))


@pytest.mark.parametrize('source', [
    b'VERSION = compute_version()\n', b'VERSION = "0.9.0"\nVERSION = "0.9.1"\n',
    b'if True:\n    VERSION = "0.9.0"\n', b'VERSION = "0.9.0"\nVERSION += "-dev"\n',
    b'VERSION = "0.9.0"\ndel VERSION\n', b'VERSION = 0.9\n',
    b'VERSION = "v0.9.0"\n', b'VERSION = "0.9.0"\n\xff',
])
def test_version_is_a_single_safe_ast_string_constant(source):
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.read_source_version(source)


@pytest.mark.parametrize('kind', ['source', 'file', 'ignored', 'license', 'output'])
def test_packager_rejects_every_source_and_output_symlink(source, tmp_path, kind):
    if kind == 'source':
        link = tmp_path / 'source-link'
        link.symlink_to(source, target_is_directory=True)
        root, output = link, tmp_path / 'output'
    elif kind in {'file', 'ignored'}:
        name = 'linked.py' if kind == 'file' else 'ignored.data'
        (source / 'ups_panel' / name).symlink_to(source / 'ups_panel/collector.py')
        root, output = source, tmp_path / 'output'
    elif kind == 'license':
        (source / 'LICENSE').unlink()
        (source / 'LICENSE').symlink_to(ROOT / 'LICENSE')
        root, output = source, tmp_path / 'output'
    else:
        actual = tmp_path / 'actual-output'
        actual.mkdir()
        output = tmp_path / 'output-link'
        output.symlink_to(actual, target_is_directory=True)
        root = source
    with pytest.raises(ValueError, match='symlink'):
        PACKAGER['write_package'](root, output, REVISION)


@pytest.mark.parametrize('revision', ['a' * 7, 'A' * 40, 'x' * 40, 'a' * 40 + ';touch nope', True])
def test_packager_requires_full_hex_revision(source, revision):
    with pytest.raises(ValueError, match='40 lowercase hex'):
        PACKAGER['make_package'](source, revision)


def test_packager_version_from_source_must_be_supported(source):
    (source / 'ups_panel/build_info.py').write_text('VERSION = "0.8.0"\n')
    with pytest.raises(ValueError, match='0.9.0 or later'):
        PACKAGER['make_package'](source, REVISION)


@pytest.mark.parametrize('content', [None, b'', b'\xff', b'license\0invalid', b'x' * (release.MAX_LICENSE_BYTES + 1)])
def test_license_is_required_bounded_utf8_data(package, content):
    entries = [(name, value) for name, value in files_entries(package[0]) if name != 'LICENSE']
    if content is not None:
        entries.append(('LICENSE', content))
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.inspect_package(tar_bytes(entries))


@pytest.mark.parametrize('typecode', [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE])
def test_license_cannot_be_a_link_or_directory(package, typecode):
    entries = [(name, value) for name, value in files_entries(package[0]) if name != 'LICENSE']
    info = tarfile.TarInfo('LICENSE')
    info.type = typecode
    if typecode in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
        info.linkname = 'ups_panel/collector.py'
    with pytest.raises(release.ReleaseError, match='^invalid_package$'):
        release.inspect_package(tar_bytes(entries + [(info, b'')]))


def test_license_is_not_part_of_python_source_fingerprint(package):
    data, metadata = package
    altered_license = b'MIT License\nCopyright additional contributor\n'
    changed = tar_bytes([(name, altered_license if name == 'LICENSE' else value)
                         for name, value in files_entries(data)])
    validated = release.inspect_package(changed)
    assert validated.metadata == metadata
    assert validated.license == altered_license
    assert 'LICENSE' not in validated.files
    assert release.source_fingerprint(validated.files) == metadata['source_sha256']
    assert hashlib.sha256(changed).digest() != hashlib.sha256(data).digest()


@pytest.mark.parametrize('content', [None, b'', b'\xff', b'x' * (release.MAX_LICENSE_BYTES + 1)])
def test_packager_rejects_missing_or_invalid_license(source, content):
    path = source / 'LICENSE'
    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)
    with pytest.raises((ValueError, release.ReleaseError)):
        PACKAGER['make_package'](source, REVISION)
