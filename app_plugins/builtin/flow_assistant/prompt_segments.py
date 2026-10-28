"""Conservative local prompt segmentation: preserve prose, never infer dialogue."""
from dataclasses import dataclass
import re
import unicodedata


@dataclass(frozen=True)
class PromptSegment:
    title: str
    prompt: str
    detected_duration: int | None = None
    warning: str = ''


def plain_label(line):
    return unicodedata.normalize('NFKC', line).strip().replace('\\*','*').strip('#* _\\\t')


HEADER = re.compile(r'^(?:(?:第\s*[0-9一二三四五六七八九十百零〇两]+\s*(?:段|节|幕|镜头))|'
                    r'(?:(?:段落|片段|镜头|分镜|场景|segment|scene|part)\s*[0-9一二三四五六七八九十百零〇两]+)|'
                    r'(?:\d+\s*[.、)]\s*\d+\s*(?:秒|seconds?\b|secs?\b|s\b)))(?:\s|[|｜:：()（）—–-]|$)',re.I)
SECONDS = re.compile(r'(?<!\d)(\d{1,3})\s*(?:秒|seconds?\b|secs?\b|s\b)',re.I)
DESCRIPTION = r'(?:视频描述词?|画面描述词?|画面提示词|视频提示词|描述词|video\s*prompt|visual\s*prompt)'
SPEECH = r'(?:台词|对白(?:内容|台词)?|口播(?:文案)?|旁白|朗读文本|对话|dialogue|voiceover|speech)'
FIELD = re.compile(r'^\s*(?:'+DESCRIPTION+'|'+SPEECH+r'|备注|要求|时长|音效|镜头)\s*[:：]',re.I)
SPEECH_FIELD = re.compile(r'^\s*'+SPEECH+r'\s*[:：]\s*(.*)$',re.I)
DESCRIPTION_FIELD = re.compile(r'^\s*'+DESCRIPTION+r'\s*[:：]',re.I)
RULE = re.compile(r'^\s*(?:-{3,}|\*{3,}|_{3,})\s*$')


def dialogue_text(prompt):
    lines=[];reading=False
    for raw in str(prompt).splitlines():
        label=plain_label(raw)
        found=SPEECH_FIELD.match(label)
        if found:
            reading=True;lines.append(found[1].strip('* _\\'));continue
        if reading:
            if FIELD.match(label) or HEADER.match(label) or RULE.match(raw):break
            lines.append(raw.rstrip().removesuffix('\\').strip())
    return '\n'.join(lines).strip()


def speech_metrics(prompt,duration):
    speech=dialogue_text(prompt)
    if not speech:return '', '未识别到独立台词；可在提示词中使用“台词：”或“对白：”，原文仍完整保留。'
    cjk=len(re.findall(r'[\u3400-\u9fff]',speech))
    words=re.findall(r"[^\W\d_]+(?:[’'\-][^\W\d_]+)*",re.sub(r'[\u3400-\u9fff]',' ',speech),re.UNICODE)
    parts=([f'{len(words)} 词'] if words else [])+([f'{cjk} 个汉字'] if cjk else [])
    text=' · '.join(parts) or f'{len(speech)} 个字符'
    if duration:
        rates=([f'{len(words)/duration:.1f} 词/秒'] if words else [])+([f'{cjk/duration:.1f} 字/秒'] if cjk else [])
        text+=f' · {duration}秒内完整说完需 '+ '、'.join(rates)
    else:text+=' · 模型默认时长，尚不能计算所需语速'
    return speech,text+'（仅计数，需人工校对语气、停顿和实际朗读）'


def parse_segments(text):
    text=str(text or '').replace('\r\n','\n').replace('\r','\n').strip()
    if not text:raise ValueError('请先粘贴提示词。')
    if len(text)>1000000:raise ValueError('单次粘贴最多100万字符，请分批导入。')
    lines=text.splitlines()
    if len(lines)>=2 and re.fullmatch(r'\s*```[^`]*',lines[0]) and lines[-1].strip()=='```':lines=lines[1:-1]
    headers=[i for i,line in enumerate(lines) if HEADER.match(plain_label(line))]
    result=[]
    def add(body,title,seconds=None,warning=''):
        body=list(body)
        while body and (not body[-1].strip() or RULE.match(body[-1])):body.pop()
        while body and (not body[0].strip() or RULE.match(body[0])):body.pop(0)
        if not body:
            result.append(PromptSegment(title,'',seconds,'这一段只有标题，没有正文，请补充或移除。'));return
        if not dialogue_text('\n'.join(body)):warning=(warning+'；' if warning else '')+'未找到独立台词标签，请核对。'
        result.append(PromptSegment(title,'\n'.join(body).strip(),seconds,warning))
    if headers:
        if any(line.strip() and not RULE.match(line) for line in lines[:headers[0]]):
            add(lines[:headers[0]],'标题前的文字',warning='未自动归入其他段落，请确认是否需要此条。')
        for n,start in enumerate(headers):
            heading=plain_label(lines[start]);match=SECONDS.search(heading)
            body=lines[start+1:headers[n+1] if n+1<len(headers) else len(lines)]
            # Do not lose descriptions written on the same line as a segment title.
            inline=re.search(DESCRIPTION+r'\s*[:：]|'+SPEECH+r'\s*[:：]',lines[start],re.I)
            if inline:body=[lines[start][inline.start():]]+body
            else:
                inline=re.search(r'(?:\d+\s*(?:秒|seconds?\b|secs?\b|s\b)\s*[)）]?|第\s*[0-9一二三四五六七八九十]+\s*段)\s*[:：]\s*(.+)',lines[start],re.I)
                if inline:body=[inline[1]]+body
            add(body,heading,int(match[1]) if match else None)
    else:
        starts=[i for i,line in enumerate(lines) if DESCRIPTION_FIELD.match(plain_label(line))]
        if len(starts)>1:
            if any(line.strip() and not RULE.match(line) for line in lines[:starts[0]]):
                add(lines[:starts[0]],'开头文字',warning='未自动归入其他段落，请核对。')
            for n,start in enumerate(starts):add(lines[start:starts[n+1] if n+1<len(starts) else len(lines)],f'第{n+1}段')
        else:
            chunks=[[]]
            for line in lines:
                if RULE.match(line):
                    if chunks[-1]:chunks.append([])
                else:chunks[-1].append(line)
            chunks=[chunk for chunk in chunks if any(line.strip() for line in chunk)]
            if len(chunks)>1 and all(any(FIELD.match(plain_label(line)) for line in chunk) for chunk in chunks):
                for n,chunk in enumerate(chunks,1):add(chunk,f'第{n}段')
            else:
                # Blank lines, quoted "第0秒" and ordinary instructions are not split points.
                add(lines,'第1段',warning='未发现明确分段标题，按完整一条保留。')
    if len(result)>300:raise ValueError('一次最多识别300段，请分批导入。')
    return result
