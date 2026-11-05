"""Legacy Sxx delivery names and durable completion recovery; no live services."""
import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from app_plugins.builtin.ai_agent_assistant.receipt_plan import check_parts, delivery_plan, part_number
from app_plugins.builtin.ai_agent_assistant.delivery import run_receipts
from app_plugins.builtin.ai_agent_assistant.sheets import FIELDS
from app_plugins.builtin.ai_agent_assistant.store import Store
from app_plugins.builtin.ai_agent_assistant.tasks import snapshot, spreadsheet_id
from app_plugins.builtin.ai_agent_assistant.notice_data import notice_snapshot
from test_ai_agent_assistant import target, URL
from test_agent_delivery import ReceiptSheets


class ReceiptNamesTests(unittest.TestCase):
    def test_explicit_supported_names_and_local_prefix(self):
        for name in ('01_S01.mp4', '1_s1.MP4', 'S01.mp4', '任务44_第01段.mp4',
                     'Agent_abcdef123456_abcdef123456_01_S01.mp4'):
            with self.subTest(name=name): self.assertEqual(part_number(name,'44'),1)
    def test_numbers_without_explicit_segment_tag_are_not_guessed(self):
        for name in ('01.mp4','2026-11-05.mp4','clip123.mp4','preview_S01_old.mp4'):
            with self.subTest(name=name): self.assertIsNone(part_number(name,'44'))
    def test_inconsistent_prefix_multiple_tags_and_other_task_are_rejected(self):
        for name in ('01_S02.mp4','任务45_第01段.mp4','第01段_第02段.mp4'):
            with self.subTest(name=name),self.assertRaises(ValueError): part_number(name,'44')
    def test_complete_legacy_s_names_and_mixed_names_are_checked_by_number(self):
        plan={'count':3,'segments':[]}
        files=[{'id':'c','name':'03_S03.mp4'},{'id':'a','name':'任务44_第01段.mp4'},{'id':'b','name':'S02.mp4'}]
        self.assertEqual(check_parts(files,plan,'44'),'')
        files[2]['name']='S03.mp4'
        with self.assertRaises(ValueError): check_parts(files,plan,'44')
    def test_missing_segment_is_listed_and_duplicate_does_not_replace_it(self):
        message=check_parts([{'id':'a','name':'01_S01.mp4'},{'id':'c','name':'03_S03.mp4'}],{'count':3},'44')
        self.assertIn('尚未核实段号：2',message)
        with self.assertRaises(ValueError): check_parts([{'id':'a','name':'01_S01.mp4'},{'id':'b','name':'S01.mp4'}],{'count':2},'44')
    def test_cached_public_download_filename_can_provide_the_ordinal(self):
        files=[{'id':'a'},{'id':'b'}]
        records={'a':{'path':'C:/result/Agent_abcdef123456_abcdef123456_01_S01.mp4'},
                 'b':{'path':'C:/result/Agent_abcdef123456_abcdef123456_02_S02.mp4'}}
        self.assertEqual(check_parts(files,{'count':2},'44',records),'')

    def test_existing_14_and_13_file_queues_finish_without_redownload(self):
        for count in (14,13):
            with self.subTest(count=count),tempfile.TemporaryDirectory() as root:
                store=Store(Path(root)/'private'); value=snapshot(target(root,'44'),'sk')
                directory=Path(value['target_dir'])/'result'; directory.mkdir(parents=True)
                text='\n\n'.join(f'【第{i}段｜约10秒】\nExact segment {i}.' for i in range(1,count+1))
                value['spoken']=text; key=store.stage([value])['imported'][0]
                store.mark([key],'submitted',spreadsheet_id(URL)+':口播表格')
                items=[]
                for number in range(1,count+1):
                    file_id='file_'+str(number); name=f'{number:02}_S{number:02}.mp4'; data=b'cached-video'
                    path=directory/('Agent_abcdef123456_abcdef123456_'+name); path.write_bytes(data)
                    store.save_delivery(key,file_id,{'state':'downloaded','path':str(path),'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
                    items.append({'id':file_id,'name':name,'url':'https://drive.google.com/file/d/synthetic/view'})
                store.save_delivery(key,'@discovery',{'state':'checked','total':count})
                store.delivery_note(key,'收取待核对','旧版仅识别中文段号')
                values={'job_id':key,'task_id':'44','status':'已完成','spoken':text,'archive':'未下载',
                    'result':'https://drive.google.com/drive/folders/synthetic_folder','notes':'音频待用户终审；第03段请留意。'}
                api=ReceiptSheets([[values.get(field,'') for field,_,_ in FIELDS]])
                run=SimpleNamespace(check_canceled=Mock(),report=Mock()); fetch=Mock()
                with patch('app_plugins.builtin.ai_agent_assistant.delivery.collect_files',return_value=items):
                    result=run_receipts({'root':str(store.root.path)},run,sheets=api,drive=object(),fetch=fetch)
                fetch.assert_not_called(); self.assertEqual(result['downloaded'],0)
                self.assertEqual(store.delivery_statuses()[key]['status'],'已下载'); self.assertEqual(len(api.writes),1)
                self.assertIn('音频待用户终审',store.delivery_statuses()[key]['detail'])
                self.assertEqual(notice_snapshot(store,announce=False)['items'][0]['text'],f'新成品 · {count}')
