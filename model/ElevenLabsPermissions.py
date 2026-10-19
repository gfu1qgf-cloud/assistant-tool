"""One key pool, tri-state endpoint permissions, and non-billable inspection.

The public ordinary-user API does not expose the key's complete scopes. A
successful user/subscription lookup must never imply TTS/alignment permission.
Only explicit permission errors or successful real operations establish those.
"""
import json
import math
import time
import urllib.error
import urllib.request

TTS = 'text_to_speech'
ALIGNMENT = 'forced_alignment'
USER = 'user_read'
LABELS = {TTS: '语音', ALIGNMENT: '逐词对齐', USER: '账户查询'}


def permission_value(status, capability):
    if not isinstance(status, dict):
        return None
    value = status.get('permission_' + capability)
    if type(value) is bool:
        return value
    if capability == ALIGNMENT:
        if status.get('forced_alignment_status') == 'permission_denied':
            return False
        if status.get('forced_alignment_status') == 'available':
            return True
    if capability == TTS and type(status.get('last_success_unix')) in (int, float) and status['last_success_unix'] > 0:
        return True
    return None


def permission_updates(capability, allowed):
    if capability not in LABELS or type(allowed) is not bool:
        raise ValueError('无效的 ELE 权限记录。')
    return {'permission_' + capability: allowed,
            'permission_' + capability + '_at': int(time.time())}


def describe_permissions(status):
    labels = {True: '已确认', False: '无权限', None: '待确认'}
    return '｜'.join(LABELS[item] + '：' + labels[permission_value(status, item)]
                    for item in (TTS, ALIGNMENT, USER))


def missing_permission(details):
    """Some ELE scope denials are HTTP 401, not invalid credentials."""
    codes = ' '.join(str(details.get(name, '')).lower() for name in ('code', 'status', 'type'))
    message = str(details.get('message', '')).lower()
    return any(marker in codes for marker in (
        'missing_permissions', 'missing_permission', 'insufficient_permissions',
        'insufficient_scope', 'permission_denied',
    )) or ('missing the permission' in message or 'missing required permission' in message)


def inspect_key(api_key, timeout=8):
    """Exactly one read-only lookup. Never generate/upload samples to probe."""
    from model.OfflineMode import require_online, OfflineModeError
    now = int(time.time())
    result = {'permission_probe_checked_at': now}
    try:
        require_online('ElevenLabs Key 只读权限检测')
    except OfflineModeError:
        return dict(result, permission_probe_status='offline')
    request = urllib.request.Request('https://api.elevenlabs.io/v1/user',
        headers={'Accept': 'application/json', 'xi-api-key': api_key}, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read(256 * 1024).decode('utf-8'))
        if not isinstance(data, dict) or not isinstance(data.get('user_id'), str) or not data['user_id']:
            return dict(result, permission_probe_status='invalid_response')
        result.update(permission_updates(USER, True))
        result.update(permission_authentication=True, permission_authentication_at=now,
                      permission_probe_status='partial')
        subscription = data.get('subscription')
        if isinstance(subscription, dict):
            for source, name in (('character_count', 'used'), ('character_limit', 'limit'),
                                 ('next_character_count_reset_unix', 'reset_unix')):
                value = subscription.get(source)
                if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                    result['permission_quota_' + name] = value
        return result
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read(16 * 1024).decode('utf-8'))
            detail = body.get('detail', body) if isinstance(body, dict) else {}
            detail = detail if isinstance(detail, dict) else {}
        except (ValueError, OSError, TypeError):
            detail = {}
        if missing_permission(detail):
            result.update(permission_updates(USER, False))
            return dict(result, permission_authentication=True, permission_authentication_at=now,
                        permission_probe_status='partial')
        code = ' '.join(str(detail.get(name, '')).lower() for name in ('status', 'code', 'type'))
        if any(marker in code for marker in ('invalid_api_key', 'missing_api_key', 'invalid_token')):
            return dict(result, permission_authentication=False, permission_authentication_at=now,
                        permission_probe_status='invalid_key')
        return dict(result, permission_probe_status='rate_limit' if error.code == 429 else 'unconfirmed')
    except (OSError, ValueError, TimeoutError):
        return dict(result, permission_probe_status='network_error')


def probe_description(status):
    names = {'partial': '只读检测已完成；生成／对齐权限由实际使用结果确认',
             'offline': '离线模式，未发送检测请求', 'invalid_key': 'Key 已明确失效',
             'network_error': '网络检测未完成，保留已有权限记录',
             'rate_limit': '检测被限流，保留已有权限记录',
             'invalid_response': '接口响应无法确认，未更改生成权限',
             'unconfirmed': '账户查询未确认，不代表无法生成语音'}
    return names.get(status.get('permission_probe_status'), '添加时自动检测；不会上传音频或付费试生成')
