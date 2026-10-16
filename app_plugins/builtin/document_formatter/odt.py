"""Patch only ODF styles; preserve all other ZIP entries and document content."""
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import zipfile
from xml.dom import Node

from defusedxml.minidom import parseString
from model.AppDataStorage import data_file, data_io
from .rules import normalize_rules, paragraph_roles, MAX_PARAGRAPHS

NS = {"office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
      "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
      "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
      "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
      "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
      "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
      "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"}
MAX_INPUT = 64 * 1024 * 1024
MAX_XML = 8 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024


def xml(raw):
    if len(raw) > MAX_XML: raise ValueError("文档XML过大，已停止读取。")
    return parseString(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)


def elements(parent, prefix, name):
    return list(parent.getElementsByTagNameNS(NS[prefix], name))


def child(parent, prefix, name):
    return next((node for node in parent.childNodes if node.nodeType == Node.ELEMENT_NODE
                 and node.namespaceURI == NS[prefix] and node.localName == name), None)


def add(parent, prefix, name):
    node = parent.ownerDocument.createElementNS(NS[prefix], f"{prefix}:{name}")
    parent.appendChild(node)
    return node


def attr(node, prefix, name, value):
    node.setAttributeNS(NS[prefix], f"{prefix}:{name}", str(value))


def text(node):
    if node.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE): return node.data
    if node.nodeType != Node.ELEMENT_NODE: return ""
    if node.namespaceURI in {NS['office'], NS['draw']} or (node.namespaceURI == NS['text'] and node.localName == 'note'): return ""
    if node.namespaceURI == NS['text']:
        if node.localName == 'line-break': return '\n'
        if node.localName == 'tab': return '\t'
        if node.localName == 's':
            count = int(node.getAttributeNS(NS['text'], 'c') or 1)
            if not 1 <= count <= 10000: raise ValueError('连续空格数量异常。')
            return ' ' * count
    return ''.join(text(item) for item in node.childNodes)


def paragraph_nodes(document):
    body = next(iter(elements(document, 'office', 'text')), None)
    if body is None: raise ValueError('这不是ODT文字文档，不能用ODS表格代替。')
    return [node for node in body.getElementsByTagName('*')
            if node.namespaceURI == NS['text'] and node.localName in {'p', 'h'}]


def records(document, styles):
    known = {node.getAttributeNS(NS['style'], 'name'): node for dom in (document, styles)
             for node in elements(dom, 'style', 'style')}
    result = []
    for index, node in enumerate(paragraph_nodes(document)):
        structured, ancestor = False, node.parentNode
        while ancestor and ancestor.nodeType == Node.ELEMENT_NODE:
            if ancestor.namespaceURI in {NS['table'], NS['draw']} or (
                    ancestor.namespaceURI == NS['text'] and ancestor.localName in {'list', 'note', 'index-body'}) or (
                    ancestor.namespaceURI == NS['office'] and ancestor.localName == 'annotation'):
                structured = True
            ancestor = ancestor.parentNode
        name = node.getAttributeNS(NS['text'], 'style-name')
        heading, current, visited = node.localName == 'h', name, set()
        while current and current not in visited:
            visited.add(current)
            if current.casefold().replace('_20_', ' ').startswith(('heading', 'title', '标题')): heading = True
            value = known.get(current)
            current = value.getAttributeNS(NS['style'], 'parent-style-name') if value else ''
        result.append({'id': f'p{index}', 'text': text(node), 'heading': heading, 'structured': structured})
        if len(result) > MAX_PARAGRAPHS: raise ValueError('文档段落过多，请拆分后处理。')
    return result


