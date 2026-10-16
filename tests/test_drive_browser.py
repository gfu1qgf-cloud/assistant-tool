import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from PIL import Image
from model.AppDataStorage import ApplicationStorage
from model import GoogleDriveBrowser as drive
from model.DrivePreviewCache import DrivePreviewCache, PINS
from model.DriveVideoFrames import RangeReader, BLOCK


class Run:
    def check_canceled(self): pass
    def report(self, *_): pass


class Guard:
    def __init__(self, root): self.volume = type('Volume', (), {'root': root})(); self.available = True
    def check(self, *_args, **_kwargs):
        if not self.available: raise OSError('volume unavailable')


class Response:
    def __init__(self, data=b'', status=200, headers=None): self.data, self.status_code, self.headers, self.closed = data, status, headers or {}, False
    def iter_content(self, size):
        for pos in range(0, len(self.data), size): yield self.data[pos:pos+size]
    def close(self): self.closed = True


class Session:
    def __init__(self, replies): self.replies, self.calls = list(replies), []
    def get(self, url, **kwargs): self.calls.append((url, kwargs)); return self.replies.pop(0)


class StorageCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.guard = Guard(self.root / 'private')
        self.storage = ApplicationStorage(self.root/'private', guard=self.guard)
        self.binding = patch('model.AppDataStorage._APPLICATION_STORAGE', self.storage); self.binding.start()
    def tearDown(self): self.binding.stop(); self.temp.cleanup()


