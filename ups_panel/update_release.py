"""Fixed-repository collector releases and non-executing package validation.

Only the public GitHub repository below is a release authority. No API accepts a
user-selected repository or URL. Downloaded Python is parsed as data, never
imported here. Archive parsing deliberately supports only our small USTAR/gzip
format: ZIP, PAX, GNU extensions, links and special files are rejected.
"""
import ast
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
from http.client import HTTPException
import json
import math
import os
from pathlib import Path
import re
import tarfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, getproxies
import zlib

REPOSITORY = 'BSakura-Miku/ugreen-ups-panel'
API_ROOT = 'https://api.github.com/repos/' + REPOSITORY
LATEST_URL = API_ROOT + '/releases/latest'
WEB_ROOT = 'https://github.com/' + REPOSITORY
UPDATER_SCHEMA = 1
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_CONTENT_BYTES = 4 * 1024 * 1024
MAX_SOURCE_BYTES = 512 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_METADATA_BYTES = 16384
MAX_LICENSE_BYTES = 32 * 1024
MAX_MEMBERS = 128
MAX_TAR_BYTES = MAX_CONTENT_BYTES + MAX_MEMBERS * 1024 + 10240
MAX_NOTES_CHARS = 2000
MAX_TIMEOUT = 15
_VERSION = r'(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})'
_PY_NAME = re.compile(r'ups_panel/[A-Za-z_][A-Za-z0-9_]{0,63}\.py\Z')
ERROR_CODES = frozenset({
    'network_error', 'response_too_large', 'untrusted_url', 'invalid_response',
    'no_stable_release', 'invalid_release', 'asset_missing', 'invalid_digest',
    'checksum_mismatch', 'release_changed', 'invalid_package',
    'incompatible_package', 'unsafe_destination', 'package_write_failed',
})


class ReleaseError(Exception):
    """A fixed public error code, without server response/URL/credential text."""

    def __init__(self, code):
        self.code = code if code in ERROR_CODES else 'invalid_response'
        super().__init__(self.code)


def _require(value, code):
    if not value:
        raise ReleaseError(code)


def _integer(value, minimum=1, maximum=2 ** 63 - 1):
    return type(value) is int and minimum <= value <= maximum


