"""Lossless preparation of legacy drafts; no I/O or implicit approval."""
import copy
import re
from urllib.parse import urlsplit

DRIVE_URL = re.compile(r'https://(?:drive|docs)\.google\.com/[^\s<>"，；。！？（）【】]+')
LOCAL_PATH = re.compile(
    r'(?<![A-Za-z0-9])[A-Za-z]:[\\/]|file://|\\\\|\.\.?[\\/]|(?:^|[\s：:（(，,；;])(?:~[\\/]|/[A-Za-z])', re.I)


def _pure_link(value):
    try:
        parsed = urlsplit(value)
        return parsed.scheme == 'https' and parsed.netloc in {'drive.google.com', 'docs.google.com'} and not re.search(r'\s', value)
    except ValueError:
        return False


def prepare_payload(payload, *, confirm_chinese=False):
    result = copy.deepcopy(payload)
    reference = str(payload.get('reference') or '').strip()
    if reference and not LOCAL_PATH.search(reference):
        lines = [line.strip() for line in reference.splitlines() if line.strip()]
        if not all(_pure_link(line) for line in lines):
            matches = list(DRIVE_URL.finditer(reference))
            remainder = DRIVE_URL.sub('', reference)
            has_other_url = re.search(r'(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)', remainder)
            filename = re.fullmatch(r'[^\s\\/]+\.(?:png|jpe?g|webp|gif|mp4|mov|mkv|pdf)', reference, re.I)
            if not has_other_url and not filename:
                links = list(dict.fromkeys(match.group().rstrip(').,;，；') for match in matches))
                result['reference'] = '\n'.join(links)
                note = '参考素材说明（原参考栏内容，完整保留）：\n' + reference
                requirements = str(result.get('requirements') or '').strip()
                if note not in requirements:
                    result['requirements'] = (requirements + '\n\n' + note).strip()
    if confirm_chinese and str(result.get('chinese') or '').strip() and not result.get('chinese_confirmed'):
        result['chinese_confirmed'] = True
        result['chinese_source'] = '用户批量确认：现有中文台词'
    return result
