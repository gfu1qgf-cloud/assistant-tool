"""Native Qt Markdown presentation; rendering never fetches external resources."""
import html
import re
import hashlib
from model.AIChatSources import source

from qt_compat import QtGui


class _MarkdownDocument(QtGui.QTextDocument):
    def loadResource(self, resource_type, name):
        # QTextDocument's default loader can read local files. Model output is
        # untrusted, so neither local files nor remote images are loaded here.
        return None


def _table_cells(line):
    """Split GFM cells, respecting escaped pipes and optional outer pipes."""
    indent = re.match(r"[ \t]*", line)[0]
    if len(indent.expandtabs(4)) >= 4:
        return None
    line = line.strip()
    cells, current, escaped, pipes = [], [], False, 0
    for char in line:
        if char == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
            pipes += 1
        else:
            current.append(char)
        escaped = char == "\\" and not escaped
    if not pipes:
        return None
    cells.append("".join(current).strip())
    if not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return cells


def _qt_markdown(text):
    """Bridge Qt's stricter table boundary without changing stored/model text.

    GFM allows tables immediately after a paragraph; Qt's parser needs a blank
    line. Only add that boundary for a complete, matching header/delimiter pair,
    never inside fenced or indented code. Incomplete streaming text stays literal
    until the delimiter arrives, then the next render recognizes the table.
    """
    # Qt's GitHub dialect silently consumes an opening --- ... --- section as
    # YAML front matter, even when it is ordinary chat prose. Use the equivalent
    # thematic break syntax for this display copy; never alter stored text.
    text = re.sub(r'\A(\ufeff?(?:[ \t]*\r?\n)* {0,3})---[ \t]*(?=\r?\n|$)', r'\1***', text, count=1)
    lines = text.split("\n")
    output = []
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if (marker and marker[1][0] == fence[0] and len(marker[1]) >= fence[1]
                    and not marker[2].strip()):
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]))
        elif index + 1 < len(lines) and output and output[-1].strip():
            header = _table_cells(line)
            delimiter = _table_cells(lines[index + 1])
            if (header and delimiter and len(header) == len(delimiter)
                    and all(re.fullmatch(r":?-+:?", cell) for cell in delimiter)):
                output.append("")
        output.append(line)
    return "\n".join(output)


def message_html(text, markdown=True):
    if not markdown:
        return "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"
    document = _MarkdownDocument()
    features = QtGui.QTextDocument.MarkdownFeature
    document.setMarkdown(_qt_markdown(text),
                         features.MarkdownDialectGitHub | features.MarkdownNoHTML)
    body = re.search(r"<body[^>]*>([\s\S]*)</body>", document.toHtml())
    if not body:
        return "<p>" + html.escape(text) + "</p>"
    # Preserve code indentation when importing the fragment back into the chat
    # document. Qt exports this whitespace rule in <head>, outside the body.
    fragment = re.sub(r'(<span style="[^"]*)font-family:\'monospace\';',
                      r"\1font-family:'Consolas';", body.group(1))
    # Qt exports a structural newline before nested lists. With pre-wrap, its
    # HTML importer treats that newline as a second, empty parent list item.
    # Remove only this generated boundary (code text has escaped HTML tags).
    fragment = re.sub(r"\r?\n(?=[ \t]*<(?:ul|ol)\b)", "", fragment)
    return "<style>p, li, pre { white-space: pre-wrap; }</style>" + fragment


def is_web_link(url):
    return (url.isValid() and url.scheme().lower() in {"http", "https"}
            and bool(url.host()) and not url.userInfo())


def cited_message_html(text, metadata, markdown=True):
    """Insert clickable inline citations with inert placeholders, not raw HTML."""
    safe=[]
    for value in (metadata.get('sources') or [])[:1000]:
        item=source(value.get('url'),value.get('title'),value.get('start'),value.get('end'))
        if item: safe.append(item)
    if not safe: return message_html(text,markdown)
    prefix='LZXREF'+hashlib.sha256(text.encode()).hexdigest()[:16]
    while prefix in text: prefix+='X'
    replacements, spans, links = {}, [], {}
    for item in safe:
        number=links.setdefault(item['url'],(len(links)+1,item['title']))[0]
        start,end=item.get('start'),item.get('end')
        if type(start) is int and type(end) is int and 0<=start<=end<=len(text):
            # Gemini support segments contain real words: append to the segment;
            # replace only OpenAI-style citation markers, never factual prose.
            original=text[start:end]
            marker=prefix+str(len(replacements))+'END'
            anchor='<a href="'+html.escape(item['url'],quote=True)+'">['+str(number)+']</a>'
            replacements[marker]=anchor
            if '' in original or original.startswith('【'):
                spans.append((start,end,' '+marker+' '))
            else: spans.append((end,end,' '+marker+' '))
    rendered=text; boundary=len(text)+1
    for start,end,value in sorted(spans,key=lambda item:(item[0],item[1]),reverse=True):
        if end>boundary: continue
        rendered=rendered[:start]+value+rendered[end:]; boundary=start
    fragment=message_html(rendered,markdown)
    for marker,anchor in replacements.items(): fragment=fragment.replace(marker,anchor)
    fragment+='<p><b>联网来源</b><br>'
    fragment+='<br>'.join('<a href="'+html.escape(url,quote=True)+'">['+str(number)+'] '+html.escape(title)+'</a>'
                        for url,(number,title) in links.items())
    return fragment+'</p>'