def read_odt(source):
    path = Path(source)
    if path.suffix.lower() != '.odt': raise ValueError('请选择ODT文字文档；ODS是表格，不支持此格式规则。')
    with data_file(path).open('rb') as handle: raw = handle.read(MAX_INPUT+1)
    if len(raw) > MAX_INPUT: raise ValueError('ODT文件超过64MB，请拆分后处理。')
    archive = zipfile.ZipFile(io.BytesIO(raw))
    entries = archive.infolist()
    if len(entries) > 4096 or sum(item.file_size for item in entries) > MAX_EXPANDED:
        archive.close(); raise ValueError('压缩文档体积异常，已停止解析。')
    names = [item.filename for item in entries]
    if len(names) != len(set(names)) or any(PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name for name in names):
        archive.close(); raise ValueError('文档包包含重复或不安全的路径。')
    try:
        if archive.read('mimetype') != b'application/vnd.oasis.opendocument.text': raise ValueError('文件内容不是ODT文字文档。')
        if any(item.flag_bits & 1 for item in entries): raise ValueError('不支持密码加密的ODT，请先自行解锁副本。')
        if 'META-INF/manifest.xml' in names and elements(xml(archive.read('META-INF/manifest.xml')), 'manifest', 'encryption-data'):
            raise ValueError('不支持密码加密的ODT，请先自行解锁副本。')
        for name in ('content.xml', 'styles.xml'):
            if name not in names or archive.getinfo(name).file_size > MAX_XML: raise ValueError('ODT缺少必要XML或XML过大。')
        document, styles = xml(archive.read('content.xml')), xml(archive.read('styles.xml'))
        return raw, archive, document, styles
    except Exception:
        archive.close(); raise


def inspect_odt(source):
    raw, archive, document, styles = read_odt(source)
    try:
        return {'kind': 'odt', 'source': str(Path(source).resolve()), 'title': Path(source).name,
                'fingerprint': hashlib.sha256(raw).hexdigest(), 'paragraphs': records(document, styles)}
    finally: archive.close()


