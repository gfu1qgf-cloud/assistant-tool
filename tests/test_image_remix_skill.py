import base64
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from PIL import Image
from qt_compat import QtCore,QtWidgets
from model.AppDataStorage import data_directory,data_file
from app_plugins.builtin.image_remix.skill_rules import SKILL_ID,load_snapshot,validate_snapshot,selected_documents
from app_plugins.builtin.image_remix.reference_inputs import planning_images,recent_reference_items
from app_plugins.builtin.image_remix.prompts import validate_spec
from app_plugins.builtin.image_remix.planning import Planner,request_body
from app_plugins.builtin.image_remix.store import Store
from test_image_remix import spec,png
from test_image_remix_planning import PlanningClient,response


class SkillPlanningTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.store=Store(self.root/'private')

    def job(self,**changes):
        values=dict(mode='instant',rewrite_enabled=True,rewrite_model='gpt-5-mini',prompt_skill=SKILL_ID,reference_mode='scene')
        values.update(changes)
        return self.store.create(spec(self.root,**values),'synthetic-identity')

    def with_reference(self,**changes):
        path=self.root/'original.png';path.write_bytes(png())
        return self.job(references=[str(path)],**changes)

    def test_text_only_skill_preserves_requirements_count_size_and_constraints(self):
        text='耶稣在受难前祷告。固定侧面机位，全白长袖圆领衣服，不要红披布，不要钉痕。'
        job=self.job(prompt=text,innovation=100,size='1024x1024')
        body=request_body(job,job['planning']['chunks'][0]);content=json.loads(body['input'][1]['content'])
        self.assertEqual(content['original_prompt'],text)
        self.assertEqual(content['innovation'],100)
        self.assertEqual(content['image_size'],'1024x1024')
        self.assertEqual(content['requested_variants'],[{'index':1},{'index':2},{'index':3}])
        self.assertEqual(body['text']['format']['schema']['properties']['variants']['items']['required'],['index','prompt'])
        self.assertNotIn('高创新时原文的非核心细节是可替换素材',body['input'][0]['content'])
        self.assertIn('受难之前',body['input'][0]['content'])

    def test_snapshot_is_frozen_and_later_resource_edits_do_not_change_requests(self):
        job=self.job();before=request_body(job,job['planning']['chunks'][0])
        with patch('app_plugins.builtin.image_remix.skill_rules.load_snapshot',side_effect=AssertionError('Do not reload a running job')):
            restored=Store(self.store.root).get(job['id'])
            self.assertEqual(request_body(restored,restored['planning']['chunks'][0]),before)

    def test_corrupt_or_missing_snapshot_does_not_fall_back_to_generic(self):
        for mutation in ('missing','corrupt'):
            with self.subTest(mutation=mutation):
                job=self.job()
                if mutation=='missing':job.pop('skill')
                else:job['skill']['documents']['SKILL.md']+='tampered'
                with self.assertRaises(ValueError):request_body(job,job['planning']['chunks'][0])

    def test_reference_is_an_actual_bounded_image_not_a_filename(self):
        job=self.with_reference();Path(job['spec']['references'][0]).unlink()
        body=Planner(self.store,Mock()).request_body(job,job['planning']['chunks'][0])
        parts=body['input'][1]['content']
        self.assertEqual(parts[0]['type'],'input_text');self.assertEqual(parts[1]['type'],'input_image')
        raw=base64.b64decode(parts[1]['image_url'].split(',',1)[1])
        self.assertLessEqual(len(raw),192*1024)
        with Image.open(io.BytesIO(raw)) as image:self.assertLessEqual(max(image.size),768)
        self.assertNotIn(job['spec']['references'][0],parts[0]['text'])
        self.assertIn('整图参考',json.loads(parts[0]['text'])['reference_role'])

    def test_reference_role_changes_planning_and_final_image_instructions(self):
        for role in ('scene','identity'):
            job=self.with_reference(reference_mode=role)
            planner=Planner(self.store,Mock());body=planner.request_body(job,job['planning']['chunks'][0])
            text=json.loads(body['input'][1]['content'][0]['text'])['reference_role']
            self.assertIn('整图参考' if role=='scene' else '仅人物身份',text)
            planner.accept(job,job['planning']['chunks'][0],response([0,1,2]))
            self.assertIn('参考图用于理解' if role=='scene' else '仅约束面部特征',job['items'][0]['prompt'])

    def test_reference_metadata_cannot_pretend_that_images_were_read(self):
        job=self.with_reference()
        with self.assertRaises(ValueError):request_body(job,job['planning']['chunks'][0])

    def test_missing_reference_stops_before_any_paid_request_and_is_not_unknown(self):
        job=self.with_reference();snapshot=self.store.root/job['id']/'references'/job['references'][0]['file'];snapshot.unlink()
        client=PlanningClient();Planner(self.store,client).ensure(job)
        self.assertEqual(client.rewrites,[]);self.assertEqual(client.creates,[])
        self.assertEqual(job['planning']['chunks'][0]['state'],'failed')
        self.assertEqual(job['status'],'planning_failed')

    def test_reference_snapshot_tampering_is_rejected_before_network(self):
        job=self.with_reference();path=self.store.root/job['id']/'references'/job['references'][0]['file']
        path.write_bytes(b'invalid')
        with self.assertRaises(ValueError):planning_images(self.store.root,job)

    def test_skill_batch_sends_the_same_rules_and_image_inputs(self):
        job=self.with_reference(mode='batch');client=PlanningClient()
        Planner(self.store,client).ensure(job)
        rows=[json.loads(line) for line in client.uploads[0][0].splitlines()]
        self.assertEqual(rows[0]['url'],'/v1/responses')
        self.assertEqual(rows[0]['body']['input'][1]['content'][1]['type'],'input_image')
        self.assertIn('LZX4',rows[0]['body']['input'][0]['content'])
        self.assertEqual(client.creates[0][1]['phase'],'rewrite')
        self.assertEqual(client.calls,[])

    def test_instant_text_planning_finishes_before_any_image_request(self):
        job=self.job();client=PlanningClient();planner=Planner(self.store,client)
        self.assertFalse(planner.ensure(job))
        self.assertEqual(len(client.rewrites),1)
        self.assertEqual(client.calls,[])
        self.assertEqual(job['planning']['state'],'done')
        artifact=data_file(self.store.root/job['id']/'creative_plans.json').read_json()
        self.assertEqual(artifact['prompt_skill']['sha256'],job['skill']['sha256'])

    def test_batch_missing_reference_never_uploads_or_submits(self):
        job=self.with_reference(mode='batch');client=PlanningClient()
        path=self.store.root/job['id']/'references'/job['references'][0]['file'];path.unlink()
        self.assertFalse(Planner(self.store,client).ensure(job))
        self.assertEqual(client.uploads,[]);self.assertEqual(client.creates,[])
        self.assertEqual(job['status'],'planning_failed')

    def test_rule_references_are_routed_without_always_sending_examples(self):
        snapshot=load_snapshot(SKILL_ID)
        plain=selected_documents(snapshot,'一个天使',False)
        continuity=selected_documents(snapshot,'耶稣从墓穴复活',False)
        self.assertNotIn('references/output-examples.md',plain)
        self.assertNotIn('references/biblical-continuity.md',plain)
        self.assertIn('references/biblical-continuity.md',continuity)
        self.assertIn('references/jesus-appearance.md',continuity)

    def test_legacy_jobs_do_not_acquire_new_rules_or_new_image_input_cost(self):
        job=self.store.create(spec(self.root,rewrite_enabled=True),'synthetic')
        self.assertNotIn('skill',job)
        body=request_body(job,job['planning']['chunks'][0])
        self.assertIsInstance(body['input'][1]['content'],str)
        self.assertNotIn('LZX4',body['input'][0]['content'])

    def test_skill_job_upgrades_database_without_downgrading_later_legacy_saves(self):
        job=self.job()
        with self.store.connection() as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],4)
        self.store.create(spec(self.root),'synthetic')
        with self.store.connection() as db:self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],4)

    def test_only_known_profiles_and_reference_modes_are_allowed(self):
        for values in ({'prompt_skill':'../../outside'},{'reference_mode':'execute'},{'prompt_skill':SKILL_ID,'rewrite_enabled':False}):
            with self.subTest(values=values),self.assertRaises(ValueError):validate_spec(spec(self.root,**values))


