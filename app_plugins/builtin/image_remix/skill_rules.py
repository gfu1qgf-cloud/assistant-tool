"""Read bundled prompt rules as data; freeze them per job, never execute them."""
import hashlib
import json
from pathlib import Path
import re
from model.AppDataStorage import data_file

SKILL_ID = 'lzx4-biblical-image-prompts'
SKILL_TITLE = 'LZX4 圣经图片提示词'
VERSION = '2026-11-13-text-first-v1'
FILES = ('SKILL.md', 'references/jesus-appearance.md', 'references/biblical-continuity.md',
         'references/scene-playbook.md', 'references/reference-fidelity.md', 'references/output-examples.md')


def _digest(snapshot):
    value={key:snapshot[key] for key in ('id','version','documents')}
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def load_snapshot(identifier):
    if identifier == 'none': return None
    if identifier != SKILL_ID: raise ValueError('未知图片提示词SKILL，请重新选择。')
    root=Path(__file__).with_name('skills')/SKILL_ID
    documents={}
    for relative in FILES:
        path=root/relative
        if not path.is_file() or path.stat().st_size>40000: raise ValueError('SKILL资源缺失或过大：'+relative)
        documents[relative]=data_file(path).read_text(encoding='utf-8-sig')
    result={'id':identifier,'version':VERSION,'documents':documents}
    result['sha256']=_digest(result)
    return result


def validate_snapshot(value):
    if (not isinstance(value,dict) or value.get('id')!=SKILL_ID or not isinstance(value.get('version'),str)
            or not isinstance(value.get('documents'),dict) or set(value['documents'])!=set(FILES)
            or any(not isinstance(text,str) or len(text)>25000 for text in value['documents'].values())
            or sum(len(text) for text in value['documents'].values())>80000
            or value.get('sha256')!=_digest(value)):
        raise ValueError('任务SKILL快照损坏，未改用其他规则或提交生图。')
    return value


def selected_documents(snapshot, prompt, has_references):
    validate_snapshot(snapshot)
    chosen=['SKILL.md','references/reference-fidelity.md']
    if has_references or re.search(r'耶稣|基督|圣子|jesus|christ',prompt,re.I):
        chosen.append('references/jesus-appearance.md')
    if has_references or re.search(r'受难|十字|钉|伤痕|安葬|墓|复活|升天|crucifix|resurrec|burial',prompt,re.I):
        chosen.append('references/biblical-continuity.md')
    if has_references or re.search(r'十字|墓|祷告|天堂|天国|绿幕|蓝幕|救援|救助|圣婴|圣诞|教堂|讲道|牧羊|海浪|nativity|prayer|heaven|rescue',prompt,re.I):
        chosen.append('references/scene-playbook.md')
    return chosen


def instructions(snapshot, prompt, has_references):
    names=selected_documents(snapshot,prompt,has_references)
    return '\n\n'.join('--- '+name+' ---\n'+snapshot['documents'][name] for name in names)