def version_tuple(value):
    _require(isinstance(value, str) and re.fullmatch(_VERSION, value), 'invalid_release')
    return tuple(int(part) for part in value.split('.'))


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{' + str(length) + '}', value) is not None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _json(data, limit, code):
    _require(isinstance(data, bytes) and len(data) <= limit, code)
    try:
        value = json.loads(data.decode('utf-8', 'strict'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        _require(type(value) is dict, code)
        return value
    except (UnicodeError, ValueError, RecursionError, TypeError):
        raise ReleaseError(code) from None


def _trusted_url(url, *, initial=False):
    """Validate before following a redirect, and again on the final response."""
    _require(isinstance(url, str) and 0 < len(url) <= 16384
             and url.isascii() and not any(ord(c) <= 32 or ord(c) == 127 for c in url)
             and '\\' not in url, 'untrusted_url')
    try:
        parts = urlsplit(url)
        _require(parts.scheme == 'https' and parts.username is None and parts.password is None
                 and parts.port is None and not parts.fragment and '%' not in parts.path,
                 'untrusted_url')
        host, path = parts.hostname, parts.path
    except (ValueError, UnicodeError):
        raise ReleaseError('untrusted_url') from None
    if host == 'api.github.com':
        prefix = '/repos/' + REPOSITORY + '/releases/'
        allowed = (path == prefix + 'latest' or
                   re.fullmatch(re.escape(prefix) + r'[1-9][0-9]{0,18}', path) or
                   re.fullmatch(re.escape(prefix) + r'assets/[1-9][0-9]{0,18}', path))
        _require(allowed and not parts.query, 'untrusted_url')
    elif host == 'github.com':
        pattern = (re.escape('/' + REPOSITORY + '/releases/download/v') + _VERSION
                   + r'/(?:collector-v' + _VERSION + r'\.tar\.gz|SHA256SUMS)')
        _require(re.fullmatch(pattern, path) and not parts.query, 'untrusted_url')
    else:
        _require(not initial and host in {'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}
                 and re.fullmatch(r'/github-production-release-asset(?:-[0-9]+)?/[0-9]+/[A-Za-z0-9._-]+', path)
                 and len(parts.query) <= 8192, 'untrusted_url')
    return url


def _headers(url):
    headers = {'User-Agent': 'ugreen-ups-collector-updater/1', 'Accept-Encoding': 'identity'}
    if urlsplit(url).hostname == 'api.github.com':
        headers.update({'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    else:
        headers['Accept'] = 'application/octet-stream'
    return headers


class _Redirects(HTTPRedirectHandler):
    max_redirections = 5
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _trusted_url(newurl)
        _require(req.get_method() == 'GET' and code in (301, 302, 303, 307, 308), 'untrusted_url')
        # Construct fresh fixed headers; never forward a credential or cookie.
        return Request(newurl, headers=_headers(newurl), method='GET')


def _proxy_handler():
    # Support a NAS's configured HTTP(S) proxy, but never use proxy credentials.
    proxies = {}
    for protocol, value in getproxies().items():
        if protocol == 'no':
            proxies['no'] = value
        elif protocol in {'http', 'https'}:
            try:
                parsed = urlsplit(value)
                _require(parsed.scheme in {'http', 'https'} and parsed.hostname
                         and parsed.username is None and parsed.password is None
                         and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment
                         and (parsed.port is None or 1 <= parsed.port <= 65535)
                         and not any(ord(c) <= 32 for c in value), 'network_error')
            except (ValueError, UnicodeError):
                raise ReleaseError('network_error') from None
            proxies[protocol] = value
    return ProxyHandler(proxies)


@dataclass(frozen=True)
class CollectorRelease:
    version: str
    tag: str
    release_id: int
    asset_id: int
    asset_name: str
    asset_url: str
    size: int
    sha256: str
    release_url: str
    published_at: str
    notes: str

    def as_dict(self):
        return asdict(self)


class ReleaseClient:
    """Small bounded public client. Tests may inject an opener.open(req, timeout)."""

    def __init__(self, opener=None, timeout=MAX_TIMEOUT):
        _require(type(timeout) in {int, float} and math.isfinite(timeout)
                 and 0 < timeout <= MAX_TIMEOUT, 'invalid_response')
        self.timeout = timeout
        self.opener = opener if opener is not None else build_opener(_proxy_handler(), _Redirects())

    def _read(self, url, limit):
        _trusted_url(url, initial=True)
        request = Request(url, headers=_headers(url), method='GET')
        started = time.monotonic()
        try:
            open_url = self.opener.open if hasattr(self.opener, 'open') else self.opener
            with open_url(request, timeout=self.timeout) as response:
                _trusted_url(response.geturl())
                _require(response.getcode() == 200, 'network_error')
                encoding = response.headers.get('Content-Encoding', 'identity')
                _require(encoding.lower() == 'identity', 'invalid_response')
                declared = response.headers.get('Content-Length')
                if declared is not None:
                    _require(re.fullmatch(r'[0-9]{1,12}', declared), 'invalid_response')
                    _require(int(declared) <= limit, 'response_too_large')
                chunks, length = [], 0
                while True:
                    _require(time.monotonic() - started <= self.timeout, 'network_error')
                    part = response.read(min(65536, limit + 1 - length))
                    _require(isinstance(part, bytes), 'invalid_response')
                    if not part:
                        break
                    length += len(part)
                    _require(length <= limit, 'response_too_large')
                    chunks.append(part)
                if declared is not None:
                    _require(length == int(declared), 'invalid_response')
                return b''.join(chunks)
        except ReleaseError:
            raise
        except HTTPError as error:
            code = 'no_stable_release' if error.code == 404 and url == LATEST_URL else 'network_error'
            raise ReleaseError(code) from None
        except (OSError, URLError, HTTPException, ValueError, TypeError, TimeoutError):
            raise ReleaseError('network_error') from None

    def latest(self):
        return self._release(_json(self._read(LATEST_URL, MAX_JSON_BYTES), MAX_JSON_BYTES, 'invalid_response'))

    def by_id(self, release_id):
        _require(_integer(release_id), 'invalid_release')
        release = self._release(_json(self._read(API_ROOT + '/releases/' + str(release_id), MAX_JSON_BYTES),
                                      MAX_JSON_BYTES, 'invalid_response'))
        _require(release.release_id == release_id, 'invalid_release')
        return release

    def _asset(self, assets, name, version, limit):
        matches = [asset for asset in assets if type(asset) is dict and asset.get('name') == name]
        _require(len(matches) == 1, 'asset_missing')
        asset = matches[0]
        _require(_integer(asset.get('id')) and _integer(asset.get('size'), maximum=limit)
                 and asset.get('state') == 'uploaded', 'invalid_release')
        _require(asset.get('url') == API_ROOT + '/releases/assets/' + str(asset['id'])
                 and asset.get('browser_download_url') == WEB_ROOT + '/releases/download/v' + version + '/' + name,
                 'untrusted_url')
        _trusted_url(asset['browser_download_url'], initial=True)
        return asset

    def _release(self, value):
        _require(value.get('draft') is False and value.get('prerelease') is False, 'no_stable_release')
        tag = value.get('tag_name')
        _require(isinstance(tag, str) and re.fullmatch('v' + _VERSION, tag), 'invalid_release')
        version = tag[1:]
        _require(version_tuple(version) >= (0, 9, 0) and _integer(value.get('id')), 'invalid_release')
        release_url = WEB_ROOT + '/releases/tag/' + tag
        _require(value.get('html_url') == release_url, 'untrusted_url')
        published = value.get('published_at')
        _require(isinstance(published, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', published),
                 'invalid_release')
        try:
            datetime.strptime(published, '%Y-%m-%dT%H:%M:%SZ')
        except ValueError:
            raise ReleaseError('invalid_release') from None
        assets = value.get('assets')
        _require(type(assets) is list and len(assets) <= 128 and all(type(item) is dict for item in assets),
                 'invalid_release')
        name = 'collector-' + tag + '.tar.gz'
        asset = self._asset(assets, name, version, MAX_ARCHIVE_BYTES)
        digest = asset.get('digest')
        if digest is not None:
            _require(isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', digest), 'invalid_digest')
            checksum = digest[7:]
        else:
            manifest = self._asset(assets, 'SHA256SUMS', version, MAX_MANIFEST_BYTES)
            contents = self._read(manifest['browser_download_url'], MAX_MANIFEST_BYTES)
            _require(len(contents) == manifest['size'], 'invalid_response')
            manifest_digest = manifest.get('digest')
            if manifest_digest is not None:
                _require(isinstance(manifest_digest, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', manifest_digest),
                         'invalid_digest')
                _require(hashlib.sha256(contents).hexdigest() == manifest_digest[7:], 'checksum_mismatch')
            checksum = self._manifest(contents, name)
        notes = value.get('body')
        _require(notes is None or isinstance(notes, str), 'invalid_release')
        # Release Markdown is data; callers must render it with their safe renderer.
        notes = ''.join(c for c in (notes or '') if c in '\n\t' or ord(c) >= 32)[:MAX_NOTES_CHARS]
        return CollectorRelease(version, tag, value['id'], asset['id'], name,
                                asset['browser_download_url'], asset['size'], checksum,
                                release_url, published, notes)

    @staticmethod
    def _manifest(contents, name):
        try:
            lines = contents.decode('ascii', 'strict').splitlines()
        except UnicodeError:
            raise ReleaseError('invalid_digest') from None
        _require(0 < len(lines) <= 128, 'invalid_digest')
        found = {}
        for line in lines:
            match = re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,127})', line)
            _require(match is not None and match[2] not in found, 'invalid_digest')
            found[match[2]] = match[1]
        _require(name in found, 'invalid_digest')
        return found[name]

    def download(self, release):
        _require(isinstance(release, CollectorRelease), 'invalid_release')
        current = self.by_id(release.release_id)
        identity = ('version', 'tag', 'release_id', 'asset_id', 'asset_name', 'asset_url', 'size', 'sha256')
        _require(all(getattr(current, key) == getattr(release, key) for key in identity), 'release_changed')
        data = self._read(current.asset_url, MAX_ARCHIVE_BYTES)
        _require(len(data) == current.size, 'invalid_response')
        _require(hashlib.sha256(data).hexdigest() == current.sha256, 'checksum_mismatch')
        return data


def read_source_version(source):
    """Read exactly one literal module-level VERSION; never evaluate source."""
    _require(isinstance(source, bytes) and 0 < len(source) <= MAX_SOURCE_BYTES, 'invalid_package')
    try:
        tree = ast.parse(source.decode('utf-8', 'strict'))
        assignments = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id == 'VERSION':
                    assignments.append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == 'VERSION':
                assignments.append(node.value)
        stores = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == 'VERSION'
                  and isinstance(node.ctx, (ast.Store, ast.Del))]
        _require(len(assignments) == len(stores) == 1 and isinstance(assignments[0], ast.Constant)
                 and isinstance(assignments[0].value, str), 'invalid_package')
        version = assignments[0].value
        _require(re.fullmatch(_VERSION, version) is not None, 'invalid_package')
        return version
    except (SyntaxError, UnicodeError, ValueError, TypeError, RecursionError):
        raise ReleaseError('invalid_package') from None


def source_fingerprint(files):
    """Match build_info._source_digest: basename NUL bytes NUL, sorted by name."""
    digest = hashlib.sha256()
    for name in sorted(files):
        _require(isinstance(name, str) and _PY_NAME.fullmatch(name), 'invalid_package')
        content = files[name]
        _require(isinstance(content, bytes), 'invalid_package')
        digest.update(name.split('/')[1].encode('utf-8') + b'\0')
        digest.update(content)
        digest.update(b'\0')
    return digest.hexdigest()


@dataclass(frozen=True)
class VerifiedPackage:
    metadata: dict
    files: dict
    license: bytes


def _tar_files(data):
    _require(isinstance(data, bytes) and 0 < len(data) <= MAX_ARCHIVE_BYTES, 'invalid_package')
    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = decompressor.decompress(data, MAX_TAR_BYTES + 1)
        _require(len(raw) <= MAX_TAR_BYTES and decompressor.eof and not decompressor.unused_data
                 and not decompressor.unconsumed_tail, 'invalid_package')
        _require(len(raw) % 512 == 0, 'invalid_package')
        files, seen, offset, total = {}, set(), 0, 0
        while offset + 512 <= len(raw):
            header = raw[offset:offset + 512]
            if header == b'\0' * 512:
                _require(len(raw) - offset >= 1024 and not any(raw[offset:]), 'invalid_package')
                return files
            _require(header[257:263] == b'ustar\0' and header[263:265] == b'00', 'invalid_package')
            member = tarfile.TarInfo.frombuf(header, encoding='utf-8', errors='strict')
            # Inspect raw member types: TarFile normally hides PAX/GNU records.
            _require(member.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE)
                     and not member.linkname and member.mode & 0o7000 == 0
                     and member.size >= 0, 'invalid_package')
            name = member.name
            directory = member.type == tarfile.DIRTYPE
            key = name.rstrip('/') if directory else name
            _require(key not in seen and len(seen) < MAX_MEMBERS, 'invalid_package')
            seen.add(key)
            if directory:
                _require(name in ('ups_panel', 'ups_panel/') and member.size == 0, 'invalid_package')
            else:
                _require(name in {'collector-release.json', 'LICENSE'} or _PY_NAME.fullmatch(name), 'invalid_package')
                maximum = (MAX_METADATA_BYTES if name == 'collector-release.json' else
                           MAX_LICENSE_BYTES if name == 'LICENSE' else MAX_SOURCE_BYTES)
                _require(0 <= member.size <= maximum, 'invalid_package')
                total += member.size
                _require(total <= MAX_CONTENT_BYTES, 'invalid_package')
            begin, end = offset + 512, offset + 512 + member.size
            padded = ((end + 511) // 512) * 512
            _require(padded <= len(raw) and not any(raw[end:padded]), 'invalid_package')
            if not directory:
                files[name] = raw[begin:end]
            offset = padded
        raise ReleaseError('invalid_package')
    except (zlib.error, tarfile.TarError, UnicodeError, ValueError, OverflowError):
        raise ReleaseError('invalid_package') from None


def inspect_package(data, *, expected_version=None, expected_revision=None,
                    expected_source_sha256=None, updater_schema=UPDATER_SCHEMA):
    files = _tar_files(data)
    _require('collector-release.json' in files and 'LICENSE' in files and 'ups_panel/build_info.py' in files
             and 'ups_panel/__init__.py' in files and 'ups_panel/collector.py' in files, 'invalid_package')
    license_text = files.pop('LICENSE')
    _require(0 < len(license_text) <= MAX_LICENSE_BYTES and b'\0' not in license_text, 'invalid_package')
    try:
        license_text.decode('utf-8', 'strict')
    except UnicodeError:
        raise ReleaseError('invalid_package') from None
    metadata = _json(files.pop('collector-release.json'), MAX_METADATA_BYTES, 'invalid_package')
    fields = {'schema', 'version', 'revision', 'source_sha256', 'minimum_updater_schema', 'calibration_schemas'}
    _require(set(metadata) == fields and type(metadata['schema']) is int and metadata['schema'] == 1
             and isinstance(metadata['version'], str) and re.fullmatch(_VERSION, metadata['version'])
             and _hex(metadata['revision'], 40) and _hex(metadata['source_sha256'], 64)
             and _integer(metadata['minimum_updater_schema'], maximum=2 ** 31 - 1)
             and type(metadata['calibration_schemas']) is list
             and all(_integer(item, maximum=2 ** 31 - 1) for item in metadata['calibration_schemas'])
             and len(set(metadata['calibration_schemas'])) == len(metadata['calibration_schemas']),
             'invalid_package')
    _require(_integer(updater_schema, maximum=2 ** 31 - 1)
             and metadata['minimum_updater_schema'] <= updater_schema
             and metadata['calibration_schemas'] == [1, 2]
             and version_tuple(metadata['version']) >= (0, 9, 0), 'incompatible_package')
    for name, content in files.items():
        try:
            _require(b'\0' not in content, 'invalid_package')
            content.decode('utf-8', 'strict')
        except UnicodeError:
            raise ReleaseError('invalid_package') from None
    _require(read_source_version(files['ups_panel/build_info.py']) == metadata['version']
             and source_fingerprint(files) == metadata['source_sha256'], 'invalid_package')
    for expected, actual in ((expected_version, metadata['version']), (expected_revision, metadata['revision']),
                             (expected_source_sha256, metadata['source_sha256'])):
        _require(expected is None or (isinstance(expected, str) and expected == actual), 'invalid_package')
    return VerifiedPackage(metadata, files, license_text)


def _open_parent(destination):
    """Walk parent directories with dirfds/O_NOFOLLOW; do not resolve symlinks."""
    path = Path(destination)
    _require(path.is_absolute() and '..' not in path.parts and path.name not in {'', '.', '..'}, 'unsafe_destination')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parent.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        status = os.fstat(fd)
        _require(status.st_uid == os.geteuid() and status.st_mode & 0o022 == 0, 'unsafe_destination')
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def extract_package(data, destination, **expected):
    """Validate first, then create a new code-only directory without extractall.

    The destination must be absolute, absent, and inside an existing directory
    owned by the updater user that is not writable by another user/group. The
    helper is responsible for atomically selecting this verified release later.
    """
    package = inspect_package(data, **expected)
    parent_fd = release_fd = source_fd = None
    created = False
    written = []
    try:
        parent_fd, name = _open_parent(destination)
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        created = True
        release_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        os.mkdir('ups_panel', mode=0o755, dir_fd=release_fd)
        source_fd = os.open('ups_panel', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=release_fd)
        content = dict(package.files)
        content['LICENSE'] = package.license
        content['collector-release.json'] = (json.dumps(package.metadata, sort_keys=True, separators=(',', ':')) + '\n').encode()
        for path, value in sorted(content.items()):
            directory_fd = source_fd if path.startswith('ups_panel/') else release_fd
            filename = path.rsplit('/', 1)[-1]
            fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
            written.append((directory_fd, filename))
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(value)
                    stream.flush()
                    os.fchmod(stream.fileno(), 0o644)
                    os.fsync(stream.fileno())
            except BaseException:
                raise
        os.fchmod(source_fd, 0o755)
        os.fsync(source_fd)
        os.fchmod(release_fd, 0o755)
        os.fsync(release_fd)
        os.fsync(parent_fd)
        return dict(package.metadata)
    except (OSError, ValueError, TypeError, ReleaseError) as error:
        # Cleanup uses open directory descriptors, never an archive-controlled path.
        if created:
            for directory_fd, filename in reversed(written):
                try:
                    os.unlink(filename, dir_fd=directory_fd)
                except OSError:
                    pass
            if release_fd is not None:
                try:
                    os.rmdir('ups_panel', dir_fd=release_fd)
                except OSError:
                    pass
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        if isinstance(error, ReleaseError):
            raise
        raise ReleaseError('package_write_failed' if created else 'unsafe_destination') from None
    finally:
        for fd in (source_fd, release_fd, parent_fd):
            if fd is not None:
                os.close(fd)
