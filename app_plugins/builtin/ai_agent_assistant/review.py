"""Read-only AI review projection; never a generation or download approval."""
import re

STATES = ('AI预审不通过', 'AI预审通过', '部分通过', '无法审核', '待AI审核')
RECORD = re.compile(r'(?:^|[\r\n\v])\s*(?:审核状态\s*[:：]\s*)?(' + '|'.join(STATES) + r')(?=$|[\s｜|:：（(])')


def review_state(text):
    text = str(text or '').strip()
    if not text: return {'status':'', 'problem':False, 'text':''}
    # Records are append-only. The last explicit status-led record supersedes
    # older records, while words quoted in its explanation are not new states.
    matches = list(RECORD.finditer(text))
    status = matches[-1].group(1) if matches else '审核记录待核对'
    return {'status':status, 'problem':status not in {'AI预审通过','待AI审核'}, 'text':text}


def cache_review(store, job_id, text, *, available, records):
    value = {'state':'ai_review', 'available':bool(available), 'text':str(text or '').strip() if available else ''}
    if records.get('@ai_review') != value and (available or '@ai_review' in records):
        store.save_delivery(job_id, '@ai_review', value)
        records['@ai_review'] = value


def review_tooltip(base, text, *, limit=3000):
    if not text: return base[:limit]
    heading='\n\nAI审核结果（独立预审，不代表用户终审）：\n'
    # The host caps badges at 3000 characters. Prefer the latest audit record
    # and explicitly disclose truncation instead of breaking every badge.
    base=base[:700]; budget=max(0,limit-len(base)-len(heading))
    if len(text)>budget:
        notice='【记录较长，以下为末尾；完整记录见原表AI审核结果列】\n'
        text=notice+text[-max(0,budget-len(notice)):]
    return base+heading+text
