from qt_compat import QtCore, QtWidgets
from app_plugins.api import MAIN_MENU, TASK_CONTEXT_MENU, PluginCommand, PluginMainWidget, GoogleAuthorization
from .store import Store
from .tasks import snapshot, spreadsheet_id, fingerprint
from .sheets import run_export, run_copy
from .segmentation import require_available, existing_segments
from .sheet_profiles import task_kind


class AIAgentAssistantPlugin(QtCore.QObject):
    plugin_id = 'ai_agent_assistant'
    display_name = 'AI Agent 助手'
    version = '0.5.0'
    required_api_version = 2

    def __init__(self):
        super().__init__()
        self.context = self.dialog = self.handle = None
        self._store = None
        self._pending_reference_plan = None
        self._pending_image_bind = None
        from .receipt_monitor import ReceiptMonitor
        self.receipts = ReceiptMonitor(self)
        from .notices import ReceiptNotices
        self.notices = ReceiptNotices(self)

    @property
    def store(self):
        if self._store is None: self._store = Store(self.context.storage.path)
        return self._store

    def register(self, context):
        self.context = context
        self.setParent(context.parent_widget)
        context.register_google_authorization(GoogleAuthorization('sheets', 'AI Agent 助手 · 口播表', 'sheets', 'v4',
            ('https://www.googleapis.com/auth/spreadsheets',), order=60, legacy_bindings=('sheets',)))
        context.register_google_authorization(GoogleAuthorization('downloads', 'AI Agent · 成品下载', 'drive', 'v3',
            ('https://www.googleapis.com/auth/drive',), order=61))
        context.register_google_authorization(GoogleAuthorization('uploads', 'AI Agent · 素材上传', 'drive', 'v3',
            ('https://www.googleapis.com/auth/drive',), order=62))
        context.register_command(PluginCommand('open', 'AI Agent 助手…', lambda _rows: self.open_ui(), frozenset({MAIN_MENU}), order=32))
        context.register_command(PluginCommand('oral', '添加到 AI Agent 口播任务…', self.open_tasks,
            frozenset({TASK_CONTEXT_MENU}), order=35, enabled=lambda rows: bool(rows)))
        context.register_main_widget(PluginMainWidget('quick', self.quick, order=32, title=self.display_name))
        context.register_command(PluginCommand('received_check', '检查并收取 Agent 成品', self.check_received,
            frozenset({MAIN_MENU, TASK_CONTEXT_MENU}), order=36,
            tooltip='立即后台检查所有已有 Agent 任务的最新成品；不打开助手，不重新提交制作。'))
        context.register_command(PluginCommand('received_again', '重新收取所选 Agent 成品…', self.recollect_received,
            frozenset({TASK_CONTEXT_MENU}), order=37, enabled=lambda rows: bool(rows),
            tooltip='重新下载校验所选任务最近已提交作业的当前成品，保留旧文件；不重新生成。'))
        context.register_command(PluginCommand('received_open', '打开 Agent 成品目录', self.notices.open_result,
            frozenset({TASK_CONTEXT_MENU}), order=38, enabled=lambda rows: len(self.notices.selected(rows)) == 1))
        context.register_command(PluginCommand('received_seen', '标记 Agent 成品已查看', self.notices.mark_seen,
            frozenset({TASK_CONTEXT_MENU}), order=39, enabled=lambda rows: any(item['unread'] for item in self.notices.selected(rows))))
        context.register_command(PluginCommand('received_filter', '只看新成品', self.notices.set_filter,
            frozenset({MAIN_MENU, TASK_CONTEXT_MENU}), order=40, checkable=True, checked=lambda: self.notices.filtered,
            submenu='Agent 成品', tooltip='仅显示有未查看成品的任务；再次点击恢复全部，空列表时也可从插件菜单取消。'))

    def check_received(self, _rows=()):
        self.receipts.check(manual=True)
        self.report_receipt_status()

    def report_receipt_status(self):
        self.context.log(self.receipts.status)
        notify = getattr(self.context, 'notify', None)
        if notify: notify('AI Agent 成品', self.receipts.status)

    def recollect_received(self, rows):
        directories = self.context.task_badge_keys(rows)
        return self.request_recollect(directories=directories)

    def request_recollect(self, *, ids=None, directories=None):
        if not ids and not directories: raise ValueError('请先选择要重新收取的任务。')
        if self.receipts.handle is not None or self.handle is not None:
            raise ValueError('当前 Agent 操作尚未结束，请完成后再重新收取；未重置下载记录。')
        answer = QtWidgets.QMessageBox.question(self.context.parent_widget, '重新收取 Agent 成品',
            '重新读取所选任务的当前成品链接并下载校验？\n已收取的文件也会重新检查，旧文件保留；内容相同不额外留副本。\n不会重新提交制作或消耗生成积分。',
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes: return
        self.receipts.check(manual=True, ids=ids, directories=directories, force=True)
        self.report_receipt_status()

    def quick(self, parent=None):
        widget = QtWidgets.QWidget(parent); layout = QtWidgets.QVBoxLayout(widget); layout.setContentsMargins(0, 0, 0, 0)
        button = QtWidgets.QPushButton('Agent 任务 · 导入与成品', widget)
        button.clicked.connect(self.open_ui); layout.addWidget(button)
        return widget

    def open_ui(self, *_args):
        try: return self.open_dialog()
        except Exception as error:
            QtWidgets.QMessageBox.warning(self.context.parent_widget, 'AI Agent 助手暂不可用', str(error))

    def open_dialog(self):
        if self.dialog is None:
            from .ui import AssistantDialog
            self.dialog = AssistantDialog(self, self.context.parent_widget)
        self.dialog.refresh_auth()
        self.dialog.show(); self.dialog.raise_(); self.dialog.activateWindow()
        return self.dialog

    def open_tasks(self, rows):
        try:
            self.open_dialog(); self.import_rows(rows)
        except Exception as error:
            if self.dialog: self.dialog.status.setText(str(error))
            else: QtWidgets.QMessageBox.warning(self.context.parent_widget, 'AI Agent 任务导入失败', str(error))

    def import_rows(self, rows, *, new_version=False):
        return self.import_targets(self.context.task_targets(tuple(rows)), new_version=new_version)

    def import_targets(self, targets, *, new_version=False):
        if self.handle is not None: raise ValueError('请先完成或取消当前提交。')
        if len(targets) > 1000: raise ValueError('每次最多导入1000条，请在主界面分批选择。')
        payloads, skipped, errors = [], 0, []
        for target in targets:
            task = target.get('task')
            if task is None or not str(getattr(task, 'task_audio_text', '') or '').strip(): skipped += 1; continue
            try:
                value = snapshot(target, self.context.task_language(task))
                if not value.get('agent'): value['agent'] = self.store.settings().get('default_agent', '')
                if self.dialog and self.dialog.chinese_source.isChecked() and not value['chinese_confirmed']:
                    value['chinese_confirmed'] = True
                    value['chinese_source'] = '用户确认：完整任务名称即中文台词'
                payloads.append(value)
            except ValueError as error: errors.append(str(error))
        if not payloads: raise ValueError('没有可导入的口播任务。' + ('；'.join(errors[:3]) if errors else '请先加载并选择包含口播台词的任务。'))
        result = self.store.stage(payloads, new_version=new_version)
        if self.dialog:
            self.dialog.reload(select_ids=result['imported'] + result['unchanged'])
            self.dialog.status.setText(f'已导入 {len(result["imported"])} 条，已存在 {len(result["unchanged"])} 条，未覆盖 {len(result["blocked"])} 条，跳过无台词 {skipped} 条。' + '；'.join(errors[:3]))
        return result

    def submit(self, target, ids, operation, *, reference_approval=None):
        if self.handle is not None: raise ValueError('当前操作尚未结束。')
        jobs = self.store.jobs(ids)
        if not jobs: raise ValueError('请选择任务。')
        if operation == 'submit':
            oral = [job for job in jobs if task_kind(job['payload']) == 'oral' and job['state'] in {'draft', 'failed'}]
            if any(existing_segments(job['payload'].get('spoken', '')) is None for job in oral): require_available(self.context)
            missing = [job for job in oral if not job['payload'].get('agent', '').strip()]
            default = str(target.get('default_agent') or self.store.settings().get('default_agent') or '').strip()
            if missing and default:
                self.store.edit_batch([job['id'] for job in missing], 'agent', default,
                    expected={job['id']: fingerprint(job['payload']) for job in missing})
                jobs = self.store.jobs(ids); missing = []
            if missing:
                answer = QtWidgets.QMessageBox.question(self.dialog or self.context.parent_widget, '尚未分配执行 Agent',
                    f'有{len(missing)}条任务没有执行 Agent，外部AI可能无法领取。\n仍提交为待管理员分配的任务？\n取消后可右键批量分配，或填写默认 Agent。',
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                    QtWidgets.QMessageBox.StandardButton.No)
                if answer != QtWidgets.QMessageBox.StandardButton.Yes: return
        if operation == 'submit' and reference_approval is None and self.plan_local_references(target, jobs): return
        self.store.save_settings(target)
        authorization = self.context.prepare_google_account('sheets')
        self.handle = self.context.submit_background_task(title='AI Agent · ' + ('提交' if operation == 'submit' else '核对'),
            runner=run_export, payload={**target, 'ids': list(ids), 'root': str(self.context.storage.path), 'operation': operation,
                                        'account_id': authorization['account_id'], 'google_authorization': authorization,
                                        'reference_approval': reference_approval},
            kind='network', execution='parallel', resources=['agent-sheet:' + spreadsheet_id(target['url'])], retryable=False)
        self.handle.completed.connect(self.completed); self.handle.failed.connect(self.failed); self.handle.finished.connect(self.finished)
        self.handle.progressChanged.connect(self.progress)
        if self.dialog: self.dialog.busy(True); self.show_status('已交由主程序后台执行，可关闭此窗口。')

    def copy_tasks(self, ids):
        if self.handle is not None: raise ValueError('当前操作尚未结束。')
        if any(task_kind(job['payload']) == 'oral' and existing_segments(job['payload'].get('spoken', '')) is None for job in self.store.jobs(ids)):
            require_available(self.context)
        from .references import reference_sources
        if any(source['kind'] == 'local' for job in self.store.jobs(ids) for source in reference_sources(job['payload'])):
            raise ValueError('复制表格不会自动上传本地图片，请先提交并确认素材上传，或改为Drive链接。')
        self.handle = self.context.submit_background_task(title='AI Agent · 外文10秒分段与复制',
            runner=run_copy, payload={'ids': list(ids), 'root': str(self.context.storage.path)},
            kind='cpu', execution='parallel', retryable=False)
        self.handle.completed.connect(self.completed); self.handle.failed.connect(self.failed)
        self.handle.finished.connect(self.finished); self.handle.progressChanged.connect(self.progress)
        if self.dialog: self.dialog.busy(True); self.dialog.status.setText('后台分段中，完成后自动复制；不调用AI或音频模型。')

    def completed(self, result):
        if 'image_bind_choices' in result:
            self._pending_image_bind = result; return
        if 'upload_plan' in result:
            self._pending_reference_plan = result
            return
        if 'clipboard' in result: QtWidgets.QApplication.clipboard().setText(result['clipboard'])
        message = result['message']
        self.context.log(message)
        if self.dialog: self.dialog.reload(); self.show_status(message)

    def failed(self, message):
        if self.dialog: self.dialog.reload(); self.show_status(message)

    def show_status(self, message):
        if self.dialog:
            self.dialog.status.setText(message)
            self.dialog.image_page.status.setText(message)

    def progress(self, message, _percent):
        self.show_status(message)

    def finished(self):
        if self.sender() is not self.handle: return
        self.handle = None
        if self.dialog: self.dialog.reload()
        if self._pending_reference_plan is not None:
            result, self._pending_reference_plan = self._pending_reference_plan, None
            self.confirm_local_references(result)
        if self._pending_image_bind is not None:
            result, self._pending_image_bind = self._pending_image_bind, None
            try:
                from .image_ui import confirm_image_bind
                confirm_image_bind(self, result)
            except Exception as error: self.show_status(str(error))

    def query_existing_images(self, target, main_target):
        if self.handle is not None: raise ValueError('请先结束当前操作。')
        from .image_tasks import run_image_bind_list
        local = snapshot(main_target, '')
        authorization = self.context.prepare_google_account('sheets')
        self.store.save_settings(target)
        self.handle = self.context.submit_background_task(title='AI Agent · 读取已有图片任务', runner=run_image_bind_list,
            payload={**target, 'target_dir': local['target_dir'], 'parent_task_id': local['task_id'], 'authorization': authorization},
            kind='network', execution='parallel', retryable=False)
        self.handle.completed.connect(self.completed); self.handle.failed.connect(self.failed); self.handle.finished.connect(self.finished)
        if self.dialog: self.dialog.busy(True); self.show_status('只读获取已有图片任务，下一步由你确认接收目录。')

    def remove_job_records(self, ids, parent=None):
        if self.handle is not None or self.receipts.handle is not None: raise ValueError('Agent操作仍在运行，请结束后再移除。')
        jobs = self.store.jobs(ids)
        answer = QtWidgets.QMessageBox.question(parent or self.dialog or self.context.parent_widget, '移除本地记录',
            f'移除{len(jobs)}条本地记录并停止监视？云端任务、网盘文件和本地成品全部保留。\n重新导入会另建作业，请先核对旧任务避免重复派单。',
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No, QtWidgets.QMessageBox.StandardButton.No)
        if answer != QtWidgets.QMessageBox.StandardButton.Yes: return
        self.store.remove_records({job['id']: (job['state'], fingerprint(job['payload'])) for job in jobs})
        if self.dialog: self.dialog.reload()
        self.notices.refresh(); self.show_status(f'已移除{len(jobs)}条本地记录，云端任务和全部文件保留。')

    def plan_local_references(self, target, jobs):
        from .references import reference_sources, run_reference_plan
        if not any(source['kind'] == 'local' for job in jobs if job['state'] in {'draft', 'failed'} for source in reference_sources(job['payload'])):
            return False
        self.handle = self.context.submit_background_task(title='AI Agent · 预览待上传图片', runner=run_reference_plan,
            payload={'root': str(self.context.storage.path), 'ids': [job['id'] for job in jobs], 'next': dict(target)},
            kind='cpu', execution='parallel', retryable=False)
        self.handle.completed.connect(self.completed); self.handle.failed.connect(self.failed); self.handle.finished.connect(self.finished)
        if self.dialog: self.dialog.busy(True); self.show_status('后台检查图片并生成预览；确认前不会上传或提交。')
        return True

    def confirm_local_references(self, result):
        try:
            if not result['upload_plan']: raise ValueError('本地图片任务已改变，请刷新后重新提交。')
            from .reference_ui import ReferenceUploadDialog
            dialog = ReferenceUploadDialog(self, result['upload_plan'])
            accepted = dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted
            if not accepted:
                if self.dialog: self.show_status('已取消图片上传；任务未提交。')
                dialog.deleteLater(); return
            account = dialog.accounts.currentData(); parent = dialog.parent_id
            dialog.manager.bind('ai_agent_assistant.uploads', account)
            authorization = self.context.prepare_google_account('uploads')
            if authorization['account_id'] != account: raise ValueError('上传账号已改变，请重新确认。')
            settings = self.store.settings(); settings['upload_folder'] = parent; self.store.save_settings(settings)
            plan = [{**job, 'files': [{key: value for key, value in image.items() if key != 'thumbnail'}
                for image in job['files']]} for job in result['upload_plan']]
            approval = {'confirmed': True, 'authorization': authorization, 'folder': parent, 'plan': plan}
            dialog.deleteLater()
            ids = [item['id'] for item in result['upload_plan']]
            # Include Drive-only tasks from the same reviewed selection as well.
            ids = result.get('ids', ids)
            self.submit({**result['next'], 'upload_folder': parent}, ids, 'submit', reference_approval=approval)
        except Exception as error:
            if self.dialog: self.show_status('素材上传未启动：' + str(error))
            else: self.context.log('素材上传未启动：' + str(error))

    def can_close(self): return (self.handle is None and self.receipts.handle is None and self.notices.handle is None, 'AI Agent 后台操作仍在结束中，请先完成或取消。')
    def start(self): self.notices.refresh(); self.receipts.start()
    def stop(self):
        if not self.notices.stop(): return False
        if not self.receipts.stop(): return False
        if self.handle is not None: return False
        if self.dialog: self.dialog.close()
        return True
