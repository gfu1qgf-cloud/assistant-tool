import tempfile
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import patch
from app_plugins.builtin.task_audio_subtitle import TaskAudioSubtitlePlugin,run_task_audio_job
from model.BackgroundTasks import TaskRunContext


class AudioQuietTests(unittest.TestCase):
    def test_thousand_normal_skips_emit_no_per_item_log_and_create_no_empty_folders(self):
        with tempfile.TemporaryDirectory() as root:
            plugin=TaskAudioSubtitlePlugin()
            jobs=[dict(label=str(i),target_dir=str(Path(root)/str(i)),
                task=SimpleNamespace(task_audio_type='',task_audio_text='text')) for i in range(1000)]
            messages=[]; positions=[]
            result=plugin._execute_jobs('audio',jobs,{},lambda *args:messages.append(args),
                item_progress=lambda i,total:positions.append((i,total)))
            self.assertEqual(messages,[])
            self.assertEqual(result['skipped_no_voice'],1000)
            self.assertEqual(positions[-1],(1000,1000))
            self.assertEqual(list(Path(root).iterdir()),[])

    def test_existing_audio_and_subtitle_are_silent_but_errors_still_log(self):
        with tempfile.TemporaryDirectory() as root:
            audio=Path(root)/'task_audio.mp3'; audio.write_bytes(b'existing')
            job=dict(target_dir=root,task=SimpleNamespace(task_audio_type='voice',task_audio_text='text'))
            plugin=TaskAudioSubtitlePlugin(); messages=[]
            with patch.object(plugin,'_create_subtitle',return_value=('existing',audio.with_suffix('.srt'))):
                result=plugin._execute_jobs('audio',[job],{'with_subtitles':True},lambda *args:messages.append(args))
            self.assertEqual(messages,[]); self.assertEqual(result['subtitle_existing'],1)
            audio.unlink()
            with patch.object(plugin,'_create_audio',side_effect=RuntimeError('API failed')):
                result=plugin._execute_jobs('audio',[job],{},lambda *args:messages.append(args))
            self.assertEqual(result['failed'],1)
            self.assertTrue(any('API failed' in args[0] for args in messages))

    def test_background_skip_progress_is_throttled_and_only_one_summary_is_logged(self):
        with tempfile.TemporaryDirectory() as root:
            logs=[]; reports=[]
            context=TaskRunContext(threading.Event(),lambda *args:reports.append(args),logs.append)
            payload=dict(config={},config_path=str(Path(root)/'config.json'),audio_settings={},key_index=0,
                operation='audio',options={},model_name='unused',jobs=[dict(target_dir=str(Path(root)/str(i)),
                    task=dict(task_audio_type='',task_audio_text='text')) for i in range(1000)])
            result=run_task_audio_job(payload,context)
            self.assertEqual(result['skipped_no_voice'],1000)
            self.assertEqual(logs,[result['summary']])
            self.assertLessEqual(len(reports),101); self.assertEqual(reports[-1][1],100)
