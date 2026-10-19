import copy
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.store import Store, ProfileStore, fresh_state
from app_plugins.builtin.batch_text_video.background import execute_batch, run_batch_video_job, validate_batch_outputs
from app_plugins.builtin.batch_text_video.node_effects import prepare_bundle, TYPE_ID, new_effect
from app_plugins.builtin.batch_text_video.captions import new_caption, task_word_timings
from app_plugins.builtin.batch_text_video.playback import preview_key, PlaybackWorker
from app_plugins.builtin.batch_text_video.ui import BatchTextVideoDialog
from model.BackgroundTasks import BackgroundTaskManager


class GlobalRenderQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'source.mp4'
        self.source.write_bytes(b'synthetic')
        self.store = Store(self.root/'project')
        state = self.store.load()
        state['settings']['layers'] = []
        state['jobs'] = [self.job('a')]
        self.store.save(state, 0)

    def job(self, identifier, name=None):
        return dict(id=identifier, name=name or identifier, path=str(self.source),
                    title='One', body='two', status='待生成', output='', error='')

    def test_plans_survive_switch_and_per_profile_commits_do_not_replace_active_editor(self):
        state = self.store.load()
        state = self.store.create_profile(state, '第二项目')
        second = state['active_profile']
        state['jobs'] = [self.job('b')]
        state = self.store.save(state, state['revision'])
        state = self.store.select_profile(state, 'default')
        first = self.store.add_render_plan('default', ['a'])
        other = self.store.add_render_plan(second, ['b'])
        self.assertEqual([p['id'] for p in self.store.load()['render_plans']], [first['id'], other['id']])
        render = ProfileStore(self.store.directory, second)
        submitted = render.load()
        self.assertEqual(submitted['jobs'][0]['id'], 'b')
        self.store.update_render_plan(other['id'], 'submitted', 'task-b')
        # The UI can switch and save a different profile while this job waits.
        ui = self.store.load()
        ui['settings']['body_color'] = '#abcdef'
        ui = self.store.save(ui, ui['revision'])
        self.assertEqual(render.load()['revision'], submitted['revision'])
        (self.root/'made.mp4').write_bytes(b'video')
        saved = render.completed(submitted, 'b', self.root/'made.mp4', None)
        self.assertEqual(saved['jobs'][0]['status'], '已完成')
        live = self.store.load()
        self.assertEqual(live['active_profile'], 'default')
        self.assertEqual(live['settings']['body_color'], '#abcdef')
        self.assertEqual(live['jobs'][0]['id'], 'a')
        self.assertEqual(render.load()['jobs'][0]['output'], str(self.root/'made.mp4'))
        self.assertEqual(len(live['render_plans']), 2)

    def test_active_editor_merges_other_profile_completion_but_rejects_own_stale_revision(self):
        state = self.store.create_profile(self.store.load(), '第二项目')
        second = state['active_profile']
        state['jobs'] = [self.job('b')]
        state = self.store.save(state, state['revision'])
        state = self.store.select_profile(state, 'default')
        old_ui = copy.deepcopy(state)
        renderer = ProfileStore(self.store.directory, second)
        render_state = renderer.load()
        (self.root/'made.mp4').write_bytes(b'video')
        renderer.completed(render_state, 'b', self.root/'made.mp4', None)
        old_ui['settings']['body_color'] = '#123456'
        merged = self.store.save(old_ui, old_ui['revision'])
        self.assertEqual(merged['settings']['body_color'], '#123456')
        self.assertEqual(ProfileStore(self.store.directory, second).load()['jobs'][0]['status'], '已完成')
        with self.assertRaisesRegex(ValueError, '当前配置'):
            self.store.save(old_ui, old_ui['revision'])

    def test_same_batch_output_collision_rejected_before_first_encode(self):
        state = self.store.load()
        state['settings']['overwrite'] = True
        state['jobs'] = [self.job('a', 'same'), self.job('b', 'same')]
        state = self.store.save(state, state['revision'])
        invoked = []
        def render(*args, **kwargs):
            invoked.append(True)
            return self.root/'same.mp4', None
        result, _ = execute_batch(self.store, state, ['a', 'b'], threading.Event(),
            lambda *_: None, lambda *_: None, lambda *_: None,
            renderers={'static_text': render})
        self.assertEqual(invoked, [])
        self.assertEqual(result['completed'], 0)
        self.assertIn('同一成品', result['error'])

    def test_case_equivalent_names_conflict_independent_of_overwrite_switch(self):
        state = self.store.load()
        state['jobs'] = [self.job('a', 'Same'), self.job('b', 'same')]
        for overwrite in (False, True):
            state['settings']['overwrite'] = overwrite
            with self.assertRaisesRegex(ValueError, '同一成品'):
                validate_batch_outputs(state, ['a','b'])
        validate_batch_outputs(state, ['a'])

    def test_selected_node_iterations_follow_submission_order(self):
        from dataclasses import asdict
        from unittest.mock import patch
        from app_plugins.builtin.batch_text_video import node_effects as fx
        state = self.store.load()
        state['jobs'] = [self.job(str(i)) for i in range(1, 6)]
        effect = new_effect(); effect['workflow_id'] = 'a'*32
        state['settings']['layers'] = [effect]
        services = SimpleNamespace(available_nodes=lambda: object(),
            workflow_library=SimpleNamespace(load=lambda *_: {'workflow_type': TYPE_ID}))
        with patch('model.node_workflow.tasks.prepare_job', side_effect=lambda *a, global_data, **k:
                   ({'global_data': asdict(global_data)}, ())):
            bundle, _, _ = prepare_bundle(state, ['4', '2'], services)
        self.assertEqual([(k, next(iter(v.values()))['global_data']['iteration'])
                         for k,v in bundle['jobs'].items()], [('4',1), ('2',2)])
        self.assertTrue(all(next(iter(v.values()))['global_data']['iteration_count'] == 2
                            for v in bundle['jobs'].values()))

    def test_preview_uses_staged_plan_iteration_for_that_job(self):
        from dataclasses import asdict
        from app_plugins.builtin.batch_text_video import node_effects as fx
        state = self.store.load()
        state['jobs'] = [self.job(str(i)) for i in range(1, 6)]
        effect = new_effect(); effect['workflow_id'] = 'a'*32
        state['settings']['layers'] = [effect]
        state['render_plans'] = [{'profile_id':'default','status':'staged','ids':['4','2']}]
        services = SimpleNamespace(available_nodes=lambda: object(),
            workflow_library=SimpleNamespace(load=lambda *_: {'workflow_type': TYPE_ID}))
        with patch('model.node_workflow.tasks.prepare_job', side_effect=lambda *a, global_data, **k:
                   ({'global_data': asdict(global_data)}, ())):
            bundle, _ = fx.prepare_preview_bundle(state, state['jobs'][1], services)
        value = next(iter(bundle['jobs']['2'].values()))['global_data']
        self.assertEqual((value['iteration'],value['iteration_count']), (2,2))

    def test_dynamic_only_task_can_build_preview_key_without_ordinary_srt(self):
        voice = self.root/'task_audio.wav'; voice.write_bytes(b'audio')
        (self.root/'task_audio[动态].json').write_text('{}')
        layer = new_caption(); layer['caption_timing'] = 'task'
        state = self.store.load(); state['settings']['layers'] = [layer]; state['settings']['use_voice'] = True
        job = self.job('a'); job.update(voice_path=str(voice), task_voice_path=str(voice))
        words = [{'text':'One','start':0.,'end':.5},{'text':'two','start':.5,'end':1.}]
        with patch('model.ElevenLabsTimings.load_timings', return_value=words):
            self.assertEqual(task_word_timings(job, str(voice), layer, 'One','two',1.,self.root), words)
        self.assertEqual(len(preview_key(job, state)), 64)

        # Playback must also pass its source-freezing stage, before rendering.
        job.update(voice_override=True, task_dir=str(self.root))
        worker = PlaybackWorker(job, state, self.root, 'e'*64)
        with patch('app_plugins.builtin.batch_text_video.playback.resolve_tools', return_value=('ffmpeg','ffprobe')), \
             patch('app_plugins.builtin.batch_text_video.playback.probe', return_value={'duration':1.}), \
             patch('app_plugins.builtin.batch_text_video.playback.render_static_text',
                   return_value=(self.root/'preview.mp4',None)) as render:
            worker.run()
        self.assertTrue(render.called)
        self.assertEqual(render.call_args.args[0]['caption_subtitles'], {})

    def test_worker_reports_stable_batch_total_and_individual_items(self):
        state = ProfileStore(self.store.directory, 'default').load()
        events, items, stages = [], [], []
        context = SimpleNamespace(is_canceled=lambda: False,
            report=lambda message, value: events.append((message,value)),
            report_item=lambda identifier, value: items.append((identifier,value)), log=lambda *_:None)
        context.report_item_stage = lambda identifier, stage: stages.append((identifier, stage))
        def fake_batch(_store, _state, ids, _token, progress, _message, done, **_kwargs):
            progress.stage(ids[0], '等待字幕模型')
            progress(ids[0], 30); progress(ids[0], 80); done(ids[0], '已完成')
            return {'completed':1,'failed':0,'cancelled':False,'paths':[]}, _state
        with patch('app_plugins.builtin.batch_text_video.background.execute_batch', side_effect=fake_batch):
            run_batch_video_job({'store_dir':str(self.store.directory), 'profile_id':'default',
                                 'state':state,'ids':['a']}, context)
        self.assertEqual([v for _,v in items], [30,80,100])
        self.assertEqual([v for _,v in events], [30,80,100])
        self.assertTrue(all('第 1/' not in message for message,_ in events))
        self.assertEqual(stages, [('a','等待字幕模型'), ('a','已完成')])

    def test_dialog_can_stage_two_profiles_submit_and_switch_while_queued(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        state = self.store.create_profile(self.store.load(), '第二项目')
        other = state['active_profile']
        state['jobs'] = [self.job('b')]
        state = self.store.save(state, state['revision'])
        state = self.store.create_profile(state, '第三项目')
        third = state['active_profile']
        self.store.select_profile(state, 'default')
        manager = BackgroundTaskManager(history_path=self.root/'history.json')
        context = SimpleNamespace(
            submit_background_task=lambda **kwargs: manager.submit(owner='batch_text_video', **kwargs),
            log=lambda *_a, **_k: None)
        with patch.object(manager, '_dispatch', return_value=None), \
             patch.object(BatchTextVideoDialog, 'request_background'):
            dialog = BatchTextVideoDialog(context=context, store=self.store)
            dialog.start_batch(False)
            self.assertEqual(len(self.store.load()['render_plans']), 1)
            dialog.switch_profile(other)
            self.assertEqual(dialog.state['active_profile'], other)
            dialog.start_batch(False)
            self.assertEqual(len(self.store.load()['render_plans']), 2)
            dialog.start_staged_plans()
            self.assertEqual(len(dialog._host_plans), 2)
            self.assertEqual(len(manager.records()), 2)
            self.assertTrue(all(row['status'] == 'queued' for row in manager.records()))
            self.assertFalse(dialog.left_column.isEnabled())
            self.assertTrue(dialog.render_queue_panel.isEnabled())
            self.assertFalse(dialog.task_text_panel.isEnabled())
            self.assertTrue(dialog.menu_bar.isEnabled())
            dialog.switch_profile('default')
            self.assertEqual(dialog.state['active_profile'], 'default')
            self.assertFalse(dialog.left_column.isEnabled())
            manager._records[dialog._host_plans['default'].task_id]['status'] = 'running'
            manager._on_item_progress(dialog._host_plans['default'].task_id, 'a', 35)
            from app_plugins.builtin.batch_text_video.row_progress import PROGRESS_ROLE
            self.assertEqual(dialog.jobs.item(0, 0).data(PROGRESS_ROLE), 35)
            self.assertEqual(dialog.jobs.item(0, 2).text(), '35%')
            self.assertIsNone(dialog.jobs.cellWidget(0, 2))
            manager._on_item_stage(dialog._host_plans['default'].task_id, 'a', '等待字幕模型')
            self.assertEqual(dialog.jobs.item(0, 1).text(), '等待字幕模型')
            self.assertIn('等待字幕模型', dialog.render_queue_panel.table.item(0, 1).text())
            manager._on_item_progress(dialog._host_plans['default'].task_id, 'a', 36)
            self.assertEqual(dialog.jobs.item(0, 1).text(), '等待字幕模型')
            dialog.switch_profile(third)
            self.assertTrue(dialog.left_column.isEnabled())
            self.assertTrue(dialog.task_text_panel.isEnabled())
            for handle in list(dialog._host_plans.values()):
                manager._records[handle.task_id]['status'] = 'queued'
                manager.cancel(handle.task_id)
            self.assertFalse(dialog._host_plans)
            dialog.close()
            app.processEvents()

    def test_submit_failure_keeps_staged_plan_and_shows_reason(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        context = SimpleNamespace(submit_background_task=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError('密盘空间不足')),
                                  log=lambda *_a, **_k: None)
        with patch.object(BatchTextVideoDialog, 'request_background'):
            dialog = BatchTextVideoDialog(context=context, store=self.store)
            dialog.start_batch(False)
            dialog.start_staged_plans()
            self.assertIn('密盘空间不足', dialog.status.text())
            self.assertFalse(dialog._host_plans)
            self.assertTrue(dialog.jobs.isEnabled())
            self.assertFalse(hasattr(dialog,'generate'))
            self.assertEqual(self.store.load()['render_plans'][0]['status'], 'staged')
            dialog.close()
            app.processEvents()

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG') and Path(os.environ.get('NODE_FFMPEG','')).is_file(),
                         '需要本地FFmpeg短片')
    def test_real_short_export_commits_only_its_own_profile(self):
        ffmpeg = os.environ['NODE_FFMPEG']
        subprocess.run([ffmpeg, '-y', '-nostdin', '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'color=c=blue:s=320x568:r=25:d=1',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(self.source)],
                       check=True, timeout=45, capture_output=True)
        state = self.store.load()
        state['settings'].update(ffmpeg_path=ffmpeg, output_dir=str(self.root/'out'),
                                 music_enabled=False, overwrite=True)
        state['jobs'] = [self.job('a', 'made')]
        state = self.store.save(state, state['revision'])
        state = self.store.create_profile(state, '第二项目')
        state['jobs'] = [self.job('b')]
        state = self.store.save(state, state['revision'])
        self.store.select_profile(state, 'default')
        profile = ProfileStore(self.store.directory, 'default')
        input_state = profile.load()
        reports, items = [], []
        context = SimpleNamespace(is_canceled=lambda:False,
            report=lambda message, value: reports.append((message,value)),
            report_item=lambda identifier, value: items.append((identifier,value)),
            log=lambda *_:None)
        result = run_batch_video_job({'store_dir':str(self.store.directory), 'profile_id':'default',
                                      'state':input_state, 'ids':['a']}, context)
        self.assertEqual(result['completed'], 1)
        self.assertTrue(Path(result['paths'][0]).is_file())
        self.assertEqual(profile.load()['jobs'][0]['status'], '已完成')
        self.assertEqual(ProfileStore(self.store.directory, state['active_profile']).load()['jobs'][0]['status'], '待生成')
        self.assertEqual(items[-1], ('a',100))


if __name__ == '__main__': unittest.main()