def format_odt(snapshot, rules, overrides=None, check=lambda: None):
    rules = normalize_rules(rules)
    raw, archive, document, styles = read_odt(snapshot['source'])
    try:
        if hashlib.sha256(raw).hexdigest() != snapshot['fingerprint']: raise ValueError('预览后ODT已被修改，请重新读取；原文件未改变。')
        before = records(document, styles)
        roles = paragraph_roles(before, rules, overrides)
        if not any(role != 'keep' for role in roles.values()): raise ValueError('没有需要格式化的段落。')
        for dom in (document, styles):
            for prefix in ('office', 'style', 'fo', 'text'):
                dom.documentElement.setAttribute(f'xmlns:{prefix}', NS[prefix])
        automatic = child(document.documentElement, 'office', 'automatic-styles')
        if automatic is None: automatic = add(document.documentElement, 'office', 'automatic-styles')
        existing = {node.getAttributeNS(NS['style'], 'name') for dom in (document, styles)
                    for node in elements(dom, 'style', 'style') + elements(dom, 'style', 'page-layout')}
        sequence = 0
        def name():
            nonlocal sequence
            while True:
                sequence += 1; value = f'ATFormat{sequence}'
                if value not in existing: existing.add(value); return value
        def style(parent_name, family, role):
            value = name(); node = add(automatic, 'style', 'style')
            attr(node, 'style', 'name', value); attr(node, 'style', 'family', family)
            if parent_name: attr(node, 'style', 'parent-style-name', parent_name)
            props = add(node, 'style', 'text-properties')
            size = rules['title_pt'] if role == 'title' else rules['body_pt']
            attr(props, 'fo', 'font-size', f'{size:g}pt')
            for suffix in ('asian', 'complex'): attr(props, 'style', f'font-size-{suffix}', f'{size:g}pt')
            if role == 'title':
                attr(props, 'fo', 'font-weight', 'bold')
                for suffix in ('asian', 'complex'): attr(props, 'style', f'font-weight-{suffix}', 'bold')
            if family == 'paragraph':
                props = add(node, 'style', 'paragraph-properties')
                for key, setting in [('margin-left', 'indent_left_cm'), ('margin-right', 'indent_right_cm'), ('text-indent', 'first_line_cm')]:
                    attr(props, 'fo', key, f'{rules[setting]:g}cm')
                attr(props, 'fo', 'line-height', f'{rules["line_spacing"]*100:g}%')
                if rules['reset_spacing']:
                    for key in ('margin-top', 'margin-bottom'): attr(props, 'fo', key, '0cm')
            return value, node
        layouts = elements(styles, 'style', 'page-layout') + elements(document, 'style', 'page-layout')
        master_name = None
        if not layouts:
            automatic_styles = child(styles.documentElement, 'office', 'automatic-styles')
            if automatic_styles is None: automatic_styles = add(styles.documentElement, 'office', 'automatic-styles')
            layout = add(automatic_styles, 'style', 'page-layout'); attr(layout, 'style', 'name', 'ATFormatPage')
            masters = child(styles.documentElement, 'office', 'master-styles')
            if masters is None: masters = add(styles.documentElement, 'office', 'master-styles')
            masters_existing = elements(masters, 'style', 'master-page')
            master = masters_existing[0] if masters_existing else add(masters, 'style', 'master-page')
            master_name = master.getAttributeNS(NS['style'], 'name') or 'ATFormatMaster'
            attr(master, 'style', 'name', master_name); attr(master, 'style', 'page-layout-name', 'ATFormatPage')
            layouts = [layout]
        for layout in layouts:
            properties = child(layout, 'style', 'page-layout-properties')
            if properties is None: properties = add(layout, 'style', 'page-layout-properties')
            for key, setting in [('page-width', 'page_width_cm'), ('page-height', 'page_height_cm'),
                                 ('margin-top', 'margin_top_cm'), ('margin-bottom', 'margin_bottom_cm'),
                                 ('margin-left', 'margin_left_cm'), ('margin-right', 'margin_right_cm')]:
                attr(properties, 'fo', key, f'{rules[setting]:g}cm')
            attr(properties, 'style', 'print-orientation', 'portrait' if rules['page_width_cm'] <= rules['page_height_cm'] else 'landscape')
        char_styles = {}
        for index, paragraph in enumerate(paragraph_nodes(document)):
            check(); role = roles[f'p{index}']
            if role == 'keep': continue
            value, created = style(paragraph.getAttributeNS(NS['text'], 'style-name'), 'paragraph', role)
            if master_name: attr(created, 'style', 'master-page-name', master_name); master_name = None
            attr(paragraph, 'text', 'style-name', value)
            for inline in paragraph.getElementsByTagName('*'):
                if inline.namespaceURI != NS['text'] or inline.localName not in {'span', 'a'}: continue
                ancestor, skip = inline.parentNode, False
                while ancestor is not paragraph and ancestor is not None:
                    if ancestor.namespaceURI in {NS['office'], NS['draw']} or (ancestor.namespaceURI == NS['text'] and ancestor.localName == 'note'): skip = True
                    ancestor = ancestor.parentNode
                if skip: continue
                original = inline.getAttributeNS(NS['text'], 'style-name')
                if not original: continue
                key = original, role
                if key not in char_styles: char_styles[key] = style(original, 'text', role)[0]
                attr(inline, 'text', 'style-name', char_styles[key])
        if [item['text'] for item in records(document, styles)] != [item['text'] for item in before]:
            raise ValueError('格式校验失败：文档文字发生变化；没有输出。')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w') as target:
            for entry in [archive.getinfo('mimetype')] + [entry for entry in archive.infolist() if entry.filename != 'mimetype']:
                check()
                data = document.toxml(encoding='utf-8') if entry.filename == 'content.xml' else styles.toxml(encoding='utf-8') if entry.filename == 'styles.xml' else archive.read(entry.filename)
                target.writestr(entry, data, compress_type=zipfile.ZIP_STORED if entry.filename == 'mimetype' else entry.compress_type)
        check(); source = Path(snapshot['source']); raw_output = output.getvalue()
        descriptor, pending = data_io.mkstemp(dir=source.parent, prefix='.document-format-', suffix='.tmp')
        os.close(descriptor)
        try:
            with data_file(pending).open('wb') as handle: handle.write(raw_output); handle.flush()
            check()
            with data_file(snapshot['source']).open('rb') as handle: current = handle.read(MAX_INPUT+1)
            if hashlib.sha256(current).hexdigest() != snapshot['fingerprint']: raise ValueError('格式化期间源文件已改变，未发布成品；请重新读取。')
            for index in range(1, 1000):
                destination = source.with_name(f'{source.stem}-已格式化' + (f' ({index})' if index > 1 else '') + '.odt')
                try:
                    # Windows rename and POSIX hard-link both refuse an existing
                    # destination. Never replace a user's file or symlink target.
                    if os.name == 'nt': data_io.rename(pending, destination)
                    else: data_io.link(pending, destination)
                    return {'kind': 'odt', 'paths': [str(destination)], 'output': str(destination), 'formatted': sum(role != 'keep' for role in roles.values())}
                except FileExistsError: continue
            raise ValueError('同名格式化文件过多，请先选择另一份源文件。')
        finally:
            if Path(pending).exists():
                try: data_file(pending).remove()
                except OSError: pass  # Do not follow a lost/reused encrypted drive.
    finally: archive.close()