class DriveBrowserTests(StorageCase):
    def test_offline_cached_original_preview_needs_no_login_or_network(self):
        from app_plugins.builtin.google_drive_browser.background import run_drive_job
        cache = DrivePreviewCache(self.root/'cache'); item = {'id':'img','mimeType':'image/png','version':'1'}
        path = cache.path('media','account',item); picture = io.BytesIO(); Image.new('RGB',(700,200),'cyan').save(picture,'PNG')
        with cache.writer(path,len(picture.getvalue())) as stream: stream.write(picture.getvalue())
        with patch('app_plugins.builtin.google_drive_browser.background.service',side_effect=AssertionError('offline login/network')):
            result = run_drive_job({'operation':'preview','item':item,'account':'account','cache_root':str(cache.root)},Run())
        self.assertEqual(result['kind'],'preview'); self.assertEqual(result['local'],str(path)); self.assertTrue(result['image'])

    def test_open_single_file_only_prepares_gallery_not_full_download(self):
        from app_plugins.builtin.google_drive_browser.background import run_drive_job
        item = {'id':'v','name':'large.mp4','mimeType':'video/mp4','size':'1000000000'}
        key = drive.snapshot_key(item)
        with patch('app_plugins.builtin.google_drive_browser.background.service') as factory:
            service = factory.return_value.__enter__.return_value; service.account='account'
            service.browse.return_value={'kind':'file','item':item}
            value = run_drive_job({'operation':'browse','item':item,'key':key,'cache_root':str(self.root/'cache')},Run())
            service.media.assert_not_called()
        self.assertEqual(value['kind'],'list'); self.assertTrue(value['single_file']); self.assertEqual(drive.load_snapshot(key)['items'][0]['id'],'v')

    def test_new_client_validation_never_accepts_custom_token_server(self):
        with self.assertRaisesRegex(ValueError,'桌面应用'):
            drive.client_config({'installed':{'client_id':'i','client_secret':'s','auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://evil.test/token'}})

    def test_offline_mode_rejects_new_network_work_before_credentials(self):
        with patch('model.OfflineMode._enabled',True):
            with self.assertRaisesRegex(PermissionError,'离线'): drive.load_credentials()

    def test_links_and_resource_keys(self):
        for url in ['https://drive.google.com/file/d/Abc_123/view?resourcekey=r-Key', 'https://drive.google.com/drive/folders/Abc_123?resourcekey=r-Key', 'https://drive.google.com/open?id=Abc_123&resourcekey=r-Key']:
            self.assertEqual(drive.target(url), {'id': 'Abc_123', 'resourceKey': 'r-Key'})
        for bad in ['http://drive.google.com/file/d/a', 'https://evil.example/file/d/a', 'https://drive.google.com@evil.test/file/d/a', 'file:///a', 'https://drive.google.com/open?id=a%0d%0ax']:
            with self.assertRaises(ValueError): drive.target(bad)
        self.assertEqual(drive.target(drive.item_link({'id': 'root'}))['id'], 'root')
        self.assertEqual(drive.target(drive.item_link({'id': 'shared'}))['id'], 'shared')

    def test_private_bookmarks_deduplicate_rename_remove_and_preserve(self):
        url = 'https://drive.google.com/drive/folders/abc?resourcekey=key'
        drive.bookmark('私有收藏', url); drive.bookmark('重命名', url)
        self.assertEqual(drive.read_bookmarks(), [{'name': '重命名', 'url': url}])
        file = self.storage.root/'PluginData/google_drive_browser/bookmarks.json'
        self.assertTrue(file.is_file()); original = file.read_bytes()
        self.guard.available = False
        with self.assertRaises(OSError): drive.bookmark('new', url)
        self.assertEqual(file.read_bytes(), original)
        self.guard.available = True; drive.bookmark('', url, remove=True); self.assertEqual(drive.read_bookmarks(), [])

    def test_no_plaintext_fallback_and_no_reset_corrupt_bookmarks(self):
        with patch('model.AppDataStorage._APPLICATION_STORAGE', ApplicationStorage(self.root/'plain')):
            with self.assertRaisesRegex(ValueError, '密盘'): drive.read_bookmarks()
        file = drive.private_root().file('bookmarks.json'); file.write_text('{broken')
        with self.assertRaises(ValueError): drive.bookmark('name', 'https://drive.google.com/file/d/abc/view')
        self.assertEqual(file.read_text(), '{broken')

    def test_snapshot_private_and_distinct_filters(self):
        item = {'id': 'abc', 'resourceKey': 'secret'}
        keys = [drive.snapshot_key(item, media=media) for media in ['all', 'image', 'video']]
        self.assertEqual(len(set(keys)), 3)
        drive.save_snapshot(keys[0], {'items': [{'name': '私有文件名'}]})
        self.assertEqual(drive.load_snapshot(keys[0])['items'][0]['name'], '私有文件名')
        with self.assertRaises(ValueError): drive.load_snapshot('../outside')

    def test_browse_folder_pagination_sort_query_and_no_google_thumbnail(self):
        session = Session([Response(json.dumps({'files': [{'id': 'a', 'name': '图片'}], 'nextPageToken': 'next'}).encode())])
        result = drive.DriveBrowserService(session, 'account', Run()).browse({'id': 'root'}, search="a'b", order='modifiedTime desc', media='image')
        self.assertEqual(result['next'], 'next')
        url, options = session.calls[0]
        self.assertIn("name contains 'a\\'b'", options['params']['q'])
        self.assertIn('folder,modifiedTime desc', options['params']['orderBy'])
        self.assertNotIn('thumbnailLink', options['params']['fields'])
        self.assertNotIn('webContentLink', options['params']['fields'])
        self.assertEqual(options['params']['pageSize'], 60)

    def test_resource_key_and_shortcut_cycle(self):
        item = {'id': 'x', 'resourceKey': 'k'}
        session = Session([Response(json.dumps({'id': 'x', 'mimeType': drive.SHORTCUT,
            'shortcutDetails': {'targetId': 'x'}}).encode())])
        with self.assertRaisesRegex(ValueError, '循环'): drive.DriveBrowserService(session, 'a', Run()).get(item)
        self.assertEqual(session.calls[0][1]['headers']['X-Goog-Drive-Resource-Keys'], 'x/k')

    def test_http_errors_are_sanitized_and_redirects_constrained(self):
        response = Response(b'secret refresh_token=hidden', 403)
        service = drive.DriveBrowserService(Session([response]), 'a', Run())
        with self.assertRaisesRegex(ValueError, '权限'): service.request('files')
        self.assertTrue(response.closed)
        session = Session([Response(status=302, headers={'Location': 'https://evil.test/token'})])
        with self.assertRaisesRegex(ValueError, '非Google'): drive.DriveBrowserService(session, 'a', Run()).request('files')
        self.assertEqual(len(session.calls), 1)

    def test_range_must_return_exact_206_and_no_read_on_200(self):
        item = {'id': 'x', 'size': '1000'}
        response = Response(b'abcde', 206, {'Content-Range': 'bytes 5-9/1000'})
        session = Session([response])
        self.assertEqual(drive.DriveBrowserService(session, 'a', Run()).read_range(item, 5, 9), b'abcde')
        self.assertEqual(session.calls[0][1]['headers']['Range'], 'bytes=5-9')
        response = Response(b'WHOLE VIDEO', 200); response.iter_content = Mock(side_effect=AssertionError('must not download'))
        with self.assertRaisesRegex(ValueError, '未下载完整'): drive.DriveBrowserService(Session([response]), 'a', Run()).read_range(item, 5, 9)
        response.iter_content.assert_not_called()

    def test_original_image_thumbnail_retains_all_edges_and_aspect(self):
        image = Image.new('RGB', (1800, 600), 'white')
        for x0, color in [(0, 'red'), (1700, 'blue')]: image.paste(color, (x0, 0, x0+100, 600))
        raw = io.BytesIO(); image.save(raw, 'PNG')
        session = Session([Response(raw.getvalue())]); cache = DrivePreviewCache(self.root/'cache')
        path = drive.DriveBrowserService(session, 'acct', Run()).thumbnail({'id': 'wide', 'mimeType': 'image/png', 'size': str(len(raw.getvalue()))}, cache)
        with Image.open(path) as thumb:
            self.assertEqual(thumb.size, (960, 320))
            self.assertGreater(thumb.getpixel((0, 160))[0], 230); self.assertGreater(thumb.getpixel((959, 160))[2], 230)
        self.assertIn('/drive/v3/files/wide', session.calls[0][0]); self.assertNotIn('thumbnail', session.calls[0][0])
        self.assertNotIn('wide', Path(path).name)

    def test_cached_thumbnail_does_not_network(self):
        cache = DrivePreviewCache(self.root/'cache'); item = {'id': 'x', 'version': '1', 'mimeType': 'video/mp4'}
        path = cache.path('thumbs', 'acct', item)
        with cache.writer(path, 3) as stream: stream.write(b'jpg')
        session = Session([])
        self.assertEqual(drive.DriveBrowserService(session, 'acct', Run()).thumbnail(item, cache), str(path)); self.assertFalse(session.calls)

    def test_video_thumbnail_uses_range_not_media_download(self):
        cache = DrivePreviewCache(self.root/'cache'); session = Session([])
        service = drive.DriveBrowserService(session, 'acct', Run())
        with patch('model.DriveVideoFrames.extract_frame', return_value=(b'own-frame', 2000)) as extract:
            result = service.thumbnail({'id': 'v', 'mimeType': 'video/mp4', 'size': '1000000000'}, cache)
        self.assertEqual(Path(result).read_bytes(), b'own-frame'); self.assertFalse(session.calls); extract.assert_called_once()

    def test_private_token_reuse_does_not_modify_original_upload_token(self):
        from google.oauth2.credentials import Credentials
        from datetime import datetime, timedelta
        credentials = Credentials(token='test-token', refresh_token='test-refresh', token_uri='https://oauth2.googleapis.com/token', client_id='id', client_secret='secret', scopes=[drive.FULL], expiry=datetime.utcnow()+timedelta(hours=1))
        original = self.storage.root/'GoogleDriveToken.json'; original.parent.mkdir(parents=True)
        original.write_text(credentials.to_json()); before = original.read_bytes()
        with patch.object(drive, 'TOKEN_FILE', original):
            actual, key = drive.load_credentials()
        self.assertEqual(actual.token, 'test-token'); self.assertEqual(len(key), 64); self.assertEqual(original.read_bytes(), before)
        self.assertTrue((self.storage.root/'PluginData/google_drive_browser/google_token.json').is_file())

    def test_missing_scope_not_silently_upgraded(self):
        file = drive.private_root().file('google_token.json'); file.write_json({'token_uri': 'https://oauth2.googleapis.com/token', 'scopes': ['https://www.googleapis.com/auth/documents']})
        original = file.read_bytes()
        with self.assertRaisesRegex(ValueError, '读取权限'): drive.load_credentials()
        self.assertEqual(file.read_bytes(), original)


