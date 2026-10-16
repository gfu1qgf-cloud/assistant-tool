"""Seek the editor clock without encoding, saving or consuming node pools."""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from qt_compat import QtCore, QtGui, QtWidgets
from PyQt6.QtTest import QTest
from app_plugins.builtin.batch_text_video.preview_timeline import AnimationTimeline, editor_duration
from app_plugins.builtin.batch_text_video.scrolling_text import new_scrolling_component, render_scroll_preview
from app_plugins.builtin.batch_text_video.layers import normalize_layers, DEFAULT_LAYERS, ImageCache
from app_plugins.builtin.batch_text_video.store import DEFAULTS, fresh_state
from app_plugins.builtin.batch_text_video.layout import ensure_fonts, render_preview_overlay
from app_plugins.builtin.batch_text_video.design_preview import prepare_design_bundle, design_layers
from app_plugins.builtin.batch_text_video.node_effects import fallback_registry, new_effect, EffectRuntime
from app_plugins.builtin.batch_text_video.component_nodes import component_graph
from model.node_workflow.library import WorkflowLibrary
from model.node_workflow.graph import new_node
import test_batch_review_regressions as fixture


def scroll_layer(**values):
    layer = new_scrolling_component()
    layer.update(text='FIRST\nSECOND\nTHIRD\nLAST', font='Segoe UI', font_min=70, font_max=70,
                 outline_width=0, shadow_enabled=False, padding=4, x=50, y=50, width=90, height=80,
                 scroll_start_anchor='top', scroll_end_anchor='bottom', scroll_end_offset=-50,
                 scroll_edge_fade=0, **values)
    return normalize_layers([layer])[0]


def settings_for(layer):
    return {**copy.deepcopy(DEFAULTS), 'darkness': 0, 'layers': [layer] +
            [dict(item, enabled=False) for item in DEFAULT_LAYERS]}


class AnimationScrubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        ensure_fonts()

    def test_duration_uses_final_voice_or_fixed_clock_not_background_length(self):
        settings = dict(DEFAULTS, unvoiced_duration_mode='fixed', unvoiced_duration_seconds=15)
        self.assertEqual(editor_duration(settings, 8), 15)
        self.assertEqual(editor_duration(settings, 8, 63.44, has_voice=True), 63.44)
        self.assertIsNone(editor_duration(settings, 8, None, has_voice=True))
        settings['unvoiced_duration_mode'] = 'source'
        self.assertEqual(editor_duration(settings, 8), 8)
        self.assertIsNone(editor_duration(settings))

    def test_bar_jumps_to_exact_end_and_follows_duration_changes(self):
        bar = AnimationTimeline()
        try:
            self.assertFalse(bar.slider.isEnabled())
            bar.set_duration(63.44)
            changed = Mock(); bar.timeChanged.connect(changed)
            bar.end_button.click()
            self.assertEqual(bar.snapshot(), {'duration': 63.44, 'time': 63.44})
            self.assertIn('63.44 / 63.44', bar.time_label.text())
            self.assertEqual(changed.call_count, 1)
            bar.end_button.click()
            self.assertEqual(changed.call_count, 2)
            bar.set_duration(20)
            self.assertEqual(bar.snapshot()['time'], 20)
            self.assertEqual(changed.call_count, 2)
            bar.start_button.click()
            self.assertEqual(bar.snapshot()['time'], 0)
            bar.set_duration(None)
            self.assertFalse(bar.end_button.isEnabled())
        finally:
            bar.deleteLater()

    def test_clicking_timeline_jumps_to_clicked_position(self):
        bar = AnimationTimeline()
        bar.resize(600, 90); bar.set_duration(10); bar.show()
        try:
            self.app.processEvents()
            point = QtCore.QPoint(round(bar.slider.width()*.75), bar.slider.height()//2)
            QTest.mouseClick(bar.slider, QtCore.Qt.MouseButton.LeftButton,
                             QtCore.Qt.KeyboardModifier.NoModifier, point)
            self.assertAlmostEqual(bar.snapshot()['time'], 7.5, delta=.25)
        finally:
            bar.close(); bar.deleteLater()

    def test_scroll_preview_start_middle_end_use_export_position_math(self):
        layer = scroll_layer()
        settings = settings_for(layer)
        before = copy.deepcopy(settings)
        images = []
        for time in (0, 5, 10):
            image, _ = render_preview_overlay('', '', 320, 240,
                {**settings, '_editor_timing': {'duration': 10, 'time': time}})
            expected, _ = render_scroll_preview(layer, 320, 240, duration=10, time=time)
            self.assertEqual(image, expected)
            images.append(image)
        self.assertNotEqual(images[0], images[1])
        self.assertNotEqual(images[1], images[2])
        self.assertEqual(settings, before)

    def test_delay_and_early_finish_hold_correct_frame(self):
        layer = scroll_layer(scroll_start_delay=2, scroll_end_early=2)
        settings = settings_for(layer)
        def image(time):
            return render_preview_overlay('', '', 320, 240,
                {**settings, '_editor_timing': {'duration': 10, 'time': time}})[0]
        self.assertEqual(image(0), image(2))
        self.assertEqual(image(8), image(10))

    def node_setup(self, directory):
        directory = Path(directory)
        source = directory / 'synthetic.mp4'; source.write_bytes(b'synthetic source identity')
        registry = fallback_registry()
        layer = scroll_layer()
        graph, _ = component_graph(layer, DEFAULTS, registry)
        palette = new_node(registry.definitions['core.text_palette'])
        palette['params'].update(family='ocean', seed='fixed-test', scope='fixed')
        graph['nodes'].append(palette)
        text = next(n for n in graph['nodes'] if n['kind'] == 'batch_text_video.text_layer')
        graph['connections'].append([palette['id'], 'text', text['id'], 'color'])
        library = WorkflowLibrary(directory / 'workflows')
        saved = library.save(graph, 'Synthetic scroll effect', registry)
        effect = new_effect(); effect['workflow_id'] = saved['workflow_id']
        job = {'id': 'job', 'name': 'synthetic', 'path': str(source), 'voice_path': '',
               'title': 'Title', 'body': 'Body', 'task_dir': str(directory)}
        state = fresh_state()
        state['jobs'] = [job]
        state['settings'] = settings_for(effect)
        services = SimpleNamespace(available_nodes=lambda: registry, workflow_library=library)
        return state, job, services, registry

    def test_node_scroll_sources_and_palette_match_actual_operation_builders(self):
        with tempfile.TemporaryDirectory() as folder:
            state, job, services, registry = self.node_setup(folder)
            before = copy.deepcopy(state)
            bundle, registry = prepare_design_bundle(state, job, services)
            colours, frames = [], []
            for time in (0, 5, 10):
                settings = {**state['settings'], '_editor_timing': {'time': time, 'duration': 10}}
                expanded, notices = design_layers(settings, job, bundle, registry, 320, 240, folder, threading.Event())
                self.assertFalse(notices)
                text = next(layer for layer in expanded['layers'] if layer['kind'] == 'text')
                self.assertEqual(text['text'], 'FIRST\nSECOND\nTHIRD\nLAST')
                colours.append(text['color'])
                frames.append(render_preview_overlay('', '', 320, 240, expanded)[0])
                self.assertEqual(state, before)
            self.assertEqual(len(set(colours)), 1)
            self.assertNotEqual(frames[0], frames[-1])
            runtime = EffectRuntime(bundle, registry, preview=True)
            info = {'duration': 10, 'audio': False, 'video': {'width': 320, 'height': 240}}
            with runtime.evaluated(job, state['settings']['layers'][0], folder, info, '', '') as (_, _, operations):
                actual = json.loads(dict(operations[0].options)['layer'])
            self.assertEqual(actual['color'], colours[0])
            self.assertEqual(runtime.sequence_plan, [])

    def test_scrubbing_rejects_arbitrary_code_before_any_preparation(self):
        with tempfile.TemporaryDirectory() as folder:
            state, job, services, registry = self.node_setup(folder)
            key = state['settings']['layers'][0]['workflow_id']
            graph = services.workflow_library.load(key)
            graph['nodes'].append(new_node(registry.definitions['core.python_code']))
            services.workflow_library.save(graph, 'Synthetic restricted preview', registry)
            with patch('model.node_workflow.tasks.prepare_job') as prepare:
                with self.assertRaisesRegex(ValueError, '不自动调用'):
                    prepare_design_bundle(state, job, services)
                prepare.assert_not_called()

    def test_disabled_node_text_is_a_valid_empty_canvas(self):
        with tempfile.TemporaryDirectory() as folder:
            state, job, services, registry = self.node_setup(folder)
            key = state['settings']['layers'][0]['workflow_id']
            graph = services.workflow_library.load(key)
            next(n for n in graph['nodes'] if n['kind'] == 'batch_text_video.text_layer')['params']['layer']['enabled'] = False
            services.workflow_library.save(graph, 'Synthetic disabled text', registry)
            bundle, registry = prepare_design_bundle(state, job, services)
            settings = {**state['settings'], '_editor_timing': {'duration': 10, 'time': 10}}
            expanded, notices = design_layers(settings, job, bundle, registry, 320, 240, folder)
            self.assertFalse(notices)
            image, _ = render_preview_overlay('', '', 320, 240, expanded)
            self.assertFalse(image.isNull())

    def test_blocked_effect_keeps_its_explicit_notice_not_a_false_text_fit_error(self):
        effect = new_effect()
        settings = {**settings_for(effect), '_editor_timing': {'duration': 10, 'time': 10},
                    '_design_notice': 'Synthetic preview restriction'}
        expanded, notices = design_layers(settings, {}, None, None, 320, 240, '')
        self.assertEqual(notices, ['Synthetic preview restriction'])
        image, _ = render_preview_overlay('', '', 320, 240, expanded)
        self.assertFalse(image.isNull())

    @unittest.skipUnless(os.environ.get('NODE_FFMPEG'), 'requires real FFmpeg')
    def test_seek_to_end_matches_real_final_node_video_frame(self):
        import test_batch_scrolling_text as helpers
        from app_plugins.builtin.batch_text_video.engine import render_static_text
        helper = helpers.RealScrollTextTests()
        with tempfile.TemporaryDirectory() as folder:
            state, job, services, registry = self.node_setup(folder)
            video = job['path']
            job['name'] = 'rendered-end-check'
            helper.ffmpeg(['-f', 'lavfi', '-i', 'color=c=black:s=240x160:r=20:d=2',
                           '-c:v', 'libx264', '-pix_fmt', 'yuv420p', video])
            state['settings'].update(ffmpeg_path=os.environ['NODE_FFMPEG'], aspect='source',
                auto_crop_black_bars=False, music_enabled=False, overwrite=True)
            key = state['settings']['layers'][0]['workflow_id']
            graph = services.workflow_library.load(key)
            next(n for n in graph['nodes'] if n['kind'] == 'batch_text_video.text_layer')['params']['layer']['scroll_end_early'] = .5
            services.workflow_library.save(graph, 'Synthetic end-frame check', registry)
            bundle, registry = prepare_design_bundle(state, job, services)
            runtime = EffectRuntime(bundle, registry, threading.Event(), preview=True)
            output, _ = render_static_text(job, state, folder, preview=True, node_effects=runtime)
            actual = helper.frame(output, 1.9)
            settings = {**state['settings'], '_editor_timing': {'duration': 2, 'time': 2}}
            expanded, notices = design_layers(settings, job, bundle, registry, 240, 160, folder, threading.Event())
            self.assertFalse(notices)
            overlay, _ = render_preview_overlay('', '', 240, 160, expanded)
            expected = QtGui.QImage(240, 160, QtGui.QImage.Format.Format_RGB32)
            expected.fill(QtGui.QColor('black'))
            painter = QtGui.QPainter(expected); painter.drawImage(0, 0, overlay); painter.end()
            self.assertLess(helper.image_difference(actual, expected), 3)


class AnimationScrubDialogTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BatchReviewRegressions.setUpClass.__func__)
    setUp = fixture.BatchReviewRegressions.setUp
    tearDown = fixture.BatchReviewRegressions.tearDown
    wait = fixture.BatchReviewRegressions.wait

    def test_editor_slider_changes_frame_without_persistence_or_video_render(self):
        dialog = self.dialog
        layer = scroll_layer()
        dialog.layer_panel.set_layers(settings_for(layer)['layers'])
        dialog.controls['unvoiced_duration_mode'].setCurrentIndex(dialog.controls['unvoiced_duration_mode'].findData('fixed'))
        dialog.controls['unvoiced_duration_seconds'].setValue(10)
        dialog.edit_timer.stop()
        dialog.update_preview()
        self.wait(lambda: dialog.overlay_worker is None)
        before = copy.deepcopy(dialog.state)
        initial = dialog.preview.overlay.copy()
        with patch.object(dialog.store, 'save') as save, \
             patch('app_plugins.builtin.batch_text_video.playback.render_static_text') as render:
            dialog.animation_timeline.end_button.click()
            self.wait(lambda: not dialog.animation_preview_timer.isActive() and dialog.overlay_worker is None)
            save.assert_not_called(); render.assert_not_called()
        self.assertEqual(dialog.state, before)
        self.assertNotEqual(dialog.preview.overlay, initial)
        self.assertEqual(dialog._overlay_request[4]['_editor_timing'], {'duration': 10, 'time': 10})
        self.assertNotIn('_editor_timing', dialog.read_settings())
        self.assertFalse(dialog.edit_timer.isActive())
        if os.environ.get('BATCH_SCRUB_QA'):
            dialog.layer_panel.select_layer_id(layer['id'])
            dialog.layer_panel.reveal_property('scroll')
            dialog.grab().save(os.environ['BATCH_SCRUB_QA'])

    def test_seek_drops_obsolete_frame_and_only_keeps_latest_pending_position(self):
        dialog = self.dialog
        dialog.controls['unvoiced_duration_mode'].setCurrentIndex(dialog.controls['unvoiced_duration_mode'].findData('fixed'))
        dialog.edit_timer.stop(); dialog.update_preview()
        self.wait(lambda: dialog.overlay_worker is None)
        old = dialog._overlay_request
        image = QtGui.QImage(1, 1, QtGui.QImage.Format.Format_ARGB32); image.fill(QtGui.QColor('red'))
        dialog.animation_timeline.slider.setValue(2500)
        dialog._overlay_ready({'request': old, 'image': image, 'info': {}})
        self.assertNotEqual(dialog.preview.overlay, image)
        dialog.animation_timeline.end_button.click()
        self.wait(lambda: not dialog.animation_preview_timer.isActive() and dialog.overlay_worker is None)
        timing = dialog._overlay_request[4]['_editor_timing']
        self.assertEqual(timing['time'], timing['duration'])

    def test_returning_from_playback_reveals_editor_clock_without_resetting_end(self):
        dialog = self.dialog
        dialog.animation_timeline.set_duration(15)
        dialog.animation_timeline.end_button.click()
        dialog.animation_preview_timer.stop()
        dialog.animation_timeline.hide(); dialog.playback_timeline.show()
        dialog.return_to_editor()
        self.assertFalse(dialog.animation_timeline.isHidden())
        self.assertTrue(dialog.playback_timeline.isHidden())
        self.assertEqual(dialog.animation_timeline.snapshot()['time'], 15)

    def test_current_node_text_workflow_is_visible_and_preparation_is_cached_between_seeks(self):
        helper = AnimationScrubTests()
        with tempfile.TemporaryDirectory() as folder:
            state, job, services, registry = helper.node_setup(folder)
            dialog = self.dialog
            dialog.context, dialog.state, dialog.current_id = services, state, job['id']
            dialog.title.setPlainText(job['title']); dialog.body.setPlainText(job['body'])
            dialog.layer_panel.set_layers(state['settings']['layers'])
            dialog.controls['unvoiced_duration_mode'].setCurrentIndex(dialog.controls['unvoiced_duration_mode'].findData('fixed'))
            dialog.controls['unvoiced_duration_seconds'].setValue(10)
            dialog.edit_timer.stop()
            before = copy.deepcopy(state)
            with patch('app_plugins.builtin.batch_text_video.design_preview.prepare_design_bundle', wraps=prepare_design_bundle) as prepare:
                dialog.update_preview()
                self.wait(lambda: dialog.overlay_worker is None)
                initial = dialog.preview.overlay.copy()
                dialog.animation_timeline.end_button.click()
                self.wait(lambda: not dialog.animation_preview_timer.isActive() and dialog.overlay_worker is None)
                self.assertEqual(prepare.call_count, 1)
            self.assertNotEqual(dialog.preview.overlay, initial)
            self.assertIn('节点文字', dialog._overlay_status)
            self.assertNotIn('快览未执行', dialog._overlay_status)
            self.assertEqual(dialog.state, before)


if __name__ == '__main__':
    unittest.main()
