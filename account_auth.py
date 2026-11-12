"""V380 account QR authorization and camera bootstrap without a desktop client.

Only call these functions for accounts and devices the operator is authorized to use.
The vendor endpoints use their native HTTP protocol. Never log request bodies.
"""
from __future__ import annotations
import base64
import hashlib
import http.client
import json
import re
import secrets
import socket
import time
from urllib.parse import urlsplit, urlencode
from interop_constants import (ACCOUNT_BASE_URL, ACCOUNT_SIGNATURE_SUFFIX,
                               NEO_SIGNATURE_SUFFIX, DEFAULT_DISPATCH_PLATFORM,
                               DISPATCH_SIGNATURE_SUFFIX)

class AccountError(RuntimeError):
    """A sanitized account failure, with no token or raw response attached."""


def _request(host, port, path, body, *, secure=False, form=False):
    connection = (http.client.HTTPSConnection if secure else http.client.HTTPConnection)(host, port, timeout=8)
    try:
        payload = urlencode(body).encode() if form else json.dumps(body, separators=(',', ':')).encode()
        content_type = 'application/x-www-form-urlencoded' if form else 'application/json'
        connection.request('POST', path, payload,
                           {'Content-Type': content_type, 'Connection': 'close'})
        response = connection.getresponse()
        if response.status != 200:
            raise AccountError('Account HTTP status ' + str(response.status))
        raw = response.read(512 * 1024 + 1)
        if len(raw) > 512 * 1024:
            raise AccountError('Account response exceeds the size limit')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise AccountError('Unexpected account response')
        return result
    except (OSError, http.client.HTTPException, ValueError):
        raise AccountError('Account service unavailable or invalid response') from None
    finally:
        connection.close()


def _account_request(path, fields):
    fields = dict(fields, timestamp=str(int(time.time())))
    material = '&'.join(k + '=' + str(v) for k, v in sorted(fields.items()))
    fields['sign'] = hashlib.md5((material + ACCOUNT_SIGNATURE_SUFFIX).encode()).hexdigest()
    endpoint = urlsplit(ACCOUNT_BASE_URL)
    return _request(endpoint.hostname, endpoint.port or 80, path, fields)


def _papi_request(path, fields):
    fields = dict(fields, timestamp=str(int(time.time())))
    material = '&'.join(k + '=' + str(v) for k, v in sorted(fields.items()))
    fields['sign'] = hashlib.md5((material + ACCOUNT_SIGNATURE_SUFFIX).encode()).hexdigest()
    return _request('papi.av380.net', 443, path, fields, secure=True, form=True)


def refresh_public_key(token, device_id):
    """Refresh a camera's current public key through the native HTTPS endpoint."""
    if not isinstance(device_id, int) or not 0 < device_id <= 0xffffffff:
        raise AccountError('Invalid camera identifier')
    result = _check(_papi_request('/device/renew-public-key',
                    {'accesstoken': _token(token), 'deviceid': str(device_id)}))
    key = result.get('public_key')
    try:
        if not isinstance(key, str) or len(base64.b64decode(key, validate=True)) != 32:
            raise ValueError()
    except ValueError:
        raise AccountError('Camera public key refresh returned an invalid key') from None
    return key


def _check(result):
    if result.get('result') != 0:
        code = result.get('error_code', result.get('result'))
        if not isinstance(code, int):
            code = 'unknown'
        raise AccountError('Account API code ' + str(code))
    return result


