"""Current Agent workbook contracts, fake services and isolated directories only."""
import copy
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtWidgets
from app_plugins.builtin.ai_agent_assistant import sheets, segmentation
from app_plugins.builtin.ai_agent_assistant.store import Store
from app_plugins.builtin.ai_agent_assistant.tasks import snapshot, fingerprint, spreadsheet_id, validate_payload
from app_plugins.builtin.ai_agent_assistant.sheet_profiles import IMAGE_FIELDS, archive_value
from app_plugins.builtin.ai_agent_assistant.receipt_plan import delivery_plan, check_parts
from app_plugins.builtin.ai_agent_assistant.image_tasks import image_snapshot, bind_image_jobs, run_image_bind_list
from app_plugins.builtin.ai_agent_assistant.delivery import run_receipts
from test_ai_agent_assistant import target, URL, FakeSheets
from test_agent_delivery import ReceiptSheets, COLS

TEXT = '【第1段｜约6秒】\nFirst exact segment.\n\n【第2段｜约8秒】\nSecond exact segment!'
IMAGE_LINK = 'https://drive.google.com/file/d/synthetic_image/view'
VIDEO_LINK = 'https://drive.google.com/file/d/synthetic_video/view'
IMAGE_COLS = {key: index for index, (key, _, _) in enumerate(IMAGE_FIELDS)}


class ImageSheets(FakeSheets):
    def __init__(self, rows=None):
        super().__init__(header=[title for _, title, _ in IMAGE_FIELDS]); self.rows = rows or []
    def get(self, **kwargs):
        if 'ranges' in kwargs:
            result = {'sheets': [{'data': [{'rowData': [{'values': []}]}]}]}
        elif 'fields' in kwargs:
            result = {'spreadsheetUrl': URL, 'sheets': [{'properties': {'sheetId': 1, 'title': '图片转动画',
                'gridProperties': {'rowCount': 50, 'columnCount': len(self.header)}}}]}
        elif kwargs.get('range', '').split('!')[-1].startswith('A1:'):
            result = {'values': [['共享成品池'], self.header]}
        elif kwargs.get('valueRenderOption') == 'UNFORMATTED_VALUE':
            result = {'values': [[row[IMAGE_COLS['job_id']]] for row in self.rows]}
        else: result = {'values': copy.deepcopy(self.rows)}
        return SimpleNamespace(execute=lambda **_: result)
    def append(self, **kwargs):
        def execute(**_):
            self.writes.append(copy.deepcopy(kwargs)); self.rows.extend(copy.deepcopy(kwargs['body']['values']))
            return {'updates': {'updatedRows': len(kwargs['body']['values'])}}
        return SimpleNamespace(execute=execute)
    def update(self, **kwargs):
        self.writes.append(copy.deepcopy(kwargs)); return SimpleNamespace(execute=lambda **_: {'updatedCells': 1})


