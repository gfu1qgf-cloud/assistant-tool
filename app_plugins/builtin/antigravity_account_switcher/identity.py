"""Read known protobuf fields, never search arbitrary binary data for emails.

Field numbers verified against the installed client's public descriptors:
exa.codeium_common_pb.UserStatus: name=3, email=7, g1_tier=35,
profile_picture_url=38. OAuthTokenInfo: access_token=1, refresh_token=3.
"""
import base64
import re


class IdentityFormatError(ValueError):
    pass


def _decode(value):
    if isinstance(value, str):
        value = value.encode('ascii')
    if not isinstance(value, bytes) or len(value) > 2_000_000:
        raise IdentityFormatError('不支持的账号记录格式')
    try:
        return base64.b64decode(value + b'=' * (-len(value) % 4), validate=True)
    except (ValueError, UnicodeError) as error:
        raise IdentityFormatError('账号记录不是合法Base64') from error


def _varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]; offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise IdentityFormatError('账号记录的整数编码不完整')


def _fields(data):
    fields, offset, count = {}, 0, 0
    while offset < len(data):
        count += 1
        if count > 2048:
            raise IdentityFormatError('账号记录字段过多')
        key, offset = _varint(data, offset)
        tag, kind = key >> 3, key & 7
        if tag < 1 or tag > 536870911:
            raise IdentityFormatError('账号字段编号无效')
        if kind == 0:
            value, offset = _varint(data, offset)
        elif kind in (1, 5):
            size = 8 if kind == 1 else 4
            value = data[offset:offset+size]; offset += size
            if len(value) != size:
                raise IdentityFormatError('账号记录已截断')
        elif kind == 2:
            size, offset = _varint(data, offset)
            value = data[offset:offset+size]; offset += size
            if len(value) != size:
                raise IdentityFormatError('账号记录已截断')
        else:
            raise IdentityFormatError('账号记录使用了不支持的编码')
        fields.setdefault(tag, []).append((kind, value))
    return fields


def _one(fields, number, *, optional=False):
    values = fields.get(number, [])
    if not values and optional:
        return None
    if len(values) != 1 or values[0][0] != 2:
        raise IdentityFormatError('账号记录关键字段缺失或重复')
    return values[0][1]


def topic_payload(raw, sentinel):
    envelope = _fields(_decode(raw))
    matches = []
    for kind, entry in envelope.get(1, []):
        if kind != 2:
            raise IdentityFormatError('账号主题封装无效')
        item = _fields(entry)
        if _one(item, 1) == sentinel.encode('ascii'):
            wrapped = _fields(_one(item, 2))
            matches.append(_decode(_one(wrapped, 1)))
    if len(matches) != 1:
        raise IdentityFormatError('没有唯一的账号主题记录')
    return matches[0]


def has_oauth_token(raw):
    try:
        values = _fields(topic_payload(raw, 'oauthTokenInfoSentinelKey'))
        access = _one(values, 1, optional=True)
        refresh = _one(values, 3, optional=True)
        return bool(access and len(access) > 5 and refresh and len(refresh) > 5)
    except (IdentityFormatError, UnicodeError, TypeError):
        return False


def parse_user_status(raw):
    values = _fields(topic_payload(raw, 'userStatusSentinelKey'))
    try:
        email = _one(values, 7).decode('utf8')
        name = (_one(values, 3, optional=True) or b'').decode('utf8')
        tier = (_one(values, 35, optional=True) or b'').decode('utf8')
        avatar = (_one(values, 38, optional=True) or b'').decode('utf8')
    except UnicodeError as error:
        raise IdentityFormatError('账号文字编码无效') from error
    if len(email) > 320 or not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}', email):
        raise IdentityFormatError('账号邮箱字段无法确认')
    return {'email': email, 'name': name[:200] or None, 'tier': tier[:200] or None,
            'plan': None, 'avatar': avatar if avatar.startswith('https://') else None}
