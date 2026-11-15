"""Cell copy and explicit selected quantity writes; no automatic mode changes."""
import copy
import csv
import io
from pathlib import Path
from qt_compat import QtCore,QtGui,QtWidgets
from model.ClipboardHelper import set_internal_clipboard_text
from model.DailyQuantitySelection import run_selection
from model.DailyQuantityStats import _scope_key
from model.QtThreadLifecycle import thread_is_active


class QuantityOverviewTable(QtWidgets.QTableWidget):
    submit_requested=QtCore.pyqtSignal(object)
    message=QtCore.pyqtSignal(str)

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectItems)
        self.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.submitting=False

    def keyPressEvent(self,event):
        if event.matches(QtGui.QKeySequence.StandardKey.Copy):self.copy_selection();event.accept();return
        super().keyPressEvent(event)

    def selected_quantities(self):
        cells=[];ignored=0
        for index in sorted(self.selectedIndexes(),key=lambda i:(i.row(),i.column())):
            item=self.item(index.row(),index.column());value=item.data(QtCore.Qt.ItemDataRole.UserRole) if item else None
            if isinstance(value,dict):cells.append(copy.deepcopy(value))
            else:ignored+=1
        return {'cells':cells,'ignored':ignored}

    def request_submission(self):
        if self.submitting:return
        request=self.selected_quantities()
        if request['cells']:self.submit_requested.emit(request)
        else:self.message.emit('请选择12点、18点或24点的数量格；合计和文字可以复制，但不直接提交。')

    def copy_selection(self):
        indexes=self.selectedIndexes()
        if not indexes:self.message.emit('请先选择要复制的单元格。');return False
        selected={(i.row(),i.column()) for i in indexes}
        rows=range(min(r for r,c in selected),max(r for r,c in selected)+1)
        cols=range(min(c for r,c in selected),max(c for r,c in selected)+1)
        output=io.StringIO(newline='');writer=csv.writer(output,delimiter='\t',lineterminator='\n')
        def text(r,c):
            item=self.item(r,c)
            value=item.text() if item and (r,c) in selected else ''
            if value.lstrip().startswith(('=','+','-','@')):value="'"+value
            return value
        writer.writerows([[text(r,c) for c in cols] for r in rows])
        set_internal_clipboard_text(output.getvalue().rstrip('\n'))
        self.message.emit(f'已复制 {len(indexes)} 个单元格；不连续选择的空位保留为空白，便于粘贴到表格。')
        return True

    def _menu(self,pos):
        menu=QtWidgets.QMenu(self)
        copy_action=menu.addAction('复制所选\tCtrl+C',self.copy_selection);copy_action.setEnabled(bool(self.selectedIndexes()))
        count=len(self.selected_quantities()['cells'])
        action=menu.addAction(f'提交所选数值到Google表格（{count}格）',self.request_submission)
        action.setEnabled(count>0 and not self.submitting)
        menu.exec(self.viewport().mapToGlobal(pos))


