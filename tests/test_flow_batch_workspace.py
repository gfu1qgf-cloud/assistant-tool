import copy
import unittest
from unittest.mock import patch
from qt_compat import QtCore,QtGui,QtWidgets
import test_flow_setup as fixtures
from test_flow_prompt_segments import SAMPLE
from app_plugins.builtin.flow_assistant.prompt_segments import parse_segments
from app_plugins.builtin.flow_assistant.batch_workspace import INDEX,signature
from app_plugins.builtin.flow_assistant.prompt_import_ui import PromptImportDialog


class WorkspaceTests(unittest.TestCase):
    setUpClass=fixtures.SetupTests.__dict__['setUpClass']
    setUp=fixtures.SetupTests.setUp
    ui=fixtures.SetupTests.ui

    def workspace(self):
        image=QtGui.QImage(160,240,QtGui.QImage.Format.Format_RGB32);image.fill(QtGui.QColor('#87adad'));image.save(str(self.image))
        dialog,launcher,commands=self.ui();dialog.set_targets([self.args['target']])
        return dialog,dialog.batch,commands

    def test_one_page_manual_add_is_staged_and_each_row_edits_the_same_editor(self):
        dialog,batch,commands=self.workspace();manual=dialog.manual
        self.assertEqual([dialog.tabs.tabText(i) for i in range(dialog.tabs.count())].count('添加与统一提交'),1)
        self.assertNotIn('手动逐条提交',[dialog.tabs.tabText(i) for i in range(dialog.tabs.count())])
        manual.prompt.setPlainText('台词：first');manual.enqueue()
        manual.prompt.setPlainText('台词：second');manual.enqueue()
        self.assertEqual(len(batch.rows),2)
        self.assertFalse(any(c['action']=='enqueue' for c in commands),'manual addition must not generate or enqueue before final submit')
        batch.compose.setCurrentCell(0,0);self.assertEqual(manual.prompt.toPlainText(),'台词：first')
        manual.prompt.setPlainText('台词：edited');manual.options.duration.setCurrentIndex(manual.options.duration.findData(8))
        self.assertEqual(batch.rows[0]['prompt'],'台词：edited');self.assertEqual(batch.rows[0]['options']['duration'],8)
        self.assertEqual(batch.rows[1]['prompt'],'台词：second');self.assertEqual(batch.rows[1]['options']['duration'],10)
        self.assertIs(dialog.tabs.currentWidget(),batch)

    def test_import_uses_one_picture_default_ten_seconds_and_all_generation_controls(self):
        dialog,batch,_=self.workspace();manual=dialog.manual
        manual.options.aspect.setCurrentText('16:9');manual.options.resolution.setCurrentText('360p');manual.options.count.setValue(2);manual.options.budget.setValue(19)
        batch.import_segments(parse_segments(SAMPLE),self.args['target'],str(self.image))
        self.assertEqual(len(batch.rows),3)
        for row in batch.rows:
            self.assertEqual(row['image'],str(self.image));self.assertEqual(row['options'],{'duration':10,'aspect':'16:9','resolution':'360p','count':2,'budget':19})
        self.assertIn('标注8秒',batch.rows[0]['warning'])
        batch.compose.setCurrentCell(2,0);self.assertIn('Nie je to teória,',manual.prompt.toPlainText())
        self.assertIn('Nie je to teória,',manual.speech_review.text.toPlainText())

    def test_final_submission_waits_for_matching_receipt_and_preserves_full_prompts(self):
        dialog,batch,commands=self.workspace();batch.import_segments(parse_segments(SAMPLE),self.args['target'],str(self.image))
        batch.enqueue();request=next(c for c in reversed(commands) if c['action']=='enqueue');before=len(commands)
        self.assertFalse(request['collect_only']);self.assertTrue(request['start_after_save']);self.assertEqual(len(request['items']),3)
        self.assertFalse(any(c['action']=='submit_selected' for c in commands))
        batch.acknowledged({'request_id':'stale','count':3,'ids':['a','b','c']});self.assertEqual(len(commands),before)
        batch.acknowledged({'request_id':request['request_id'],'count':3,'ids':['a','b','c']})
        self.assertFalse(any(c['action']=='submit_selected' for c in commands),'backend must finalize without a second UI command')
        self.assertEqual(batch.compose.rowCount(),0);self.assertFalse(batch.rows)
        self.assertIn('Tri proroctvá',dialog.manual.prompt.toPlainText())
        self.assertEqual(dialog.manual.prompt.property('flowSubmissionStage'),'queue')
        count=sum(c['action']=='enqueue' for c in commands);batch.enqueue();self.assertEqual(sum(c['action']=='enqueue' for c in commands),count)

    def test_saved_only_never_releases_even_if_global_collection_was_switched_off(self):
        dialog,batch,commands=self.workspace();batch.add_image(self.args['target'],self.image);batch.save_only.setChecked(True)
        dialog.manual.collect_only.setChecked(False);batch.enqueue();request=next(c for c in reversed(commands) if c['action']=='enqueue')
        self.assertTrue(request['collect_only']);self.assertFalse(request['start_after_save'])
        batch.acknowledged({'request_id':request['request_id'],'count':1,'ids':['one']})
        self.assertFalse(any(c['action']=='submit_selected' for c in commands))

    def test_draft_restores_all_segments_and_pending_result_is_looked_up_without_replay(self):
        dialog,batch,commands=self.workspace();batch.import_segments(parse_segments(SAMPLE),self.args['target'],str(self.image))
        batch.enqueue();request=next(c for c in reversed(commands) if c['action']=='enqueue')
        drafts={c['key']:copy.deepcopy(c['value']) for c in commands if c['action']=='draft_save'}
        self.assertIn(INDEX,drafts)
        other,restored,other_commands=self.workspace();restored.apply_state({'drafts':drafts,'jobs':[],'preferences':{}})
        self.assertEqual([r['prompt'] for r in restored.rows],[r['prompt'] for r in batch.rows])
        restored.enqueue();self.assertEqual(other_commands[-1],{'action':'submission_lookup','request_id':request['request_id'],'source':'batch'})
        self.assertFalse(any(c['action']=='enqueue' for c in other_commands))

    def test_deleting_local_entries_and_discarding_never_touch_running_jobs(self):
        dialog,batch,commands=self.workspace();batch.import_segments(parse_segments(SAMPLE),self.args['target'],str(self.image))
        batch.compose.selectRow(1);batch.remove_selected();self.assertEqual(len(batch.rows),2)
        self.assertFalse(any(c['action'] in {'cancel','delete_records'} for c in commands))
        dialog.state={'jobs':[{'id':'held','status':'collected'},{'id':'running','status':'generating'}]}
        batch.discard();self.assertFalse(batch.pending_rows())
        self.assertEqual(next(c for c in reversed(commands) if c['action']=='discard_collected')['ids'],['held'])

    def test_changing_a_row_does_not_reset_the_shared_batch_budget(self):
        dialog,batch,_=self.workspace();batch.import_segments(parse_segments(SAMPLE),self.args['target'],str(self.image))
        dialog.manual.batch_limit.setValue(45);batch.compose.setCurrentCell(2,0)
        self.assertEqual(dialog.manual.batch_limit.value(),45)

    def test_abandoning_an_unconfirmed_submission_never_releases_it_on_late_receipt(self):
        dialog,batch,commands=self.workspace();batch.add_image(self.args['target'],self.image);batch.enqueue()
        request=next(c for c in reversed(commands) if c['action']=='enqueue')
        batch.request_id='';batch.set_busy(False);batch.discard();batch.enqueue()
        batch.acknowledged({'request_id':request['request_id'],'count':1,'ids':['held']})
        self.assertFalse(any(c['action']=='submit_selected' for c in commands))
        self.assertEqual(next(c for c in reversed(commands) if c['action']=='discard_collected')['ids'],['held'])

    def test_import_preview_invalidates_old_result_after_source_changes(self):
        editor=PromptImportDialog('one.png');self.addCleanup(editor.close)
        editor.source.setPlainText(SAMPLE);editor.parse();self.assertEqual(len(editor.segments),3);self.assertTrue(editor.add.isEnabled())
        editor.source.setPlainText('第1段｜10秒');self.assertFalse(editor.add.isEnabled());editor.parse();self.assertFalse(editor.add.isEnabled())

    def test_compact_parameter_popup_keeps_all_shared_controls_available(self):
        dialog,batch,_=self.workspace();dialog.show_flow_page(batch);dialog.show();self.app.processEvents()
        manual=dialog.manual;manual.parameters_button.click();self.app.processEvents()
        self.assertTrue(manual.parameter_popup.isVisible())
        for widget in (manual.options.aspect,manual.options.resolution,manual.options.budget,manual.batch_limit):self.assertTrue(widget.isVisible())
        self.assertTrue(manual.options.duration.isVisible());self.assertTrue(manual.options.count.isVisible())
        manual.parameter_popup.close()

    def test_wide_list_and_editor_are_left_of_a_complete_portrait_reference(self):
        dialog,batch,_=self.workspace();dialog.show_flow_page(batch);dialog.show();self.app.processEvents()
        preview=dialog.manual.picker.preview;table=batch.compose
        self.assertGreater(preview.mapTo(dialog,QtCore.QPoint()).x(),table.mapTo(dialog,QtCore.QPoint()).x()+table.width())
        self.assertGreater(table.width(),preview.width())
        rect=preview.canvas_rect();self.assertAlmostEqual(rect.width()/rect.height(),9/16,delta=.01)
        self.assertEqual(preview.image.size(),QtCore.QSize(160,240),'preview never crops or modifies the original image')
        button=dialog.submit_button;table_pos=table.mapTo(dialog,QtCore.QPoint())
        self.assertGreater(button.mapTo(dialog,QtCore.QPoint()).y(),table_pos.y()+table.height())
        self.assertEqual(button.text(),'提交表格任务')
        self.assertTrue(dialog.manual.collect_only.isHidden());self.assertTrue(dialog.manual.collect_discard.isHidden())
        self.assertEqual(dialog.manual.submit.text(),'追加任务')

    def test_saved_only_resume_releases_just_the_jobs_bound_to_this_table(self):
        dialog,batch,commands=self.workspace();row=batch.add_image(self.args['target'],self.image)
        batch.save_only.setChecked(True);batch.enqueue();request=batch.request_id
        batch.acknowledged({'request_id':request,'count':1,'ids':['ours'],'row_receipts':{row['id']:['ours']}})
        dialog.state={'jobs':[{'id':'ours','status':'collected'},{'id':'other','status':'collected'}]}
        batch.save_only.setChecked(False);batch.enqueue()
        release=commands[-1]
        self.assertEqual(release['action'],'submit_selected');self.assertEqual(release['ids'],['ours'])
        self.assertEqual(len(batch.rows),1,'keep the table until release is acknowledged')
        batch.acknowledged({'request_id':release['request_id'],'count':1,'ids':['ours'],'collected':False})
        self.assertFalse(batch.rows)

    def test_append_save_edit_and_final_receipt_mark_exact_editor_content_red(self):
        dialog,batch,commands=self.workspace();manual=dialog.manual
        manual.prompt.setPlainText('台词：check this text');manual.enqueue()
        self.assertTrue(manual.prompt.property('flowSubmitted'));self.assertEqual(manual.prompt.property('flowSubmissionStage'),'table')
        self.assertIn('已加入表格',manual.prompt.toolTip());self.assertIn('#dc2626',manual.prompt.styleSheet())
        batch.compose.setCurrentCell(0,0);manual.prompt.setPlainText('台词：changed')
        self.assertFalse(manual.prompt.property('flowSubmitted'));manual.enqueue()
        self.assertTrue(manual.prompt.property('flowSubmitted'))
        manual.options.duration.setCurrentIndex(manual.options.duration.findData(8))
        self.assertFalse(manual.prompt.property('flowSubmitted'));manual.enqueue()
        self.assertTrue(manual.prompt.property('flowSubmitted'))
        batch.enqueue();request=batch.request_id
        batch.acknowledged({'request_id':request,'count':1,'ids':['ours'],'collected':False})
        self.assertFalse(batch.rows);self.assertEqual(manual.prompt.toPlainText(),'台词：changed')
        self.assertTrue(manual.prompt.property('flowSubmitted'));self.assertEqual(manual.prompt.property('flowSubmissionStage'),'queue')
        manual.prompt.setPlainText('another task');self.assertFalse(manual.prompt.property('flowSubmitted'))

    def test_failure_preserves_table_and_does_not_claim_queue_submission(self):
        dialog,batch,commands=self.workspace();dialog.manual.prompt.setPlainText('keep on failure');dialog.manual.enqueue()
        batch.enqueue();batch.failed({'request_id':batch.request_id,'error':'disk not writable'})
        self.assertEqual(len(batch.rows),1);self.assertEqual(dialog.manual.prompt.property('flowSubmissionStage'),'table')
        self.assertIn('disk not writable',batch.status.text())

    def test_old_pending_collected_save_waits_for_release_receipt_before_clearing(self):
        dialog,batch,commands=self.workspace();batch.add_image(self.args['target'],self.image);batch.enqueue();request=batch.request_id
        batch.acknowledged({'request_id':request,'count':1,'ids':['ours'],'collected':True})
        self.assertEqual(commands[-1]['action'],'submit_selected');self.assertEqual(len(batch.rows),1)
        self.assertEqual(batch.pending['phase'],'release')
        batch.acknowledged({'request_id':request,'count':1,'ids':['ours'],'collected':False})
        self.assertFalse(batch.rows)

    def test_success_clears_only_exact_confirmed_snapshots_and_persists_empty_index(self):
        dialog,batch,commands=self.workspace();row=batch.add_image(self.args['target'],self.image);batch.enqueue();request=batch.request_id
        batch.add_entry(self.args['target'],str(self.image),'new entry while response was pending')
        batch.acknowledged({'request_id':request,'count':1,'ids':['ours'],'collected':False})
        self.assertEqual(len(batch.rows),1);self.assertEqual(batch.rows[0]['prompt'],'new entry while response was pending')
        batch.rows.clear();batch.compose.setRowCount(0);batch.changed();batch.flush()
        index=next(c['value'] for c in reversed(commands) if c['action']=='draft_save' and c['key']==INDEX)
        self.assertFalse(index['rows']);self.assertIsNone(index['pending'])

    def test_final_backend_submission_completes_even_if_dialog_never_receives_receipt(self):
        service=fixtures.SetupTests.service(self)
        service.commit_preferences({**service.preferences,'collect_before_submit':True})
        dialog,batch,commands=self.workspace();dialog.manual.prompt.setPlainText('submit once without a live dialog');dialog.manual.enqueue()
        batch.enqueue();request=next(c for c in reversed(commands) if c['action']=='enqueue')
        service.execute(request)
        rows=service.store.rows();self.assertEqual([r['status'] for r in rows],['queued'])
        self.assertEqual(service.released,{rows[0]['id']});self.assertEqual(len(batch.rows),1,'UI has deliberately not received its receipt')
        service.execute(request);self.assertEqual(len(service.store.rows()),1,'idempotent recovery never enqueues a second paid task')

    def test_removing_a_staged_row_clears_its_editor_marker(self):
        dialog,batch,_=self.workspace();dialog.manual.prompt.setPlainText('remove this');dialog.manual.enqueue()
        self.assertTrue(dialog.manual.prompt.property('flowSubmitted'))
        batch.compose.selectRow(0);batch.remove_selected();self.assertFalse(dialog.manual.prompt.property('flowSubmitted'))

    def test_real_buttons_add_only_a_draft_then_final_submit_queues_exactly_once(self):
        service=fixtures.SetupTests.service(self)
        service.commit_preferences({**service.preferences,'collect_before_submit':True})
        dialog,batch,commands=self.workspace();dialog.plugin.command.connect(service.execute)
        service.submitted.connect(dialog.plugin._submitted);service.submission_failed.connect(dialog.plugin._submission_failed)
        dialog.manual.prompt.setPlainText('actual button workflow');dialog.manual.submit.click()
        self.assertEqual(len(batch.rows),1);self.assertFalse(service.store.rows())
        self.assertTrue(dialog.manual.prompt.property('flowSubmitted'))
        dialog.manual.collect_submit.click()
        rows=service.store.rows();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['status'],'queued')
        self.assertFalse(batch.rows);self.assertTrue(dialog.manual.prompt.property('flowSubmitted'))
        self.assertEqual(dialog.manual.prompt.property('flowSubmissionStage'),'queue')
        self.assertFalse(dialog.manual.collect_submit.isEnabled())
        dialog.manual.collect_submit.click();self.assertEqual(len(service.store.rows()),1)
        self.assertEqual(sum(c['action']=='enqueue' for c in commands),1)

    def test_mixed_saved_and_new_rows_release_only_this_table_without_second_ui_command(self):
        service=fixtures.SetupTests.service(self);dialog,batch,commands=self.workspace()
        service.execute({'action':'enqueue','request_id':'unrelated','collect_only':True,'items':[self.args]})
        other=service.store.rows()[0]['id']
        dialog.plugin.command.connect(service.execute);service.submitted.connect(dialog.plugin._submitted)
        dialog.manual.prompt.setPlainText('first held');dialog.manual.enqueue();batch.save_only.setChecked(True);batch.enqueue()
        first=service.store.rows()[-1]['id'];dialog.state={'jobs':service.store.rows()}
        dialog.manual.prompt.setPlainText('second queued');dialog.manual.enqueue();batch.save_only.setChecked(False);batch.enqueue()
        rows=service.store.rows();self.assertEqual(rows[0]['id'],other)
        self.assertEqual([r['status'] for r in rows],['collected','queued','queued'])
        self.assertEqual(service.released,{r['id'] for r in rows[1:]});self.assertFalse(batch.rows)
        self.assertFalse(any(c['action']=='submit_selected' for c in commands))
        self.assertIn('已提交2条',batch.status.text())

    def test_restore_recovers_a_lost_success_receipt_without_replaying_enqueue(self):
        service=fixtures.SetupTests.service(self);dialog,batch,commands=self.workspace()
        dialog.manual.prompt.setPlainText('lost receipt recovery');dialog.manual.enqueue();batch.enqueue()
        request=next(c for c in reversed(commands) if c['action']=='enqueue');service.execute(request)
        drafts={c['key']:copy.deepcopy(c['value']) for c in commands if c['action']=='draft_save'}
        other,restored,other_commands=self.workspace()
        other.plugin.command.connect(service.execute);service.submitted.connect(other.plugin._submitted)
        restored.apply_state({'drafts':drafts,'jobs':service.store.rows(),'preferences':{}})
        self.app.processEvents()
        self.assertFalse(restored.rows);self.assertEqual(len(service.store.rows()),1)
        self.assertTrue(any(c['action']=='submission_lookup' for c in other_commands))
        self.assertFalse(any(c['action']=='enqueue' for c in other_commands))

    def test_table_submit_preserves_safety_pause_and_unrelated_collected_work(self):
        service=fixtures.SetupTests.service(self);dialog,batch,commands=self.workspace()
        service.safety_paused=True;batch.add_image(self.args['target'],self.image);batch.enqueue()
        request=next(c for c in reversed(commands) if c['action']=='enqueue');receipts=[];service.submitted.connect(receipts.append)
        service.execute(request);self.assertEqual(service.store.rows()[0]['status'],'queued')
        self.assertFalse(service.released);self.assertTrue(service.safety_paused);self.assertTrue(receipts[-1]['held'])
        batch.acknowledged(receipts[-1]);self.assertFalse(batch.rows);self.assertIn('安全暂停',batch.status.text())

    def test_late_first_snapshot_merges_prior_list_without_replacing_new_editor_text(self):
        prior,first,commands=self.workspace();first.add_image(self.args['target'],self.image);first.flush()
        drafts={c['key']:copy.deepcopy(c['value']) for c in commands if c['action']=='draft_save'}
        other,current,_=self.workspace();current.add_entry(self.args['target'],str(self.image),'new current draft')
        other.manual.prompt.setPlainText('still typing this')
        current.apply_state({'drafts':drafts,'jobs':[],'preferences':{}})
        self.assertEqual(len(current.rows),2);self.assertEqual(other.manual.prompt.toPlainText(),'still typing this')


if __name__=='__main__':unittest.main()