def _token(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', value):
        raise AccountError('Invalid account session')
    return value


def start_qr():
    """Return an ephemeral QR session. Encode its payload as a QR image."""
    mac = ':'.join(f'{x:02X}' for x in (b'\x02' + secrets.token_bytes(5)))
    result = _account_request('/qrcode/check', {'mac': mac, 'key': ''})
    key = result.get('key')
    if not isinstance(key, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', key):
        raise AccountError('Account service did not issue a login QR code')
    return {'mac': mac, 'key': key, 'payload': 'V380LOGIN:' + key, 'created_at': time.time()}


def poll_qr(session):
    """Return pending, scanned, expired, or confirmed (with an account object)."""
    result = _account_request('/qrcode/check', {'mac': session['mac'], 'key': session['key']})
    if result.get('result') == 0:
        data = result.get('data')
        if not isinstance(data, dict):
            raise AccountError('Missing account session')
        return {'state': 'confirmed', 'account': {'token': _token(data.get('access_token')),
                'user_id': data.get('user_id'), 'position': result.get('user_position', '')}}
    code = result.get('error_code')
    if result.get('result') == 10022:
        state = {30001: 'pending', 30002: 'expired', 30003: 'scanned', 30004: 'scanned'}.get(code)
        if state:
            return {'state': state}
    _check(result)
    raise AccountError('Unexpected QR login state')


def list_cameras(token):
    """Return authorized device metadata, including its public X25519 key."""
    result = _check(_account_request('/device/list', {'accesstoken': _token(token),
                      'type': 'all', 'updatetimestamp': '0'}))
    devices = result.get('data')
    if not isinstance(devices, list):
        raise AccountError('Account did not return a camera list')
    output = []
    for item in devices:
        if not isinstance(item, dict):
            continue
        device_id = item.get('device_id')
        key = item.get('public_key')
        if not isinstance(device_id, int) or not 0 < device_id <= 0xffffffff:
            continue
        try:
            if not isinstance(key, str) or len(base64.b64decode(key, validate=True)) != 32:
                continue
        except ValueError:
            continue
        output.append({'device_id': device_id, 'device_public_key': key,
                       'name': str(item.get('nickname') or 'Camera')[:128],
                       'model': str(item.get('model') or '')[:128]})
    return output


def build_camera_credentials(token, camera):
    """Discover vendor controllers/relays and build the direct receiver config."""
    token = _token(token)
    device_id = camera['device_id']
    # Re-fetch the authorized list instead of accepting a caller-supplied key/ID.
    cameras = list_cameras(token)
    selected = next((c for c in cameras if c['device_id'] == device_id), None)
    if selected is None:
        raise AccountError('Camera is not available in this account')
    services = _check(_account_request('/tool/get-iot-server-list', {'accesstoken': token}))
    servers = services.get('server_list', {})
    controller = servers.get('key', 'secret.av380.net:80')
    if not isinstance(controller, str):
        raise AccountError('Invalid camera controller')
    parsed = urlsplit('http://' + controller)
    host = parsed.hostname
    if not host or (parsed.port or 80) != 80:
        raise AccountError('Unsupported camera controller endpoint')
    try:
        addresses = socket.getaddrinfo(host, 80, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        raise AccountError('Camera controller DNS lookup failed') from None
    media = urlsplit('http://' + servers.get('media', 'dispatch.av380.net:8001'))
    if not media.hostname or (media.port or 8001) != 8001:
        raise AccountError('Unsupported relay discovery endpoint')
    stamp = int(time.time())
    platform = DEFAULT_DISPATCH_PLATFORM
    material = f'dev_id={device_id}&platform={platform}&timestamp={stamp}' + DISPATCH_SIGNATURE_SUFFIX
    dispatch = _request(media.hostname, media.port or 8001, '/api/v1/get_stream_server',
                        {'dev_id': device_id, 'platform': platform, 'timestamp': stamp,
                         'sign': hashlib.sha1(material.encode()).hexdigest()})
    relays = dispatch.get('data')
    if dispatch.get('code') != 2000 or not isinstance(relays, list) or not relays:
        raise AccountError('Camera video relay discovery failed')
    return {'token': token, 'device_id': device_id,
            'device_public_key': selected['device_public_key'],
            'signature_suffix': NEO_SIGNATURE_SUFFIX,
            'secret_host': host, 'secret_ip': addresses[0][4][0],
            'secret_ips': list(dict.fromkeys(x[4][0] for x in addresses)),
            'secret_path': '/v1/app/exch', 'call_path': '/v1/app/call', 'call_value': 4,
            'dispatch_host': media.hostname, 'dispatch_port': media.port or 8001,
            'dispatch_platform': platform, 'dispatch_salt': DISPATCH_SIGNATURE_SUFFIX,
            'relays': relays}
