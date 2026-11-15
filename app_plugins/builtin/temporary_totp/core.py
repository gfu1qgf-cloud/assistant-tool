"""Independent RFC 6238 implementation. No files, network, logging or history.

RFC 6238: https://www.rfc-editor.org/rfc/rfc6238
URI convention: https://github.com/google/google-authenticator/wiki/Key-Uri-Format
No KeePassXC source or assets are used. Memory wiping is best effort: Python,
HMAC and Qt may have made copies; this is not a secure-memory vault.
"""
import base64
import binascii
from dataclasses import dataclass,field
import hmac
import math
import re
import struct
import time
from urllib.parse import urlsplit,parse_qsl,unquote


class TotpError(ValueError):pass


@dataclass
class TotpSettings:
    secret:bytearray=field(repr=False)
    algorithm:str='SHA1'
    digits:int=6
    period:int=30
    label:str=field(default='',repr=False)

    def wipe(self):
        for index in range(len(self.secret)):self.secret[index]=0
        self.secret=bytearray();self.label=''


class TotpSession:
    def __init__(self):self.settings=None
    def clear(self,*_args):
        if self.settings is not None:self.settings.wipe()
        self.settings=None


def parameters(algorithm='SHA1',digits=6,period=30):
    algorithm=str(algorithm).upper().replace('-','')
    if algorithm not in ('SHA1','SHA256','SHA512'):raise TotpError('仅支持SHA-1、SHA-256或SHA-512。')
    if type(digits) is not int or digits not in (6,7,8):raise TotpError('数字TOTP仅支持6、7或8位。')
    if type(period) is not int or not 1<=period<=3600:raise TotpError('周期应为1至3600秒。')
    return algorithm,digits,period


def decode_secret(text):
    if not isinstance(text,str) or not text or len(text)>4096:raise TotpError('请粘贴有效的Base32密钥或otpauth链接。')
    if not re.fullmatch(r'[A-Za-z2-7=\s]+',text):raise TotpError('密钥不是有效Base32；请检查是否混入其他文字或不支持的编码。')
    value=re.sub(r'\s+','',text).upper()
    if not re.fullmatch(r'[A-Z2-7]+={0,6}',value):raise TotpError('Base32密钥格式无效。')
    body=value.rstrip('=');padding='='*((-len(body))%8)
    if len(body)%8 not in (0,2,4,5,7) or ('=' in value and value!=body+padding):raise TotpError('Base32密钥长度或补位无效。')
    try:secret=base64.b32decode(body+padding,casefold=False)
    except (binascii.Error,ValueError):raise TotpError('Base32密钥无法解码。') from None
    if not secret:raise TotpError('密钥不能为空。')
    if base64.b32encode(secret).decode('ascii').rstrip('=')!=body:raise TotpError('Base32密钥的末尾补位无效。')
    return bytearray(secret)


def parse_input(text,*,algorithm='SHA1',digits=6,period=30):
    if not isinstance(text,str) or len(text)>4096:raise TotpError('输入过长或格式无效。')
    value=text.strip();label=''
    if value.casefold().startswith('otpauth:'):
        try:
            uri=urlsplit(value)
            if uri.scheme.lower()!='otpauth' or uri.netloc.lower()!='totp' or uri.fragment:
                raise TotpError('仅支持otpauth://totp/链接，不支持HOTP或其他类型。')
            entries=parse_qsl(uri.query,keep_blank_values=True,max_num_fields=32)
        except (ValueError,UnicodeError):raise TotpError('otpauth链接格式无效，仅支持数字TOTP。') from None
        options={}
        for key,item in entries:
            key=key.casefold()
            if key in options:raise TotpError('otpauth链接含重复参数，无法确定使用哪组设置。')
            options[key]=item
        if 'encoder' in options:raise TotpError('此链接使用特殊编码，目前只支持数字TOTP。')
        value=options.get('secret','')
        try:digits=int(options.get('digits','6'));period=int(options.get('period','30'))
        except ValueError:raise TotpError('otpauth链接的位数或周期不是整数。') from None
        algorithm=options.get('algorithm','SHA1')
        label=unquote(uri.path.lstrip('/'))[:160]
    algorithm,digits,period=parameters(algorithm,digits,period)
    return TotpSettings(decode_secret(value),algorithm,digits,period,label)


def code_at(settings,now=None):
    algorithm,digits,period=parameters(settings.algorithm,settings.digits,settings.period)
    if not isinstance(settings.secret,(bytes,bytearray)) or not settings.secret:raise TotpError('当前没有临时密钥，请先生成。')
    now=time.time() if now is None else now
    try:
        if isinstance(now,bool) or not math.isfinite(now) or now<0:raise ValueError()
        counter=int(now//period)
        if not 0<=counter<2**64:raise ValueError()
        digest=hmac.new(settings.secret,struct.pack('>Q',counter),algorithm.lower()).digest()
        offset=digest[-1]&15
        number=(int.from_bytes(digest[offset:offset+4],'big')&0x7fffffff)%10**digits
        remaining=period-(now%period)
    except (ValueError,OverflowError,TypeError,struct.error):raise TotpError('系统时间或所选算法不可用，请检查时间与高级设置。') from None
    return str(number).zfill(digits),remaining,counter