class RecentReferenceTests(unittest.TestCase):
    setUp=SkillPlanningTests.setUp
    job=SkillPlanningTests.job
    with_reference=SkillPlanningTests.with_reference
    def test_recent_snapshots_survive_original_moves_and_are_deduplicated(self):
        job=self.with_reference();self.with_reference()
        Path(job['spec']['references'][0]).unlink()
        rows=recent_reference_items(self.store.all(),self.store.root)
        self.assertEqual(len(rows),1)
        self.assertTrue(Path(rows[0]['path']).is_file())
        self.assertEqual(rows[0]['name'],'original.png')
        self.assertNotIn('recent_references',rows[0]['path'])

    def test_recent_list_is_bounded_and_does_not_trust_arbitrary_paths(self):
        job=self.with_reference();altered=deepcopy(job);altered['references'][0]['file']='../../outside.png'
        self.assertEqual(recent_reference_items([altered],self.store.root),[])
        missing=deepcopy(job);missing['id']='0'*32
        self.assertEqual(recent_reference_items([missing],self.store.root),[])
        self.assertEqual(len(recent_reference_items([job]*30,self.store.root,limit=1)),1)


class SkillUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    def setUp(self):
        from app_plugins.builtin.image_remix.plugin import ImageRemixPlugin
        from app_plugins.builtin.image_remix.ui import RemixDialog
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.parent=QtWidgets.QWidget();self.plugin=ImageRemixPlugin()
        self.plugin.context=SimpleNamespace(storage=data_directory(self.root/'private'))
        self.dialog=RemixDialog(self.plugin,self.parent)
    def tearDown(self):
        self.dialog.close();self.parent.deleteLater();self.app.sendPostedEvents(None,QtCore.QEvent.Type.DeferredDelete)
        self.app.processEvents();self.temp.cleanup()

    def test_text_first_defaults_and_skill_off_keep_an_explicit_opt_out(self):
        self.assertEqual(self.dialog.spec()['prompt_skill'],SKILL_ID)
        self.assertEqual(self.dialog.spec()['size'],'1024x1536')
        self.assertEqual(self.dialog.spec()['references'],[])
        self.dialog.prompt_skill.setCurrentIndex(0)
        self.assertEqual(self.dialog.spec()['prompt_skill'],'none')
        self.assertEqual(self.dialog.spec()['reference_mode'],'identity')

    def test_running_pre_skill_submitter_is_rejected_before_widget_actions(self):
        from app_plugins.builtin.image_remix.ui import RemixDialog
        with self.assertRaises(RuntimeError):RemixDialog(SimpleNamespace(submission_api_version=4),self.parent)

    def test_draft_persists_profile_and_reference_role_but_reusing_legacy_keeps_legacy(self):
        self.dialog.prompt.setPlainText('纯文字主题')
        self.dialog.reference_mode.setCurrentIndex(1)
        draft=self.dialog.spec();self.dialog.prompt.clear();self.dialog.load_draft(draft)
        self.assertEqual(self.dialog.spec()['reference_mode'],'identity')
        self.dialog.prompt.clear();self.dialog.load_draft({'prompt':'旧任务'},preserve_legacy=True)
        self.assertEqual(self.dialog.spec()['prompt_skill'],'none')

    def test_recent_picker_can_add_a_snapshot_without_file_dialog_or_api(self):
        store=Store(self.root/'private');path=self.root/'original.png';path.write_bytes(png())
        job=store.create(spec(self.root,rewrite_enabled=True,references=[str(path)]),'synthetic')
        self.dialog.jobs=[job];path.unlink()
        def accept(dialog):
            images=dialog.findChild(QtWidgets.QListWidget);self.assertIsNotNone(images)
            images.item(0).setSelected(True)
            return QtWidgets.QDialog.DialogCode.Accepted
        with patch.object(QtWidgets.QDialog,'exec',accept),patch.object(QtWidgets.QFileDialog,'getOpenFileNames',side_effect=AssertionError('No file picker')):
            self.dialog.choose_recent_references()
        self.assertEqual(self.dialog.references.count(),1)
        self.assertTrue(Path(self.dialog.spec()['references'][0]).is_file())


class SkillPackagingTests(unittest.TestCase):
    def test_runtime_rules_are_bundled_without_private_data_or_developer_docs(self):
        import ast
        import app_plugins.builtin.image_remix as package
        root=Path(package.__file__).resolve().parents[3]
        tree=ast.parse(data_file(root/'packaging/AssistantTool.spec').read_text(encoding='utf-8-sig'))
        position=next(i for i,node in enumerate(tree.body) if isinstance(node,ast.Assign)
                      and any(isinstance(target,ast.Name) and target.id=='image_skill' for target in node.targets))
        nodes=tree.body[position:position+2]
        self.assertIsInstance(nodes[1],ast.For)
        namespace={'project_root':root,'datas':[]}
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'skill-resources','exec'),namespace)
        resources=namespace['datas']
        self.assertEqual(len(resources),7)
        for source,destination in resources:
            self.assertTrue(Path(source).is_file())
            self.assertIn('/image_remix/skills/',str(source).replace('\\','/'))
            self.assertNotIn('PluginData',destination)
        self.assertIn('SKILL.md',{Path(source).name for source,_ in resources})
