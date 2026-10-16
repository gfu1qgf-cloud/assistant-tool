import copy
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from model.AppDataStorage import ApplicationStorage
from model.BrowserBookmarks import (BrowserProfile,BookmarkLibrary,discover_profiles,read_profile,
    url_key,chromium_entries,empty_chromium,ROOT_GUIDS)


def chrome_tree(url='https://example.test/a?x=1#keep'):
    roots={}
    for index,name in enumerate(('bookmark_bar','other','synced'),1):
        roots[name]={'type':'folder','id':str(index),'guid':f'root-{index}',
            'name':'书签栏' if index==1 else name,'children':[]}
    roots['bookmark_bar']['children']=[{'type':'folder','id':'4','name':'原文件夹','children':[
        {'type':'url','id':'5','name':'标题一','url':url}]}]
    return {'version':1,'checksum':'original','checksum_sha256':'original','roots':roots,
            'sync_metadata':'preserve-but-cloud-sync-can-repopulate'}


def firefox_fixture(path):
    db=sqlite3.connect(path)
    db.executescript('''
    CREATE TABLE moz_places(id INTEGER PRIMARY KEY,url TEXT,foreign_count INTEGER,visit_count INTEGER);
    CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY,parent INTEGER,type INTEGER,fk INTEGER,title TEXT,
        guid TEXT UNIQUE,lastModified INTEGER,syncStatus INTEGER,syncChangeCounter INTEGER);
    CREATE TABLE moz_bookmarks_deleted(guid TEXT PRIMARY KEY,dateRemoved INTEGER);
    CREATE TABLE moz_items_annos(item_id INTEGER,content TEXT);
    INSERT INTO moz_places VALUES(1,'https://example.test/a?x=1#keep',1,45);
    INSERT INTO moz_places VALUES(2,'https://history-only.test/',0,9);
    ''')
    for index,guid in enumerate(ROOT_GUIDS,1):
        db.execute('INSERT INTO moz_bookmarks VALUES(?,?,?,?,?,?,?,?,?)',(index,0 if index==1 else 1,2,None,guid,guid,0,2,0))
    db.execute('INSERT INTO moz_bookmarks VALUES(7,3,1,1,?, ?,0,2,0)',('火狐书签','bookmark___1'))
    db.execute('INSERT INTO moz_items_annos VALUES(7,?)',('annotation',)); db.commit(); db.close()


class Guard:
    available=True
    def check(self,*args,**kwargs):
        if not self.available:raise OSError('lost volume')
    def close(self):pass


class BookmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.data=self.root/'encrypted-test-fixture';self.data.mkdir()
        self.storage=ApplicationStorage(self.data,guard=Guard());self.addCleanup(self.storage.close)
        self.patch=patch('model.AppDataStorage._APPLICATION_STORAGE',self.storage);self.patch.start();self.addCleanup(self.patch.stop)
        self.running=patch('model.BrowserBookmarks.running_browsers',return_value=[]);self.running.start();self.addCleanup(self.running.stop)
        self.library=BookmarkLibrary(self.storage.for_plugin('chrome_launcher'))
        self.chrome=self.root/'chrome'/'Default'/'Bookmarks';self.chrome.parent.mkdir(parents=True)
        self.chrome.write_text(json.dumps(chrome_tree(),ensure_ascii=False),encoding='utf8')
        self.profile=BrowserProfile('Chrome','默认',str(self.chrome))
    def test_import_deduplicates_exact_urls_preserves_titles_folders_and_sources(self):
        edge=self.root/'Edge'/'Profile 1'/'Bookmarks';edge.parent.mkdir(parents=True)
        other=chrome_tree();other['roots']['bookmark_bar']['children'][0]['children'][0]['name']='另一标题'
        edge.write_text(json.dumps(other),encoding='utf8')
        firefox=self.root/'Firefox'/'places.sqlite';firefox.parent.mkdir();firefox_fixture(firefox)
        before=self.chrome.read_bytes()
        result=self.library.import_profiles([self.profile,BrowserProfile('Edge','用户1',str(edge)),BrowserProfile('Firefox','default',str(firefox),'firefox')])
        self.assertEqual((result['read'],result['added'],result['duplicates'],result['profiles']),(3,1,2,3))
        item=self.library.load()['items'][0];self.assertEqual(len(item['origins']),3)
        self.assertIn('原文件夹',item['origins'][0]['folder']);self.assertEqual(item['title'],'标题一')
        self.assertEqual(before,self.chrome.read_bytes());self.assertEqual(len(read_profile(BrowserProfile('Firefox','default',str(firefox),'firefox'))[0]),1)
    def test_url_key_keeps_query_fragment_case_paths_slashes_and_special_schemes(self):
        self.assertEqual(url_key('HTTPS://EXAMPLE.test/A?x=1#X'),'https://example.test/A?x=1#X')
        self.assertNotEqual(url_key('https://example.test/A'),url_key('https://example.test/a'))
        self.assertNotEqual(url_key('https://example.test/A'),url_key('https://example.test/A/'))
        self.assertNotEqual(url_key('https://example.test/A?x=1'),url_key('https://example.test/A?x=2'))
        self.assertEqual(url_key('javascript:alert(1)'),'javascript:alert(1)')
        self.assertNotEqual(url_key('https://User:Pass@EXAMPLE.test/'),url_key('https://user:pass@example.test/'))
    def test_chromium_checksum_matches_native_empty_root_encoding(self):
        import hashlib
        original=chrome_tree();value=empty_chromium(original)
        expected=b'1'+ '书签栏'.encode('utf-16-le')+b'folder'+b'2'+'other'.encode('utf-16-le')+b'folder'+b'3'+'synced'.encode('utf-16-le')+b'folder'
        self.assertEqual(value['checksum'],hashlib.md5(expected,usedforsecurity=False).hexdigest())
        self.assertEqual(value['checksum_sha256'],hashlib.sha256(expected).hexdigest())
        self.assertEqual(chromium_entries(value),[]);self.assertEqual(original['checksum'],'original')
        self.assertEqual(value['sync_metadata'],original['sync_metadata'])
    def test_cleanup_backup_roundtrip_and_sources_only_after_library_verification(self):
        before=self.chrome.read_bytes();result=self.library.import_profiles([self.profile])
        cleanup=self.library.clear_imported(result['batch'])
        self.assertEqual(cleanup['removed'],1);self.assertEqual(cleanup['failed'],0)
        self.assertEqual(read_profile(self.profile)[0],[])
        backup=self.library.storage.file(cleanup['profiles'][0]['backup'])
        self.assertEqual(backup.read_bytes(),before);self.assertTrue(backup.path.is_relative_to(self.data))
        self.assertEqual(self.library.restore(result['batch']),1);self.assertEqual(self.chrome.read_bytes(),before)
    def test_browser_open_changed_source_missing_library_and_invalid_schema_do_not_clear(self):
        result=self.library.import_profiles([self.profile]);before=self.chrome.read_bytes()
        with patch('model.BrowserBookmarks.running_browsers',return_value=['chrome.exe']),self.assertRaises(ValueError):self.library.clear_imported(result['batch'])
        self.assertEqual(before,self.chrome.read_bytes())
        self.chrome.write_text(json.dumps(chrome_tree('https://new.test/')))
        changed=self.chrome.read_bytes();cleanup=self.library.clear_imported(result['batch'])
        self.assertEqual(cleanup['failed'],1);self.assertEqual(changed,self.chrome.read_bytes())
        result=self.library.import_profiles([self.profile]);self.library.remove([self.library.load()['items'][-1]['id']])
        cleanup=self.library.clear_imported(result['batch']);self.assertEqual(cleanup['failed'],1);self.assertEqual(changed,self.chrome.read_bytes())
    def test_plaintext_backup_refused_and_volume_loss_blocks_cleanup(self):
        result=self.library.import_profiles([self.profile]);before=self.chrome.read_bytes()
        with patch('model.AppDataStorage._APPLICATION_STORAGE',ApplicationStorage(self.data)):
            with self.assertRaisesRegex(ValueError,'VC'):self.library.clear_imported(result['batch'])
        self.storage.guard.available=False
        with self.assertRaises(OSError):self.library.clear_imported(result['batch'])
        self.assertEqual(before,self.chrome.read_bytes())
    def test_restore_refuses_overwriting_new_bookmarks(self):
        result=self.library.import_profiles([self.profile]);self.library.clear_imported(result['batch'])
        self.chrome.write_text(json.dumps(chrome_tree('https://new.test/')));before=self.chrome.read_bytes()
        with self.assertRaisesRegex(ValueError,'新数据'):self.library.restore(result['batch'])
        self.assertEqual(before,self.chrome.read_bytes())
    def test_restore_verifies_backup_bytes_before_overwriting_source(self):
        result=self.library.import_profiles([self.profile]);cleanup=self.library.clear_imported(result['batch'])
        before=self.chrome.read_bytes()
        backup=self.library.storage.file(cleanup['profiles'][0]['backup']);backup.write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'SHA-256'):self.library.restore(result['batch'])
        self.assertEqual(before,self.chrome.read_bytes())
    def test_firefox_cleanup_preserves_history_roots_and_restores_transactional_snapshot(self):
        path=self.root/'firefox'/'places.sqlite';path.parent.mkdir();firefox_fixture(path)
        profile=BrowserProfile('Firefox','default',str(path),'firefox')
        result=self.library.import_profiles([profile]);cleanup=self.library.clear_imported(result['batch'])
        self.assertEqual(cleanup['failed'],0);self.assertEqual(read_profile(profile)[0],[])
        db=sqlite3.connect(path)
        try:
            self.assertEqual(db.execute('SELECT count(*) FROM moz_bookmarks').fetchone()[0],6)
            self.assertEqual(db.execute('SELECT visit_count FROM moz_places ORDER BY id').fetchall(),[(45,),(9,)])
            self.assertEqual(db.execute('SELECT foreign_count FROM moz_places WHERE id=1').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT guid FROM moz_bookmarks_deleted').fetchall(),[('bookmark___1',)])
        finally:db.close()
        self.assertEqual(self.library.restore(result['batch']),1);self.assertEqual(len(read_profile(profile)[0]),1)
    def test_unknown_firefox_schema_rolls_back_and_original_data_remains(self):
        path=self.root/'firefox'/'places.sqlite';path.parent.mkdir();firefox_fixture(path)
        db=sqlite3.connect(path);db.execute('ALTER TABLE moz_bookmarks RENAME COLUMN syncStatus TO incompatible');db.close()
        profile=BrowserProfile('Firefox','default',str(path),'firefox')
        result=self.library.import_profiles([profile]);before=path.read_bytes();cleanup=self.library.clear_imported(result['batch'])
        self.assertEqual(cleanup['failed'],1);self.assertEqual(before,path.read_bytes());self.assertEqual(len(read_profile(profile)[0]),1)
    def test_all_profiles_channels_account_bookmarks_and_firefox_ini_discovery(self):
        local=self.root/'local';roaming=self.root/'roaming'
        for prefix,channel in (('Google','Chrome'),('Google','Chrome Beta'),('Microsoft','Edge')):
            folder=local/prefix/channel/'User Data'
            for name in ('Default','Profile 1'):
                path=folder/name;path.mkdir(parents=True)
                (path/'Bookmarks').write_text(json.dumps(chrome_tree()))
            (folder/'Default'/'AccountBookmarks').write_text(json.dumps(chrome_tree()))
        ff=roaming/'Mozilla'/'Firefox';folder=ff/'Profiles'/'abc.default';folder.mkdir(parents=True);firefox_fixture(folder/'places.sqlite')
        (ff/'profiles.ini').write_text('[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\n')
        found=discover_profiles(local=local,roaming=roaming)
        self.assertEqual(len(found),10);self.assertEqual(sum(p.kind=='firefox' for p in found),1)
    def test_partial_import_reports_error_and_only_successful_profiles_enter_cleanup_batch(self):
        bad=self.root/'bad';bad.write_text('{broken')
        result=self.library.import_profiles([self.profile,BrowserProfile('Chrome','损坏',str(bad))])
        self.assertEqual(len(result['errors']),1);self.assertEqual(result['profiles'],1)
        cleanup=self.library.clear_imported(result['batch']);self.assertEqual(cleanup['failed'],0);self.assertEqual(bad.read_text(),'{broken')


class BookmarkPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from qt_compat import QtWidgets
        cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def test_panel_lists_search_add_delete_and_no_url_execution_for_javascript(self):
        from PYUI.browser_bookmarks_pyui import BrowserBookmarksPanel
        from qt_compat import QtWidgets,QtGui
        with tempfile.TemporaryDirectory() as root:
            storage=ApplicationStorage(root).for_plugin('chrome_launcher');library=BookmarkLibrary(storage)
            library.add('safe','https://example.test/');library.add('special','javascript:alert(1)')
            panel=BrowserBookmarksPanel(SimpleNamespace(storage=storage),None)
            self.assertEqual(panel.table.rowCount(),2)
            panel.search.setText('special');self.assertEqual(panel.table.rowCount(),1);panel.table.selectRow(0)
            with patch.object(QtWidgets.QMessageBox,'information'),patch.object(QtGui.QDesktopServices,'openUrl') as open_url:
                panel.open_selected();open_url.assert_not_called()
            panel.deleteLater();self.app.processEvents()
    def test_data_directory_failure_is_reported_without_escaping_qt(self):
        from PYUI.browser_bookmarks_pyui import BrowserBookmarksPanel
        from qt_compat import QtWidgets
        with tempfile.TemporaryDirectory() as root:
            owner=ApplicationStorage(root,guard=Guard());storage=owner.for_plugin('chrome_launcher')
            panel=BrowserBookmarksPanel(SimpleNamespace(storage=storage),None)
            owner.guard.available=False
            with patch.object(QtWidgets.QMessageBox,'warning') as warning:
                panel.open_data_directory();warning.assert_called_once()
            panel.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()
