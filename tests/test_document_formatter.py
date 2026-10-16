"""ODT byte-preservation, exact formatting and official Docs request contracts."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

from app_plugins.builtin.document_formatter import odt, google_docs
from app_plugins.builtin.document_formatter.rules import DEFAULT_RULES, normalize_rules, paragraph_roles
from app_plugins.builtin.document_formatter.background import run_document_job
from model.BackgroundTasks import BackgroundTaskResultError, BackgroundTaskCanceled


def fixture_odt(path, content=None, manifest=None, extra=None):
    namespace = ' '.join(f'xmlns:{key}="{value}"' for key, value in odt.NS.items())
    namespace += ' xmlns:xlink="http://www.w3.org/1999/xlink"'
    content = content or f'''<office:document-content {namespace} office:version="1.2">
      <office:automatic-styles/><office:body><office:text>
      <text:h text:style-name="Heading_20_1" text:outline-level="1">Život a nádej 🙏</text:h>
      <text:p text:style-name="Body">正文 中文 <text:span text:style-name="Huge">Ahoj, že áno.</text:span>
      <text:a xlink:href="https://example.org/path?a=1&amp;b=2" text:style-name="Link">链接</text:a><text:s text:c="2"/><text:tab/>koniec</text:p>
      <text:list><text:list-item><text:p text:style-name="Body">列表保留</text:p></text:list-item></text:list>
      <table:table table:name="Test"><table:table-row><table:table-cell office:value-type="string"><text:p>表格内容</text:p></table:table-cell></table:table-row></table:table>
      </office:text></office:body></office:document-content>'''
    styles = f'''<office:document-styles {namespace} office:version="1.2"><office:styles>
      <style:style style:name="Body" style:family="paragraph"><style:paragraph-properties fo:text-align="justify" fo:margin-top="0.2cm"/>
      <style:text-properties fo:font-family="Arial" fo:font-size="25pt"/></style:style>
      <style:style style:name="Heading_20_1" style:family="paragraph"><style:text-properties fo:font-size="18pt"/></style:style>
      <style:style style:name="Huge" style:family="text"><style:text-properties fo:font-size="99pt" fo:color="#ff0000" fo:font-style="italic"/></style:style>
      <style:style style:name="Link" style:family="text"><style:text-properties fo:font-size="22pt" style:text-underline-style="solid"/></style:style>
      </office:styles><office:automatic-styles><style:page-layout style:name="P1"><style:page-layout-properties fo:page-width="20cm" fo:page-height="28cm" fo:margin="3cm"/></style:page-layout></office:automatic-styles>
      <office:master-styles><style:master-page style:name="Standard" style:page-layout-name="P1"/></office:master-styles></office:document-styles>'''
    manifest = manifest or (f'<manifest:manifest {namespace}><manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
                            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
                            '<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/></manifest:manifest>')
    entries = {'mimetype': b'application/vnd.oasis.opendocument.text', 'content.xml': content.encode(), 'styles.xml': styles.encode(),
               'META-INF/manifest.xml': manifest.encode(), 'Pictures/original.png': b'unchanged-image-bytes', 'meta.xml': b'<metadata>Original</metadata>'}
    entries.update(extra or {})
    with zipfile.ZipFile(path, 'w') as archive:
        for name, value in entries.items(): archive.writestr(name, value)
    return entries


def google_fixture():
    def paragraph(start, text, heading=False):
        # Indexes deliberately follow UTF-16, not Python's character count.
        end = start + len(text.encode('utf-16-le')) // 2
        return {'startIndex': start, 'endIndex': end, 'paragraph': {
            'paragraphStyle': {'namedStyleType': 'HEADING_1' if heading else 'NORMAL_TEXT', 'alignment': 'CENTER' if heading else 'JUSTIFIED',
                               'indentFirstLine': google_docs.pt(1), 'lineSpacing': 120},
            'elements': [{'startIndex': start, 'endIndex': end, 'textRun': {'content': text, 'textStyle': {
                'fontSize': {'magnitude': 22, 'unit': 'PT'}, 'bold': False, 'italic': True,
                'weightedFontFamily': {'fontFamily': 'Arial'}, 'link': {'url': 'https://example.org'}}}}]}}
    title = paragraph(1, 'Nádej 🙏\n', True)
    body = paragraph(title['endIndex'], '中文 že áno 🙏\n')
    return {'documentId': 'fixture_document_123', 'title': 'Fixture', 'revisionId': 'rev1', 'tabs': [
        {'tabProperties': {'tabId': 't.0', 'title': 'First'}, 'documentTab': {'documentStyle': {
            'pageSize': {'width': google_docs.pt(20), 'height': google_docs.pt(28)}, 'marginTop': google_docs.pt(2),
            'documentFormat': {'documentMode': 'PAGELESS'}}, 'body': {'content': [
                {'startIndex': 0, 'endIndex': 1, 'sectionBreak': {'sectionStyle': {'marginLeft': google_docs.pt(3)}}}, title, body]}},
         'childTabs': [{'tabProperties': {'tabId': 't.1', 'title': 'Second'}, 'documentTab': {'body': {'content': [paragraph(1, '第二分页\n')]}}}]}]}


class FakeDocs:
    def __init__(self): self.document = google_fixture(); self.gets, self.writes = [], []
    def documents(self): return self
    def get(self, **kw):
        self.gets.append(kw)
        return SimpleNamespace(execute=lambda **options: copy.deepcopy(self.document))
    def batchUpdate(self, **kw):
        self.writes.append(kw); self.document['revisionId'] = 'rev2'
        return SimpleNamespace(execute=lambda **options: {'writeControl': {'requiredRevisionId': 'rev2'}})


class OdtFormatterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.source = self.root/'原文 Slovak.odt'; self.entries = fixture_odt(self.source)

    def test_exact_page_paragraph_and_inline_sizes_without_rewriting_content(self):
        original = self.source.read_bytes(); snapshot = odt.inspect_odt(self.source)
        result = odt.format_odt(snapshot, DEFAULT_RULES)
        output = Path(result['output'])
        self.assertEqual(self.source.read_bytes(), original)
        after = odt.inspect_odt(output)
        self.assertEqual([item['text'] for item in after['paragraphs']], [item['text'] for item in snapshot['paragraphs']])
        self.assertEqual(result['formatted'], 2)
        with zipfile.ZipFile(output) as archive:
            for name in self.entries.keys() - {'content.xml', 'styles.xml'}: self.assertEqual(archive.read(name), self.entries[name])
            styles, content = odt.xml(archive.read('styles.xml')), odt.xml(archive.read('content.xml'))
            page = odt.elements(styles, 'style', 'page-layout-properties')[0]
            for key, expected in {'page-width': '21cm', 'page-height': '29.7cm', 'margin-top': '1.8cm', 'margin-bottom': '1.8cm', 'margin-left': '1.5cm', 'margin-right': '1.5cm'}.items():
                self.assertEqual(page.getAttributeNS(odt.NS['fo'], key), expected)
            names = {node.getAttributeNS(odt.NS['style'], 'name'): node for node in odt.elements(content, 'style', 'style')}
            for index, expected in ((0, '14pt'), (1, '12pt')):
                paragraph = odt.paragraph_nodes(content)[index]
                style = names[paragraph.getAttributeNS(odt.NS['text'], 'style-name')]
                props = odt.child(style, 'style', 'paragraph-properties')
                self.assertEqual(props.getAttributeNS(odt.NS['fo'], 'text-indent'), '0.84cm')
                self.assertEqual(props.getAttributeNS(odt.NS['fo'], 'line-height'), '150%')
                self.assertFalse(props.hasAttributeNS(odt.NS['fo'], 'text-align'))
                text_style = odt.child(style, 'style', 'text-properties')
                self.assertEqual(text_style.getAttributeNS(odt.NS['fo'], 'font-size'), expected)
                self.assertEqual(text_style.getAttributeNS(odt.NS['fo'], 'font-weight'), 'bold' if index == 0 else '')
                for inline in odt.elements(paragraph, 'text', 'span') + odt.elements(paragraph, 'text', 'a'):
                    inline_style = names[inline.getAttributeNS(odt.NS['text'], 'style-name')]
                    self.assertEqual(odt.child(inline_style, 'style', 'text-properties').getAttributeNS(odt.NS['fo'], 'font-size'), expected)
            self.assertEqual(archive.infolist()[0].filename, 'mimetype')
            self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)
        # Verify with the application's already-installed ODF parser too.
        from odf.opendocument import load
        self.assertTrue(load(str(output)).text)

    def test_output_collisions_preserve_previous_result(self):
        snapshot = odt.inspect_odt(self.source)
        first = odt.format_odt(snapshot, DEFAULT_RULES); first_bytes = Path(first['output']).read_bytes()
        second = odt.format_odt(snapshot, DEFAULT_RULES)
        self.assertNotEqual(first['output'], second['output'])
        self.assertEqual(Path(first['output']).read_bytes(), first_bytes)

    def test_stale_snapshot_no_selection_or_cancellation_writes_nothing(self):
        snapshot = odt.inspect_odt(self.source)
        with self.assertRaisesRegex(ValueError, '没有需要'): odt.format_odt(snapshot, DEFAULT_RULES, {item['id']: 'keep' for item in snapshot['paragraphs']})
        with self.assertRaises(BackgroundTaskCanceled): odt.format_odt(snapshot, DEFAULT_RULES, check=Mock(side_effect=BackgroundTaskCanceled()))
        self.source.write_bytes(self.source.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError, '修改'): odt.format_odt(snapshot, DEFAULT_RULES)
        self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_explicit_structured_paragraph_override_and_spacing(self):
        snapshot = odt.inspect_odt(self.source)
        roles = paragraph_roles(snapshot['paragraphs'], DEFAULT_RULES)
        self.assertEqual(list(roles.values()), ['title', 'body', 'keep', 'keep'])
        roles['p3'] = 'body'
        result = odt.format_odt(snapshot, {**DEFAULT_RULES, 'reset_spacing': True}, roles)
        self.assertEqual(result['formatted'], 3)
        with zipfile.ZipFile(result['output']) as archive:
            document = odt.xml(archive.read('content.xml'))
            styles = odt.elements(document, 'style', 'paragraph-properties')
            self.assertTrue(all(node.getAttributeNS(odt.NS['fo'], 'margin-top') == '0cm' for node in styles))

    def test_invalid_inputs_dtd_encryption_and_unsafe_package_rejected(self):
        with self.assertRaises(ValueError): odt.inspect_odt(self.root/'wrong.ods')
        for name, options in [('dtd', {'content': '<!DOCTYPE x [<!ENTITY x SYSTEM "file:///private">]><x>&x;</x>'}),
                              ('encrypted', {'manifest': f'<manifest:manifest xmlns:manifest="{odt.NS["manifest"]}"><manifest:encryption-data/></manifest:manifest>'}),
                              ('traversal', {'extra': {'../outside.txt': b'x'}})]:
            path = self.root/(name+'.odt'); fixture_odt(path, **options)
            with self.subTest(name=name), self.assertRaises(Exception): odt.inspect_odt(path)

    def test_minimal_document_gets_page_layout_and_metadata_stays_unchanged(self):
        entries = dict(self.entries)
        entries['styles.xml'] = f'<office:document-styles xmlns:office="{odt.NS["office"]}"><office:styles/></office:document-styles>'.encode()
        with zipfile.ZipFile(self.source, 'w') as archive:
            for name, value in entries.items(): archive.writestr(name, value)
        snapshot = odt.inspect_odt(self.source); output = odt.format_odt(snapshot, DEFAULT_RULES)['output']
        with zipfile.ZipFile(output) as archive:
            self.assertTrue(odt.elements(odt.xml(archive.read('styles.xml')), 'style', 'page-layout'))
            self.assertEqual(archive.read('meta.xml'), entries['meta.xml'])

    def test_batch_automatically_reads_unpreviewed_files_and_reports_partial_results(self):
        other = self.root/'第二.odt'; fixture_odt(other)
        run = SimpleNamespace(check_canceled=lambda: None, report=Mock())
        result = run_document_job({'operation': 'batch_odt', 'sources': [str(self.source), str(other)], 'known': [], 'rules': DEFAULT_RULES}, run)
        self.assertEqual(len(result['paths']), 2)
        with self.assertRaises(BackgroundTaskResultError) as error:
            run_document_job({'operation': 'batch_odt', 'sources': [str(self.source), str(self.root/'missing.odt')], 'known': [], 'rules': DEFAULT_RULES}, run)
        self.assertEqual([item['status'] for item in error.exception.result['results']], ['success', 'failed'])

    def test_incomplete_write_never_publishes_final_document_and_temp_is_cleaned(self):
        snapshot = odt.inspect_odt(self.source); original = self.source.read_bytes()
        from model.AppDataStorage import ManagedFile
        open_original = ManagedFile.open
        from contextlib import contextmanager
        @contextmanager
        def broken_open(managed, mode='rb', **options):
            with open_original(managed, mode, **options) as handle:
                if str(managed.path).endswith('.tmp') and mode == 'wb':
                    class Broken:
                        def write(self, raw): handle.write(raw[:10]); raise OSError('synthetic disk full')
                        def flush(self): pass
                    yield Broken()
                else: yield handle
        with patch.object(ManagedFile, 'open', broken_open), self.assertRaisesRegex(OSError, 'disk full'):
            odt.format_odt(snapshot, DEFAULT_RULES)
        self.assertEqual(self.source.read_bytes(), original); self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_corrupt_xml_is_one_batch_failure_not_a_global_abort(self):
        corrupt = self.root/'corrupt.odt'; fixture_odt(corrupt, content='<broken>')
        run = SimpleNamespace(check_canceled=lambda: None, report=Mock())
        with self.assertRaises(BackgroundTaskResultError) as error:
            run_document_job({'operation': 'batch_odt', 'sources': [str(corrupt), str(self.source)], 'known': [], 'rules': DEFAULT_RULES}, run)
        self.assertEqual([item['status'] for item in error.exception.result['results']], ['failed', 'success'])


class GoogleFormatterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.service = FakeDocs()

    def snapshot(self, tab='t.0'): return google_docs.inspect_document(self.service, 'fixture_document_123', tab)

    def test_url_validation_rejects_other_services_and_credentials_in_url(self):
        self.assertEqual(google_docs.document_target('https://docs.google.com/document/d/fixture_document_123/edit?tab=t.1'), ('fixture_document_123', 't.1'))
        for value in ('https://evil.example/document/d/fixture_document_123/edit', 'https://docs.google.com.evil.example/document/d/fixture_document_123',
                      'https://docs.google.com/spreadsheets/d/fixture_document_123', 'https://user@docs.google.com/document/d/fixture_document_123', 'http://docs.google.com/document/d/fixture_document_123'):
            with self.subTest(value=value), self.assertRaises(ValueError): google_docs.document_target(value)

    def test_nested_tabs_and_utf16_ranges_only_change_selected_tab_formatting(self):
        snapshot = self.snapshot(); self.assertEqual(len(snapshot['tabs']), 2)
        updates, restore, count = google_docs.requests_for(snapshot, DEFAULT_RULES)
        self.assertEqual(count, 2)
        self.assertTrue(all('update' in next(iter(request)) for request in updates + restore))
        page = updates[0]['updateDocumentStyle']; self.assertEqual(page['tabId'], 't.0')
        self.assertEqual(page['documentStyle']['pageSize']['width'], google_docs.pt(21))
        self.assertEqual(page['documentStyle']['documentFormat']['documentMode'], 'PAGES')
        styles = [request['updateTextStyle'] for request in updates if 'updateTextStyle' in request]
        self.assertEqual(styles[0]['range']['endIndex'], snapshot['paragraphs'][0]['end']-1)
        self.assertTrue(styles[0]['textStyle']['bold']); self.assertNotIn('bold', styles[1]['textStyle'])
        self.assertNotIn('weightedFontFamily', styles[1]['fields'])
        self.assertNotIn('alignment', updates[1]['updateParagraphStyle']['fields'])
        self.assertEqual(updates[1]['updateParagraphStyle']['paragraphStyle']['lineSpacing'], 150)
        second = self.snapshot('t.1'); self.assertEqual(second['paragraphs'][0]['text'], '第二分页\n')

    def test_stale_revision_refuses_write_and_does_not_create_backup(self):
        snapshot = self.snapshot(); self.service.document['revisionId'] = 'changed'
        with self.assertRaisesRegex(ValueError, '修改'): google_docs.apply_document(self.service, snapshot, DEFAULT_RULES, {}, self.root)
        self.assertEqual(self.service.writes, []); self.assertFalse(list(self.root.iterdir()))

    def test_backup_is_written_before_atomic_batch_and_restore_checks_revision(self):
        snapshot = self.snapshot(); value = google_docs.apply_document(self.service, snapshot, DEFAULT_RULES, {}, self.root)
        record = json.loads(Path(value['backup']).read_text(encoding='utf-8'))
        self.assertEqual(record['before'], snapshot); self.assertEqual(record['after_revision'], 'rev2')
        self.assertEqual(self.service.writes[0]['body']['writeControl'], {'requiredRevisionId': 'rev1'})
        google_docs.restore_document(self.service, self.root)
        self.assertEqual(self.service.writes[1]['body']['writeControl'], {'requiredRevisionId': 'rev2'})
        self.assertEqual(self.service.writes[1]['body']['requests'], record['restore'])
        self.assertEqual(json.loads(Path(value['backup']).read_text())['status'], 'restored')

    def test_later_edits_and_non_format_restore_requests_are_not_overwritten(self):
        value = google_docs.apply_document(self.service, self.snapshot(), DEFAULT_RULES, {}, self.root)
        self.service.document['revisionId'] = 'later'
        with self.assertRaisesRegex(ValueError, '又被编辑'): google_docs.restore_document(self.service, self.root)
        record = json.loads(Path(value['backup']).read_text(encoding='utf-8')); record['restore'] = [{'deleteContentRange': {'range': {'startIndex': 1, 'endIndex': 5}}}]
        Path(value['backup']).write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '非格式'): google_docs.restore_document(self.service, self.root)
        self.assertEqual(len(self.service.writes), 1)

    def test_offline_blocks_read_and_write_and_cancel_does_not_commit(self):
        snapshot = self.snapshot()
        with patch.object(google_docs, 'require_online', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError): google_docs.inspect_document(self.service, 'fixture_document_123')
            with self.assertRaises(RuntimeError): google_docs.apply_document(self.service, snapshot, DEFAULT_RULES, {}, self.root)
        with self.assertRaises(BackgroundTaskCanceled): google_docs.apply_document(self.service, snapshot, DEFAULT_RULES, {}, self.root, check=Mock(side_effect=BackgroundTaskCanceled()))
        self.assertEqual(self.service.writes, [])

    def test_zero_selected_paragraphs_never_commit_page_change_only(self):
        snapshot = self.snapshot()
        roles = {item['id']: 'keep' for item in snapshot['paragraphs']}
        with self.assertRaises(ValueError): google_docs.apply_document(self.service, snapshot, DEFAULT_RULES, roles, self.root)
        self.assertEqual(self.service.writes, [])

    def test_requests_match_installed_google_client_schema(self):
        from googleapiclient.discovery_cache import get_static_doc
        schema = json.loads(get_static_doc('docs', 'v1'))['schemas']
        updates, restore, _count = google_docs.requests_for(self.snapshot(), DEFAULT_RULES)
        for request in updates + restore:
            kind = next(iter(request)); definition = schema[kind[0].upper()+kind[1:]+'Request']['properties']
            self.assertFalse(set(request[kind])-set(definition))
            if kind == 'updateDocumentStyle': self.assertFalse(set(request[kind]['documentStyle'])-set(schema['DocumentStyle']['properties']))

    def test_storage_lost_after_backup_blocks_cloud_commit_without_plaintext_fallback(self):
        from model.AppDataStorage import ApplicationStorage, ManagedFile
        class Guard:
            lost = False
            def check(self, path=None, **kwargs):
                if self.lost: raise OSError('encrypted volume unavailable')
                return path
        guard = Guard(); storage = ApplicationStorage(self.root, guard=guard)
        write_original = ManagedFile.write_json
        def write_then_lose(file, value, **options):
            result = write_original(file, value, **options)
            if file.path.parent.name == 'google-history': guard.lost = True
            return result
        with patch('model.AppDataStorage.application_storage', return_value=storage), patch.object(ManagedFile, 'write_json', write_then_lose):
            with self.assertRaisesRegex(OSError, 'unavailable'):
                google_docs.apply_document(self.service, self.snapshot(), DEFAULT_RULES, {}, self.root)
        self.assertFalse(self.service.writes); self.assertFalse((self.root/'google-last.json').exists())

    def test_http_rejection_and_ambiguous_timeout_are_distinguished_without_retry(self):
        from googleapiclient.errors import HttpError
        import httplib2
        for error, message in ((HttpError(httplib2.Response({'status': '403'}), b'{"error":{"message":"denied"}}'), 'HTTP 403'),
                               (TimeoutError('synthetic timeout'), '结果未确认')):
            service = FakeDocs(); snapshot = google_docs.inspect_document(service, 'fixture_document_123')
            service.batchUpdate = Mock(side_effect=error)
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                google_docs.apply_document(service, snapshot, DEFAULT_RULES, {}, self.root)
            self.assertEqual(service.batchUpdate.call_count, 1)


class RuleTests(unittest.TestCase):
    def test_defaults_and_invalid_parameters(self):
        self.assertEqual(normalize_rules({}), DEFAULT_RULES)
        for value in ({'line_spacing': float('nan')}, {'margin_left_cm': 20}, {'title_pt': 0}, {'page_width_cm': -1}):
            with self.subTest(value=value), self.assertRaises(ValueError): normalize_rules(value)
        values = [{'id': 'p0', 'text': '', 'heading': False}, {'id': 'p1', 'text': 'Title'}, {'id': 'p2', 'text': 'Body'}]
        self.assertEqual(list(paragraph_roles(values, DEFAULT_RULES).values()), ['keep', 'title', 'body'])
        with self.assertRaises(ValueError): paragraph_roles(values, DEFAULT_RULES, {'other': 'body'})


if __name__ == '__main__': unittest.main()