class DailyQuantitySelectionController(QtCore.QObject):
    def __init__(self,plugin,dialog):
        super().__init__(dialog);self.plugin=plugin;self.dialog=dialog;self.handle=None
        self.payload=None;self.pending_plan=None;self.receipt=None
        self.ignored=0
        dialog.selected_write_requested.connect(self.submit)

    def is_busy(self):return self.handle is not None and self.handle.isRunning()

    def _current_scope(self):
        config=self.plugin.context.load_config();root=self.plugin.context.parent_widget.task_path_edit.text().strip()
        return config,root

    def _same_scope(self,config,root):
        current,current_root=self._current_scope()
        return (_scope_key(config,root),str(config.get('task_submission_creator') or '').strip())==(
            _scope_key(current,current_root),str(current.get('task_submission_creator') or '').strip())

    def submit(self,request):
        if self.is_busy():return
        if any(thread_is_active(getattr(self.plugin,name,None)) for name in ('daily_quantity_thread','daily_quantity_folder_thread',
                'daily_quantity_date_thread','daily_quantity_inspection_thread')):
            self.dialog.status.setText('当前统计／清单读取尚未结束，请完成后再提交所选数值。');return
        try:
            config,root=self._current_scope()
            previous=self.plugin.daily_quantity_dialog_scope
            if previous and (_scope_key(previous[0],previous[1]),previous[0].get('task_submission_creator'))!=(
                    _scope_key(config,root),config.get('task_submission_creator')):
                raise ValueError('表格、制作人或任务目录已改变，请重新打开每日数量窗口再提交。')
            if not Path(root).is_dir():raise ValueError('任务根目录不可用，请先核对目录。')
            self.payload={'action':'prepare','config':dict(config),'root':root,'cells':copy.deepcopy(request['cells'])}
            self.ignored=int(request.get('ignored',0))
            self.pending_plan=None;self.receipt=None
            self.dialog.set_selection_busy(True)
            self.dialog.status.setText('正在定位所选日期、本人区块和时段；此阶段只读，不填写。')
            self._submit(self.payload)
        except (OSError,ValueError,RuntimeError) as error:self._failed(str(error));self.dialog.set_selection_busy(False)

    def _submit(self,payload):
        self.handle=self.plugin.context.background_tasks.submit(owner='task_delivery',title='每日数量 · '+
            ('核对所选数值' if payload['action']=='prepare' else '填写所选数值'),runner=run_selection,
            payload=payload,kind='network',dedup_key='daily-quantity-selected:'+str(id(self)),retryable=False)
        self.handle.completed.connect(self._completed);self.handle.failed.connect(self._failed);self.handle.finished.connect(self._finished)

    def _completed(self,result):
        if result['action']=='prepare':
            if self.handle.manager.get(self.handle.task_id)['status']=='succeeded':self.pending_plan=result['plan']
            return
        self.receipt=result
        self.dialog.status.setText(f'所选数值：已核对 {len(result["verified"])} 格，写入 {len(result["updated"])} 格，'
            f'原值相同 {result["unchanged"]} 格，略过 {len(result["skipped"])} 格，待核对 {len(result["unknown"])} 格。自动填写开关未改变。')
        details=['目标表格：'+result['scope'].split('|',1)[0],
                 '本次只操作明确选择的数量格，不代表整天或其他分类已同步。']
        details += [f'{item["range"]}：{item["before"]} → {item["count"]}' for item in result['verified']]
        details += [f'未填写：{item["sheet"]} / {item["date"]} / {item["category"]} / {item["slot"]}：{item["reason"]}' for item in result['skipped']]
        details += [f'需核对：{item["range"]} → {item["count"]}（未自动重发）' for item in result['unknown']]
        details += result['warnings'];self.dialog.details.setPlainText('\n'.join(details))

    def _failed(self,message):
        if self.receipt:
            self.dialog.status.setText(self.dialog.status.text()+' 取消不会回滚已发出的写入；以详情核对结果为准。')
        else:self.dialog.status.setText('所选数值未完成提交：'+message)

    def _finished(self):
        if self.pending_plan is None:self.dialog.set_selection_busy(False);return
        plan=self.pending_plan;self.pending_plan=None
        try:
            if not self._same_scope(self.payload['config'],self.payload['root']):raise ValueError('表格、制作人或任务目录已改变，请重新提交。')
            cells=plan['cells']
            if not cells:
                self.dialog.details.setPlainText('\n'.join(item['reason'] for item in plan['skipped']))
                self.dialog.status.setText('所选数值没有可安全定位的目标格，未填写；详情见设置与记录。')
                self.dialog.set_selection_busy(False);return
            lines=[f'{item["date"]} · {item["sheet"]} · {item["category"]} · '+
                {'01':'12点','02':'18点','03':'24点'}[item['slot']]+f'：{item["before"]} → {item["count"]}' for item in cells]
            text=f'目标表格：{self.payload["config"].get("daily_quantity_sheet_url","")}\n制作人：{plan["creator"]}\n将核对／填写所选 {len(cells)} 格，覆盖为以下数值，不累加：\n\n'+'\n'.join(lines[:16])
            if len(lines)>16:text+=f'\n另有 {len(lines)-16} 格，完整清单可展开详细信息。'
            text+=f'\n\n无法定位或有公式的 {len(plan["skipped"])} 格将略过。只写数字，不改样式、下拉框或合计公式；不会开启自动填写。'
            if self.ignored:text+=f'\n另有 {self.ignored} 个合计／文字格未纳入提交。'
            box=QtWidgets.QMessageBox(self.dialog);box.setWindowTitle('确认提交所选数值')
            box.setIcon(QtWidgets.QMessageBox.Icon.Question);box.setText(text)
            box.setDetailedText('\n'.join(f'{item["range"]} · {line}' for item,line in zip(cells,lines))+
                '\n'+'\n'.join(item['reason'] for item in plan['skipped']))
            box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes|QtWidgets.QMessageBox.StandardButton.No)
            box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.No)
            if box.exec()!=QtWidgets.QMessageBox.StandardButton.Yes:
                self.dialog.status.setText('已取消所选数值提交；未写入云端。');self.dialog.set_selection_busy(False);return
            if not self._same_scope(self.payload['config'],self.payload['root']):raise ValueError('确认期间表格或任务目录已改变，未填写。')
            self.dialog.status.setText('正在重新核对并填写所选数字格…')
            self._submit({'action':'commit','config':self.payload['config'],'root':self.payload['root'],'plan':plan})
        except (OSError,ValueError,RuntimeError) as error:self._failed(str(error));self.dialog.set_selection_busy(False)