class CacheTests(StorageCase):
    def test_no_private_cache_and_no_outside_delete(self):
        with self.assertRaisesRegex(ValueError, '加密卷'): DrivePreviewCache(self.storage.root/'cache')
        cache = DrivePreviewCache(self.root/'cache')
        with self.assertRaises(ValueError): cache._owned(self.root/'outside.jpg')

    def test_budget_nested_pins_and_only_owned_files_removed(self):
        cache = DrivePreviewCache(self.root/'cache', thumb_limit=10)
        a = cache.path('thumbs', 'acct', {'id': 'a'}); b = cache.path('thumbs', 'acct', {'id': 'b'})
        with cache.writer(a, 6) as stream: stream.write(b'123456')
        with cache.pin(a):
            with cache.pin(a): pass
            self.assertIn(str(a), PINS)
            with self.assertRaises(ValueError):
                with cache.writer(b, 6): pass
        with cache.writer(b, 6) as stream: stream.write(b'654321')
        self.assertFalse(a.exists()); self.assertTrue(b.exists())
        untouched = b.parent/'user-photo.jpg'; untouched.write_bytes(b'keep')
        cache.prune('thumbs', clear=True); self.assertEqual(untouched.read_bytes(), b'keep')

    def test_cancelled_writer_removes_partial_file_not_previous(self):
        cache = DrivePreviewCache(self.root/'cache'); path = cache.path('media', 'acct', {'id': 'x'})
        with self.assertRaises(RuntimeError):
            with cache.writer(path, 3) as stream: stream.write(b'123'); raise RuntimeError('cancel')
        self.assertFalse(path.exists()); self.assertFalse(path.with_suffix('.part').exists())

    def test_stale_part_is_clearable_and_cache_key_version_account_isolation(self):
        cache = DrivePreviewCache(self.root/'cache')
        values = [cache.path('media', account, {'id': 'x', 'version': version}) for account, version in [('a','1'),('b','1'),('a','2')]]
        self.assertEqual(len(set(values)), 3)
        stale = values[0].with_suffix('.part'); stale.parent.mkdir(parents=True); stale.write_bytes(b'old')
        cache.prune('media', clear=True); self.assertFalse(stale.exists())


class RangeTests(unittest.TestCase):
    def test_range_block_reuse_and_budget(self):
        fetch = Mock(side_effect=lambda start, end: b'x'*(end-start+1))
        reader = RangeReader(100*BLOCK, fetch, lambda: None, budget=2*BLOCK)
        self.assertEqual(reader.read(5,9), b'xxxxx'); reader.read(100,200)
        self.assertEqual(fetch.call_count, 1); reader.read(2*BLOCK,2*BLOCK+5)
        with self.assertRaisesRegex(ValueError, '流量上限'): reader.read(3*BLOCK,3*BLOCK+5)
        self.assertEqual(reader.used, 2*BLOCK)
