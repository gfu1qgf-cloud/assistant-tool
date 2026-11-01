"""Explicit, local keyword emphasis; no inference or subtitle timing edits."""
from functools import lru_cache
import re
import unicodedata

DEFAULTS = {'keyword_emphasis_enabled': False, 'keyword_terms': '',
            'keyword_color': '#FFD84D', 'keyword_scale': 1.18, 'keyword_match': 'word'}
MATCHES = (('整词匹配', 'word'), ('包含匹配（中文无空格）', 'contains'))


def normalized(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold().strip()
    while text and unicodedata.category(text[0])[0] in 'PS': text = text[1:]
    while text and unicodedata.category(text[-1])[0] in 'PS': text = text[:-1]
    return text.strip()


@lru_cache(maxsize=128)
def terms(text):
    return tuple(dict.fromkeys(value for item in re.split(r'[,，;；\n]+', text)
                               if (value := normalized(item))))


def validate(style):
    if not isinstance(style['keyword_terms'],str) or len(style['keyword_terms'])>500:
        raise ValueError('关键词列表须为文字。')
    if not isinstance(style['keyword_match'],str) or style['keyword_match'] not in {key for _title, key in MATCHES}:
        raise ValueError('关键词匹配方式无效。')
    if not 1 <= style['keyword_scale'] <= 2:
        raise ValueError('关键词字号倍数应为1–2倍。')
    if len(terms(style['keyword_terms'])) > 80:
        raise ValueError('关键词最多80项，请用逗号、分号或换行分隔。')


def matches(text, style):
    if not style.get('keyword_emphasis_enabled'):
        return False
    value = normalized(text)
    choices = terms(style.get('keyword_terms', ''))
    return bool(value) and (value in choices if style.get('keyword_match', 'word') == 'word'
                           else any(term in value for term in choices))


def preserve_content(result, source):
    previous=source.get('caption_effects',{})
    if isinstance(previous,dict) and previous.get('keyword_terms'):
        validate({**DEFAULTS,**previous})
        result['caption_effects']['keyword_terms']=previous['keyword_terms']
        result['caption_effects']['keyword_match']=previous.get('keyword_match','word')
    return result
