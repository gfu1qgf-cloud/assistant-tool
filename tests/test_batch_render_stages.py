import unittest
from qt_compat import QtWidgets
from app_plugins.builtin.batch_text_video.render_progress import bind, observe_report, stage, summary
from app_plugins.builtin.batch_text_video.row_progress import set_row_progress, PROGRESS_ROLE


class RenderStagesTests(unittest.TestCase):
    def test_model_wait_and_alignment_do_not_invent_encoding_progress(self):
        values, phases, messages = [], [], []
        def progress(job_id, value): values.append((job_id,value))
        progress.stage = lambda job_id, text: phases.append((job_id,text))
        bound = bind(progress, 'one')
        report = observe_report(messages.append, bound)
        report('等待字幕模型空闲（其他识别／字幕对齐正在使用）')
        report('字幕模型已就绪，继续识别／对齐')
        self.assertEqual(phases, [('one','等待字幕模型'), ('one','字幕对齐中')])
        self.assertEqual(values, [])
        bound(7)
        self.assertEqual(values, [('one',7)])

    def test_ordinary_progress_callbacks_remain_supported(self):
        values = []
        bound = bind(lambda *v:values.append(v), 'one')
        stage(bound, '准备素材')
        stage(None, '编码中')
        bound(1)
        self.assertEqual(values, [('one',1)])

    def test_project_summary_explains_waits_instead_of_claiming_everything_encodes(self):
        result = summary({'a':'编码中','b':'等待字幕模型','c':'等待字幕模型','d':'已完成'})
        self.assertIn('编码中', result)
        self.assertIn('等待字幕模型 2', result)
        self.assertNotIn('已完成', result)
        self.assertEqual(summary({'a':'等待字幕模型'}), '等待字幕模型')

    def test_small_real_project_progress_is_not_truncated_to_zero(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        table = QtWidgets.QTableWidget(1,3)
        table.setItem(0,0,QtWidgets.QTableWidgetItem('project'))
        set_row_progress(table,0,10/39)
        self.assertEqual(table.item(0,2).text(),'1%')
        self.assertAlmostEqual(table.item(0,0).data(PROGRESS_ROLE),10/39)
        set_row_progress(table,0,35)
        self.assertEqual(table.item(0,2).text(),'35%')

    def test_integer_labels_round_up_without_claiming_completion_early(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        table = QtWidgets.QTableWidget(1,3)
        table.setItem(0,0,QtWidgets.QTableWidgetItem('project'))
        for value, expected in [(None,'—'),(-1,'0%'),(0,'0%'),(.001,'1%'),
                                (1,'1%'),(35.1,'36%'),(99.1,'99%'),(100,'100%'),(101,'100%')]:
            with self.subTest(value=value):
                set_row_progress(table,0,value)
                self.assertEqual(table.item(0,2).text(),expected)
