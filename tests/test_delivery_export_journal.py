from datetime import date
import errno
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from model.TaskResultExporter import _copy_output, copy_if_updated
from model.TaskResultOrganizer import (run_task_result_organizer, load_pending_changed_file_batches,
                                       save_pending_changed_file_batches)


class ExportJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.day = date(2026, 11, 2)
        self.output = self.root/'1102'/'result'; self.output.mkdir(parents=True)
        (self.root/'1102'/'tasks.ods').touch()
        self.config = {'run_export': True, 'run_upload': True, 'upload_only_changed_files': True,
                       'video_detection_mode': 'ask', 'task_result_pending_file': str(self.root/'pending.json'),
                       'task_table_file_name': 'tasks.ods', 'task_output_name_template': '{task_id}.mp4',
                       'task_export_profiles': {'口播': {'mode': 'single', 'task_dir': '口播', 'candidate_names': ['out.mp4']}}}
        self.tasks = [SimpleNamespace(task_id=str(i), task_type='口播', task_name='safe-title', admin='', creator='', task_date='1102')
                      for i in (340, 341, 342)]
        for task in self.tasks:
            src=self.root/'1102'/'口播'/task.task_id/'out.mp4'; src.parent.mkdir(parents=True)
            src.write_bytes(('synthetic-video-'+task.task_id).encode()); os.utime(src,(time.time()-20,time.time()-20))

    def pending(self):
        return [p for _, files in load_pending_changed_file_batches(self.config,self.root,[self.day]) for p in files]

    def run_interrupted(self, **config):
        calls=[]
        def copy(src,dst):
            if len(calls)==2: raise OSError(errno.ENOSPC,'simulated disk full')
            result=copy_if_updated(src,dst); calls.append(dst); return result
        with patch('model.TaskResultExporter.ReadTaskOds',return_value=self.tasks), \
             patch('model.TaskResultExporter.copy_if_updated',side_effect=copy), \
             patch('model.TaskResultOrganizer.load_drive_service') as drive:
            with self.assertRaisesRegex(OSError,'disk full'):
                run_task_result_organizer([self.day],self.root,{**self.config,**config})
            drive.assert_not_called()

    def test_completed_files_durable_before_later_disk_full(self):
        self.run_interrupted()
        self.assertEqual({p.name for p in self.pending()},{'340.mp4','341.mp4'})
        self.assertFalse((self.output/'342.mp4').exists())

    def test_journal_is_saved_before_next_copy_starts(self):
        seen=[]
        def copy(src,dst):
            if seen:self.assertIn(seen[-1],self.pending())
            result=copy_if_updated(src,dst);seen.append(dst);return result
        with patch('model.TaskResultExporter.ReadTaskOds',return_value=self.tasks), \
             patch('model.TaskResultExporter.copy_if_updated',side_effect=copy):
            result=run_task_result_organizer([self.day],self.root,self.config,detection_mode_resolver=lambda _: 'cancel')
        self.assertTrue(result['cancelled']);self.assertEqual(len(self.pending()),3)

    def test_retry_recovers_copied_but_unchanged_files_without_cloud(self):
        self.run_interrupted();received=[]
        with patch('model.TaskResultExporter.ReadTaskOds',return_value=self.tasks), \
             patch('model.TaskResultOrganizer.load_drive_service') as drive:
            result=run_task_result_organizer([self.day],self.root,self.config,
                detection_mode_resolver=lambda files:received.extend(files) or 'cancel')
        self.assertTrue(result['cancelled']);drive.assert_not_called()
        self.assertEqual({p.name for p in received},{'340.mp4','341.mp4','342.mp4'})

    def test_existing_pending_record_is_not_overwritten(self):
        old=self.output/'older.mp4';old.write_bytes(b'previous')
        save_pending_changed_file_batches(self.config,self.root,[self.day],[(self.output,[old])])
        self.run_interrupted()
        self.assertEqual({p.name for p in self.pending()},{'older.mp4','340.mp4','341.mp4'})

    def test_unchanged_old_outputs_are_not_requeued(self):
        for task in self.tasks:
            copy_if_updated(self.root/'1102'/'口播'/task.task_id/'out.mp4',self.output/(task.task_id+'.mp4'))
        with patch('model.TaskResultExporter.ReadTaskOds',return_value=self.tasks), \
             patch('model.TaskResultOrganizer.pending_review_recovery_records',return_value=[]):
            run_task_result_organizer([self.day],self.root,self.config)
        self.assertEqual(self.pending(),[])

    def test_upload_disabled_does_not_create_upload_intent(self):
        self.run_interrupted(run_upload=False)
        self.assertEqual(self.pending(),[])

    def test_no_update_callback_for_deferred_or_unchanged_copy(self):
        calls=[]
        with patch('model.TaskResultExporter.copy_if_updated',return_value=False):
            self.assertFalse(_copy_output('src','dst',updated_file_callback=calls.append))
        self.assertEqual(calls,[])
        from model.TaskResultFileGuard import FileStillWritingError
        with patch('model.TaskResultExporter.copy_if_updated',side_effect=FileStillWritingError('writing')):
            self.assertFalse(_copy_output('src','dst',updated_file_callback=calls.append))
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