class SchemaV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.store = Store(self.root/'private')
        self.target = target(self.root); Path(self.target['target_dir']).mkdir()
        self.payload = snapshot(self.target, 'sk'); self.payload['spoken'] = TEXT
        self.run = SimpleNamespace(check_canceled=Mock(), report=Mock())
    def job(self, value=None):
        key = self.store.stage([value or self.payload])['imported'][0]
        return self.store.jobs([key])[0]
    def test_existing_boundaries_duration_and_whitespace_preserved(self):
        from app_plugins.builtin.script_segmenter import nodes
        with patch.object(nodes, 'build') as build:
            result = segmentation.split_spoken(TEXT)
        self.assertEqual(result['text'], TEXT); self.assertEqual(result['count'], 2); build.assert_not_called()
        self.assertEqual([p['seconds'] for p in segmentation.existing_segments(TEXT)], [6, 8])
    def test_bad_existing_markers_fail_without_estimation(self):
        for text in (TEXT.replace('第2段','第3段'), TEXT.replace('约6秒','约0秒'), 'prefix\n'+TEXT,
                     '【第1段｜约10秒】', '【第1段｜坏】\nabc', TEXT.replace('约8秒','坏秒')):
            with self.subTest(text=text), self.assertRaises(ValueError): segmentation.split_spoken(text)
    def test_reordered_current_oral_headers_write_assignment_requirements_only_to_owned_cells(self):
        job = self.job({**self.payload, 'user_requirements':'保持人脸', 'agent':'Worker A'})
        titles = [title for _,title,_ in sheets.FIELDS][::-1] + ['客户手填列']
        _, mapping, count = sheets.headers([titles]); row = sheets.row_values(job,mapping,count)
        self.assertEqual(row[mapping['spoken']], TEXT); self.assertEqual(row[mapping['agent']], 'Worker A')
        self.assertEqual(row[mapping['user_requirements']], '保持人脸'); self.assertIsNone(row[-1])
        self.assertIsNone(row[mapping['result']]); self.assertIsNone(row[mapping['heartbeat']])
    def test_archive_current_and_legacy_vocabulary(self):
        self.assertEqual(archive_value('oral'), '已归档')
        self.assertEqual(archive_value('oral', {'未下载','已下载'}),'已下载')
        self.assertEqual(archive_value('image'), '已下载')
        with self.assertRaises(ValueError): archive_value('oral', {'未下载'})
    def test_atomic_batch_assignment_rejects_changed_or_submitted_jobs(self):
        first=self.job(); second=self.job({**self.payload,'source_key':'second','task_id':'002'})
        expected={j['id']:fingerprint(j['payload']) for j in (first,second)}
        self.store.mark([second['id']], 'submitted', 'destination')
        with self.assertRaises(ValueError): self.store.edit_batch(list(expected),'agent','Replacement',expected=expected)
        self.assertEqual(self.store.jobs([first['id']])[0]['payload']['agent'], 'Fixture Agent')
    def test_image_snapshot_is_unique_per_image_stable_and_needs_no_script(self):
        self.target['task'].task_audio_text=''; value=image_snapshot(self.target, IMAGE_LINK)
        validate_payload(value); self.assertEqual(value['parent_task_id'],'001')
        self.assertEqual(value['task_id'],image_snapshot(self.target,IMAGE_LINK)['task_id'])
        self.assertNotEqual(value['task_id'],image_snapshot(self.target,IMAGE_LINK.replace('synthetic_image','other_image'))['task_id'])
        self.assertEqual(value['target_dir'],self.target['target_dir'])
    def test_image_folders_docs_and_multiple_inputs_rejected(self):
        for source in ('https://drive.google.com/drive/folders/synthetic_folder',
                       'https://docs.google.com/document/d/synthetic/view', IMAGE_LINK+'\n'+IMAGE_LINK.replace('image','second')):
            with self.assertRaises(ValueError): image_snapshot(self.target,source)
    def test_image_export_second_row_header_no_oral_or_private_path(self):
        job=self.job(image_snapshot(self.target,IMAGE_LINK)); api=ImageSheets()
        result=sheets.run_export({'url':URL,'sheet':'图片转动画','root':str(self.store.root.path),
            'ids':[job['id']],'operation':'submit'},self.run,service=api)
        self.assertEqual(result['added'],1); row=api.rows[0]
        self.assertEqual(row[IMAGE_COLS['status']],'待生成'); self.assertEqual(row[IMAGE_COLS['reference']],IMAGE_LINK)
        self.assertEqual(row[IMAGE_COLS['archive']],'未下载'); self.assertIsNone(row[IMAGE_COLS['result']])
        self.assertNotIn(str(self.root),str(api.writes))
    def test_image_and_oral_target_mismatch_never_uploads_or_writes(self):
        job=self.job(image_snapshot(self.target,IMAGE_LINK)); api=FakeSheets()
        with self.assertRaises(ValueError): sheets.run_export({'url':URL,'sheet':'口播表格','root':str(self.store.root.path),
            'ids':[job['id']],'operation':'submit'},self.run,service=api)
        self.assertEqual(api.writes,[])
    def test_separate_settings_do_not_erase_each_other(self):
        self.store.save_settings({'url':URL,'sheet':'口播表格','default_agent':'Worker A'})
        self.store.save_settings({'url':URL,'sheet':'图片转动画','kind':'image'})
        self.assertEqual(self.store.settings()['sheet'],'口播表格'); self.assertEqual(self.store.settings()['image_sheet'],'图片转动画')
        self.assertEqual(self.store.settings()['default_agent'],'Worker A')
    def test_image_bind_reads_cloud_and_requires_explicit_local_directory(self):
        values={'job_id':'cloud_job_12345','task_id':'img01','reference':'existing.png','status':'已交付','quality':'通过','result':VIDEO_LINK}
        api=ImageSheets([[values.get(key,'') for key,_,_ in IMAGE_FIELDS]])
        result=run_image_bind_list({'url':URL,'sheet':'图片转动画','target_dir':self.target['target_dir'],
            'parent_task_id':'001'},self.run,service=api)
        ids=bind_image_jobs(self.store,result['image_bind_choices'],result['binding'])
        job=self.store.jobs(ids)[0]; self.assertEqual(job['state'],'submitted')
        self.assertEqual(job['payload']['target_dir'],self.target['target_dir']); self.assertEqual(api.writes,[])
        bind_image_jobs(self.store,result['image_bind_choices'],result['binding']); self.assertEqual(len(self.store.jobs()),1)
        other=self.root/'other'; other.mkdir()
        with self.assertRaises(ValueError): bind_image_jobs(self.store,result['image_bind_choices'],{**result['binding'],'target_dir':str(other)})
    def test_segment_checks_count_duplicate_owner_and_image_filenames(self):
        plan=delivery_plan('oral',TEXT)
        self.assertIn('缺段',check_parts([{'id':'one','name':'任务001_第01段.mp4'}],plan,'001'))
        for names in (['任务002_第01段.mp4'], ['任务001_第01段.mp4']*2, ['任务001_第03段.mp4']):
            with self.assertRaises(ValueError): check_parts([{'id':str(i),'name':name} for i,name in enumerate(names)],plan,'001')
        self.assertEqual(check_parts([{'id':'a','name':'任务001_第01段.mp4'},{'id':'b','name':'任务001_第02段.mp4'}],plan,'001'),'')
        self.assertEqual(check_parts([{'id':'a','name':'任务OTHER_第99段.mp4'}],delivery_plan('image',''),'001'),'')
    def test_incomplete_declared_delivery_downloads_available_without_archiving_until_complete(self):
        job=self.job(); self.store.mark([job['id']],'submitted',spreadsheet_id(URL)+':口播表格')
        cloud={'job_id':job['id'],'task_id':'001','status':'已完成','spoken':TEXT,'result':VIDEO_LINK,'archive':'未下载'}
        api=ReceiptSheets([[cloud.get(key,'') for key,_,_ in sheets.FIELDS]])
        items=[{'id':'one','name':'任务001_第01段.mp4','url':VIDEO_LINK}]
        def fetch(job,item,directory,drive,run):
            path=directory/item['name']; path.write_bytes(b'test')
            return {'state':'downloaded','path':str(path),'size':4,'sha256':hashlib.sha256(b'test').hexdigest()}
        download=Mock(side_effect=fetch)
        with patch('app_plugins.builtin.ai_agent_assistant.delivery.collect_files',side_effect=lambda *_,**__:items):
            run_receipts({'root':str(self.store.root.path)},self.run,sheets=api,drive=object(),fetch=download)
            self.assertEqual(download.call_count,1)
            self.assertEqual(api.writes,[]); self.assertEqual(self.store.delivery_files(job['id'])['@discovery']['total'],2)
            items.append({'id':'two','name':'任务001_第02段.mp4','url':VIDEO_LINK})
            run_receipts({'root':str(self.store.root.path)},self.run,sheets=api,drive=object(),fetch=download)
        self.assertEqual(download.call_count,2); self.assertEqual(len(api.writes),1)
    def test_current_archive_rule_writes_archived_not_invalid_downloaded(self):
        from app_plugins.builtin.ai_agent_assistant.delivery import acknowledge
        job=self.job(); row=['']*len(sheets.FIELDS)
        for key,value in {'job_id':job['id'],'task_id':'001','status':'已完成','result':VIDEO_LINK,'spoken':TEXT}.items(): row[COLS[key]]=value
        api=ReceiptSheets([row]); original=api.get
        def get(**kwargs):
            if 'ranges' not in kwargs: return original(**kwargs)
            return SimpleNamespace(execute=lambda **_: {'sheets':[{'data':[{'rowData':[{'values':[{'dataValidation':{'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':'已归档'}]}}}]}]}]}]})
        api.get=get
        acknowledge(api,spreadsheet_id(URL),'口播表格',{'height':1000,'header':1,'mapping':COLS,'count':len(sheets.FIELDS),'kind':'oral'},job,VIDEO_LINK,self.run,delivery_plan('oral',TEXT))
        self.assertEqual(api.writes[0]['body']['values'],[['已归档']])

    def test_oral_root_only_does_not_scan_quality_subfolders(self):
        from app_plugins.builtin.ai_agent_assistant.delivery_drive import collect_files, FOLDER
        api=Mock(); api.files.return_value.get.return_value.execute.return_value={'id':'root_folder','mimeType':FOLDER}
        api.files.return_value.list.return_value.execute.return_value={'files':[
            {'id':'passed','mimeType':'video/mp4','name':'任务001_第01段.mp4'},
            {'id':'quality','mimeType':FOLDER,'name':'待查'}]}
        files=collect_files(['https://drive.google.com/drive/folders/root_folder'],api,self.run,root_only=True)
        self.assertEqual([item['id'] for item in files],['passed']); self.assertEqual(files[0]['_depth'],0)
        self.assertEqual(api.files.return_value.list.call_count,1)

    def test_image_receipt_requires_delivered_quality_and_direct_video(self):
        value=image_snapshot(self.target,IMAGE_LINK); job=self.job(value)
        self.store.mark([job['id']],'submitted',spreadsheet_id(URL)+':图片转动画')
        values={'job_id':job['id'],'task_id':value['task_id'],'status':'已交付','quality':'待用户终审','result':VIDEO_LINK,'reference':IMAGE_LINK}
        api=ImageSheets([[values.get(key,'') for key,_,_ in IMAGE_FIELDS]])
        def fetch(job,item,directory,drive,run):
            path=directory/'completed.mp4'; path.write_bytes(b'test')
            return {'state':'downloaded','path':str(path),'size':4,'sha256':hashlib.sha256(b'test').hexdigest()}
        download=Mock(side_effect=fetch)
        with patch('app_plugins.builtin.ai_agent_assistant.delivery.collect_files',return_value=[{'id':'one','name':'any.mp4','url':VIDEO_LINK}]):
            run_receipts({'root':str(self.store.root.path)},self.run,sheets=api,drive=object(),fetch=download)
            download.assert_not_called(); self.assertEqual(api.writes,[])
            api.rows[0][IMAGE_COLS['quality']]='通过'
            run_receipts({'root':str(self.store.root.path)},self.run,sheets=api,drive=object(),fetch=download)
        download.assert_called_once(); self.assertEqual(api.writes[0]['body']['values'],[['已下载']])

    def test_existing_cloud_row_never_overwrites_user_requirements(self):
        job=self.job({**self.payload,'user_requirements':'local old text'}); api=FakeSheets(ids=[job['id']])
        result=sheets.run_export({'url':URL,'sheet':'口播表格','root':str(self.store.root.path),
            'ids':[job['id']],'operation':'submit'},self.run,service=api)
        self.assertEqual(result['confirmed'],1); self.assertEqual(api.writes,[])


class ImagePageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        from model.AppDataStorage import data_directory
        from app_plugins.builtin.ai_agent_assistant.plugin import AIAgentAssistantPlugin
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.parent=QtWidgets.QWidget(); self.plugin=AIAgentAssistantPlugin()
        self.target=target(self.temp.name); Path(self.target['target_dir']).mkdir()
        self.context=SimpleNamespace(parent_widget=self.parent,storage=data_directory(Path(self.temp.name)/'private'),
            register_command=Mock(),register_main_widget=Mock(),register_google_authorization=Mock(),log=Mock(),
            task_language=lambda _: 'sk',selected_task_rows=lambda:[0],task_targets=lambda _: [self.target],
            available_nodes=lambda: SimpleNamespace(definitions={}), prepare_google_account=Mock(return_value={'account_id':'fixture'}),
            submit_background_task=Mock(return_value=Mock()))
        self.plugin.register(self.context); self.plugin.store.save_settings({'url':URL,'sheet':'口播表格'})
        self.dialog=self.plugin.open_dialog()
    def tearDown(self):
        self.parent.deleteLater(); self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete); self.app.processEvents()
    def test_two_profiles_separate_rows_and_editable_columns(self):
        self.plugin.import_targets([self.target]); self.target['task'].task_reference_link=IMAGE_LINK
        self.dialog.image_page.import_selected()
        self.dialog.reload()
        self.assertEqual(self.dialog.table.rowCount(),1); self.assertEqual(self.dialog.image_page.table.rowCount(),1)
        self.assertEqual(self.dialog.table.horizontalHeaderItem(8).text(),'执行 Agent')
        self.assertEqual(self.dialog.table.horizontalHeaderItem(9).text(),'用户需求')
    def test_presegmented_or_image_submit_does_not_require_segmenter_plugin(self):
        self.target['task'].task_audio_text=TEXT; self.plugin.import_targets([self.target]); job=self.plugin.store.jobs()[0]
        self.plugin.submit({'url':URL,'sheet':'口播表格'},[job['id']],'submit')
        self.context.submit_background_task.assert_called_once(); self.plugin.handle=None
        value=image_snapshot(self.target,IMAGE_LINK); key=self.plugin.store.stage([value])['imported'][0]
        self.plugin.submit({'url':URL,'sheet':'图片转动画','kind':'image'},[key],'submit')
        self.assertEqual(self.context.submit_background_task.call_count,2); self.plugin.handle=None
    def test_missing_assignment_cancel_does_not_submit(self):
        self.target['task'].task_audio_text=TEXT; self.target['task'].task_agent=''; self.plugin.import_targets([self.target])
        key=self.plugin.store.jobs()[0]['id']
        with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.StandardButton.No):
            self.plugin.submit({'url':URL,'sheet':'口播表格'},[key],'submit')
        self.context.submit_background_task.assert_not_called()
    def test_default_assignment_only_fills_missing(self):
        self.target['task'].task_audio_text=TEXT; self.target['task'].task_agent=''; self.plugin.import_targets([self.target])
        key=self.plugin.store.jobs()[0]['id']
        self.plugin.submit({'url':URL,'sheet':'口播表格','default_agent':'Worker B'},[key],'submit')
        self.assertEqual(self.plugin.store.jobs([key])[0]['payload']['agent'],'Worker B'); self.plugin.handle=None

    def test_image_refresh_does_not_select_another_job_after_removal(self):
        first=image_snapshot(self.target,IMAGE_LINK); second=image_snapshot(self.target,IMAGE_LINK.replace('image','second'))
        ids=self.plugin.store.stage([first,second])['imported']; page=self.dialog.image_page
        page.reload(select=[ids[0]]); job=self.plugin.store.jobs([ids[0]])[0]
        self.plugin.store.remove_records({job['id']:(job['state'],fingerprint(job['payload']))})
        page.reload()
        self.assertEqual(page.table.rowCount(),1); self.assertEqual(page.table.selectionModel().selectedRows(),[])
