"""Native Qt Markdown presentation; rendering never fetches external resources."""
import html
import re

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
