"""Runtime controller for task-result delivery, link history, and review status."""
from model.AppDataStorage import data_io

import logging
from functools import partial
from model.QtThreadLifecycle import thread_is_active
import os
import shutil
from datetime import date
from pathlib import Path

from qt_compat import QtCore, QtWidgets
from qt_compat import QMessageBox

from model.DailyLinkHistory import (
    DAILY_LINK_HISTORY_CONFIG_KEY,
    daily_link_counts,
    daily_task_sheet_failure_count,
    normalize_daily_link_history,
    record_daily_person_links,
    update_daily_task_sheet_results,
)
from model.DailyLinkArchive import (
    load_daily_link_archive,
    merge_link_histories,
    save_daily_link_archive,
)
from model.GlobalHotkey import (
    DEFAULT_TASK_RESULT_HOTKEY,
    GlobalHotkeyManager,
    TASK_RESULT_HOTKEY_CONFIG_KEY,
    TASK_RESULT_HOTKEY_ID,
    hotkey_setting_changed,
    normalize_hotkey_sequence,
)
from model.ReviewStatusMonitor import (
    ReviewStatusMonitorThread,
    normalize_review_status_settings,
)
from model.ReviewSubmissionHistory import review_history_snapshot
from model.TaskDeliveryBackground import UPLOAD_RESOURCE, run_delivery_upload, upload_work_directory
from model.TaskResultOrganizer import (
    TaskResultOrganizerThread,
    load_effective_config as load_task_result_config,
)
from PYUI.daily_links_pyui import DailyLinksDialog
from PYUI.review_status_pyui import ReviewStatusDialog
from PYUI.task_delivery_pyui import TaskReviewListDialog, UpdatedFilesDetectionDialog
from app_plugins.builtin.task_delivery_config import (
    TaskDeliveryConfigDialog,
    missing_delivery_fields,
)


class TaskDeliveryController:
    def __init__(self, plugin, context):
        self.plugin = plugin
        self.context = context
        self.task_result_thread = None
        self.review_status_thread = None
        self.review_status_settings = normalize_review_status_settings({})
        self.review_status_config = load_task_result_config({})
        self.review_status_state = "未启动"
        self._manual_review_quantity_refresh = False
        self.daily_link_history = normalize_daily_link_history({})
        self.daily_link_archive = {}
        self.global_hotkey = DEFAULT_TASK_RESULT_HOTKEY
        self.hotkey_manager = None
        self._organize_starting = False
        self._upload_jobs = {}
        self._completed_versions = {}

    @property
    def window(self):
        return self.context.parent_widget

    @property
    def quick_actions(self):
        return self.plugin.quick_actions

    def _sync_compatibility_state(self):
        window = self.window
        window.task_result_thread = self.task_result_thread
        window.review_status_thread = self.review_status_thread
        window.review_status_settings = self.review_status_settings
        window.review_status_config = self.review_status_config
        window.review_status_state = self.review_status_state
        window.daily_link_history = self.daily_link_history
        window.task_result_global_hotkey = self.global_hotkey
        window.task_result_hotkey_manager = self.hotkey_manager

    def start(self):
        config = self.context.load_config()
        self.daily_link_history = normalize_daily_link_history(
            config.get(DAILY_LINK_HISTORY_CONFIG_KEY)
        )
        try:
            self.daily_link_archive = load_daily_link_archive(self.daily_link_history)
        except (OSError, ValueError) as error:
            self.daily_link_archive = merge_link_histories(self.daily_link_history)
            self.context.log(f"每日链接归档加载失败：{error}", logging.ERROR)
        self.review_status_settings = normalize_review_status_settings(config)
        self.review_status_config = load_task_result_config(config)
        try:
            self.global_hotkey = normalize_hotkey_sequence(
                config.get(
                    TASK_RESULT_HOTKEY_CONFIG_KEY,
                    DEFAULT_TASK_RESULT_HOTKEY,
                )
            )
        except ValueError:
            self.global_hotkey = DEFAULT_TASK_RESULT_HOTKEY
        self.hotkey_manager = GlobalHotkeyManager(
            self.window,
            hotkey_id=TASK_RESULT_HOTKEY_ID,
        )
        self.hotkey_manager.activated.connect(self.trigger_global_hotkey)
        self._sync_compatibility_state()
        self.register_hotkey(self.global_hotkey, show_error=False)
        self.update_daily_links_button()
        self.update_review_status_button()
        try:
            self.plugin.sync_delivery_todos()
        except Exception as error:
            self.context.log(f"交付待办初始化失败：{error}", logging.ERROR)
        self.start_review_status_monitor()

    def update_config(self, config):
        config[TASK_RESULT_HOTKEY_CONFIG_KEY] = self.global_hotkey
        # A stale controller must not erase links that another settings save
        # already wrote to disk. Resolved task-sheet failures still follow the
        # live controller state, so merge only the person links.
        on_disk = normalize_daily_link_history(
            config.get(DAILY_LINK_HISTORY_CONFIG_KEY)
        )
        current = normalize_daily_link_history(self.daily_link_history)
        links = merge_link_histories(on_disk, current)
        for day in set(on_disk) | set(current):
            entry = links.setdefault(day, {
                "updated_at": "", "people": {}, "task_sheet_failures": {},
            })
            if day in current:
                entry["task_sheet_failures"] = current[day]["task_sheet_failures"]
                entry["updated_at"] = max(
                    entry["updated_at"], current[day].get("updated_at", "")
                )
            elif day in on_disk:
                entry["task_sheet_failures"] = on_disk[day]["task_sheet_failures"]
        config[DAILY_LINK_HISTORY_CONFIG_KEY] = normalize_daily_link_history(links)
        return config

    def apply_settings(self, config):
        previous_settings = dict(self.review_status_settings)
        previous_config = dict(self.review_status_config)
        self.review_status_settings = normalize_review_status_settings(config)
        self.review_status_config = load_task_result_config(config)
        monitor_keys = (
            "review_sheet_url",
            "review_sheet_credentials_file",
            "review_sheet_token_file",
        )
        monitor_changed = (
            self.review_status_settings != previous_settings
            or any(
                self.review_status_config.get(key) != previous_config.get(key)
                for key in monitor_keys
            )
        )
        review_ok = (
            self.restart_review_status_monitor() if monitor_changed else True
        )
        requested_hotkey = config.get(TASK_RESULT_HOTKEY_CONFIG_KEY, self.global_hotkey)
        hotkey_ok = (
            self.register_hotkey(requested_hotkey, show_error=False)
            if hotkey_setting_changed(requested_hotkey, self.global_hotkey)
            else True
        )
        if not review_ok:
            self.review_status_settings = previous_settings
            self.review_status_config = previous_config
            self.start_review_status_monitor()
        self._sync_compatibility_state()
        self.update_shortcut_tooltip()
        return review_ok and hotkey_ok

    def can_close(self):
        thread = self.task_result_thread
        if thread_is_active(thread):
            return False, "正在整理、检测或上传文件。为避免中断上传，请等待完成。"
        return True, ""

    def stop(self):
        if not self.can_close()[0] or not self.stop_review_status_monitor():
            self.context.log('任务交付仍在安全停止，保留线程及快捷键。', logging.WARNING)
            return False
        if self.hotkey_manager is not None:
            self.hotkey_manager.close()
            self.hotkey_manager = None
        self._sync_compatibility_state()
        return True

    def register_hotkey(self, shortcut, show_error=True):
        try:
            normalized = normalize_hotkey_sequence(shortcut)
        except ValueError as error:
            self.context.log(f"全局快捷键无效：{error}", logging.ERROR)
            if show_error:
                QMessageBox.warning(
                    self.window,
                    "全局快捷键无效",
                    f"整理任务结果：{error}",
                )
            return False
        manager = self.hotkey_manager
        if manager is None:
            self.global_hotkey = normalized
            self._sync_compatibility_state()
            return True
        if manager.register(normalized):
            self.global_hotkey = normalized
            self._sync_compatibility_state()
            self.context.log(f"全局快捷键已启用：{normalized}")
            return True
        message = f"{normalized} 无法注册，可能已被其他程序占用。 {manager.last_error}"
        self.context.log(f"全局快捷键未启用：{message}", logging.ERROR)
        if show_error:
            QMessageBox.warning(self.window, "全局快捷键注册失败", message)
        return False

    def trigger_global_hotkey(self):
        self.window.activateForGlobalAction()
        self.organize_results()

    def update_shortcut_tooltip(self):
        if self.quick_actions is not None:
            self.quick_actions.organize_button.setToolTip(
                "整理并上传当前任务结果。系统全局快捷键：{}".format(
                    self.global_hotkey
                )
            )

    def organize_results(self):
        app = QtWidgets.QApplication.instance()
        if app is None or QtCore.QThread.currentThread() != app.thread():
            self.context.log("整理入口须由主界面调用，未开始任何操作。", logging.ERROR)
            return None
        # A modal preflight runs a nested event loop; hotkeys must not start a
        # second organizer before the first dialog has finished.
        if self._organize_starting:
            return None
        self._organize_starting = True
        try:
            return self._organize_results()
        finally:
            self._organize_starting = False

    def _organize_results(self):
        thread = self.task_result_thread
        if thread_is_active(thread):
            QMessageBox.information(
                self.window,
                "整理任务结果",
                "本批次仍在整理或审核；后台上传不影响下一批整理。",
            )
            return None
        base_dir = Path(self.window.task_path_edit.text().strip())
        if not base_dir.is_dir():
            self.window.Critical("任务路径不是一个目录！！")
            return None
        config = self._prepare_delivery_config()
        if config is None:
            return None
        selected_date = self.window.dateEdit.date()
        task_date = date(
            selected_date.year(),
            selected_date.month(),
            selected_date.day(),
        )
        thread = TaskResultOrganizerThread(
            [task_date],
            base_dir,
            config,
            self.window,
            interactive_detection_choice=True,
            gemini_keys=self.context.gemini_keys,
            prepare_only=True,
            upload_work_dir=upload_work_directory(base_dir),
            claimed_versions=self._claimed_upload_versions(),
        )
        thread.log.connect(self.on_task_result_log)
        thread.detection_choice_requested.connect(self.on_detection_choice_requested)
        thread.review_plan_requested.connect(self.on_review_plan_requested)
        thread.completed.connect(self.on_task_result_completed)
        thread.failed.connect(self.on_task_result_failed)
        thread.finished.connect(lambda current=thread: self.on_task_result_finished(current))
        self.task_result_thread = thread
        self._sync_compatibility_state()
        if self.quick_actions is not None:
            self.quick_actions.organize_button.setEnabled(False)
            self.quick_actions.organize_button.setText("正在整理/审核…")
        self.context.log(f"开始整理任务结果：{task_date:%Y-%m-%d}")
        thread.start()
        return thread

    def _prepare_delivery_config(self):
        try:
            original = self.context.load_config()
            config = load_task_result_config(original)
            if not missing_delivery_fields(config):
                return config
            dialog = TaskDeliveryConfigDialog(config, self.window)
            try:
                if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                    self.context.log("已取消配置填写，本次整理未开始；待上传记录保留。")
                    return None
                updates = dialog.config_updates()
            finally:
                dialog.deleteLater()
            if not self.context.patch_config(updates, expected=original):
                self.context.log("配置未能保存，本次整理未开始；待上传记录保留。", logging.WARNING)
                return None
            config = load_task_result_config(self.context.load_config())
            if missing_delivery_fields(config):
                QMessageBox.warning(self.window, "配置已变化", "当前配置仍有缺项，请重新点击整理并确认。")
                return None
            self.context.log("整理 / 上传必填配置已保存。")
            return config
        except Exception:
            logging.getLogger(__name__).exception("准备整理 / 上传配置失败")
            self.context.log("配置读取或保存失败，本次整理未开始，请查看程序日志。", logging.ERROR)
            QMessageBox.warning(self.window, "暂时无法准备配置", "配置读取或保存失败，详细信息已写入日志。\n未开始整理或上传。")
            return None

    def on_task_result_log(self, text):
        self.context.log(text)

    def on_review_plan_requested(self, entries):
        choices = None
        try:
            dialog = TaskReviewListDialog(entries, self.window)
            if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                choices = dialog.decisions()
        except Exception as error:
            logging.getLogger(__name__).exception("显示视频审核清单失败")
            self.context.log(f"显示视频审核清单失败，已取消上传：{error}", logging.ERROR)
        finally:
            if self.task_result_thread is not None:
                self.task_result_thread.set_review_plan(choices)

    def on_detection_choice_requested(self, updated_files):
        selected_mode = "cancel"
        try:
            dialog = UpdatedFilesDetectionDialog(updated_files, self.window)
            if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
                selected_mode = dialog.selected_mode
        except BaseException as error:
            self.context.log(
                f"显示更新文件列表失败，已取消本次操作：{error}",
                logging.ERROR,
            )
        finally:
            if self.task_result_thread is not None:
                self.task_result_thread.set_detection_choice(selected_mode)
        mode_names = {
            "ai": "使用 AI 检测",
            "skip": "无需检测",
            "manual": "人工清单审核",
            "cancel": "取消本次操作，保留待处理列表",
        }
        self.context.log(
            f"本轮视频处理方式：{mode_names.get(selected_mode, selected_mode)}"
        )

    def on_task_result_completed(self, result):
        if result.get("cancelled"):
            QMessageBox.information(
                self.window,
                "已取消整理任务结果",
                str(result.get("message") or "本次操作已取消。"),
            )
            return
        if bool(
            load_task_result_config(self.context.load_config()).get(
                "open_result_dir",
                True,
            )
        ):
            for directory in result.get("result_dirs", []):
                path = Path(directory)
                if path.exists():
                    try:
                        os.startfile(str(path))
                    except OSError as error:
                        self.context.log(f"打开结果目录失败：{error}", logging.ERROR)
        plan = result.pop("upload_plan", None)
        if plan is not None:
            self._queue_upload(plan)
            if result.get("deferred_file_count"):
                self.context.log(f"另有 {result['deferred_file_count']} 个文件仍在写入，留待下次整理。")
            return
        local_plan = result.pop('postprocess_plan', None)
        if local_plan is not None:
            self._queue_local_postprocess(local_plan)
            return
        saved_link_count, failed_file_count = self.record_task_result_history(result)
        if result.get("uploaded_file_count") and self.context.load_config().get(
            "daily_quantity_sheet_url"
        ):
            self.plugin.refresh_daily_quantity()
        self.request_review_status_check()
        message = str(result.get("message") or "整理完成")
        details = "{}\n本次新增/更新：{} 个文件".format(
            message,
            result.get("changed_file_count", 0),
        )
        if result.get("deferred_file_count"):
            details += f"\n暂缓整理：{result['deferred_file_count']} 个文件仍在写入或尚未稳定，下次整理自动重试（详见日志）"
        if result.get("upload_batch"):
            details += f"\n上传批次：{result['upload_batch']}"
            details += f"\n成功同步：{result.get('uploaded_file_count', 0)} 个文件"
            if result.get("shortcut_created_count"):
                details += f"\n跨批次修改视频：本次目录快捷方式 {result['shortcut_created_count']} 个"
                details += "\n注意：快捷方式不会自动授予收件人原视频的访问权限。"
            if result.get("shortcut_failed_files"):
                details += f"\n⚠ 快捷方式创建失败：{len(result['shortcut_failed_files'])} 个，请查看程序日志"
        if saved_link_count:
            details += f"\n每日链接汇总：已保存 {saved_link_count} 人（保留 7 天）"
        if failed_file_count:
            details += f"\n任务提交表待核对：{failed_file_count} 个视频"
        QMessageBox.information(self.window, "整理任务结果", details)

    def _claimed_upload_versions(self):
        versions = {key: list(value) for key, value in self._completed_versions.items()}
        from model.TaskResultOrganizer import file_identity
        for job in self._upload_jobs.values():
            for item in job["files"].values():
                versions.setdefault(file_identity(item["original"]), []).append(item["version"])
        return versions

    def _queue_upload(self, plan):
        count = len(plan["files"])
        title = f"任务上传 · {plan['batch_date']} / {plan['batch_slot']} · {count} 个文件"
        try:
            from .task_delivery_workflow import prepare
            payload, registry, resources = prepare(plan['config'], self.context)
            if payload:
                plan['postprocess'] = payload
                from model.BackgroundTasks import directory_resource
                resources = tuple(set(resources) | {directory_resource(Path(row['root']).parent / '后处理结果')
                                                    for row in plan['files'].values()})
            handle = self.context.submit_background_task(
                title=title, runner=partial(run_delivery_upload, registry=registry) if payload else run_delivery_upload,
                payload=plan, kind="network",
                resources=tuple(sorted(set(resources) | {UPLOAD_RESOURCE})), retryable=False)
            self._upload_jobs[handle.task_id] = plan
            handle.completed.connect(lambda result, job=plan: self._on_background_upload_completed(job, result))
            handle.failed.connect(lambda message: self.context.notify("任务上传未全部完成", message, critical=True))
            handle.finished.connect(lambda key=handle.task_id: self._on_background_upload_finished(key))
            self.context.log(f"{title} 已交给后台任务，上传串行执行；可以继续工作、整理下一批。")
            self._update_upload_button()
        except Exception as error:
            logging.getLogger(__name__).exception("提交后台上传失败")
            data_io.rmtree(plan["work_dir"], ignore_errors=True)
            self.on_task_result_failed("未能加入后台队列；待上传记录已保留，请重新整理。\n" + str(error))

    def _queue_local_postprocess(self, plan):
        from .task_delivery_workflow import prepare
        from model.TaskDeliveryBackground import run_delivery_postprocess
        from model.BackgroundTasks import directory_resource
        try:
            payload, registry, resources = prepare(plan['config'], self.context)
            if payload is None:
                raise ValueError('本批次配置中的后处理工作流已取消。')
            plan['postprocess'] = payload
            locks = {directory_resource(Path(row['root']).parent / '后处理结果') for row in plan['files'].values()}
            handle = self.context.submit_background_task(title='任务整理 · 本地视频后处理',
                runner=partial(run_delivery_postprocess, registry=registry), payload=plan, kind='media',
                resources=tuple(sorted(set(resources) | locks)), retryable=False)
            handle.completed.connect(lambda result: self.context.notify('本地后处理完成', result['message']))
            handle.failed.connect(lambda message: self.context.notify('本地后处理未完成', message, critical=True))
            handle.finished.connect(lambda: data_io.rmtree(plan['work_dir'], ignore_errors=True))
            self.context.log('本地后处理已进入后台；上传关闭，不会联网上传、填表或计数。')
        except Exception as error:
            data_io.rmtree(plan['work_dir'], ignore_errors=True)
            self.context.log('后处理未能提交：' + str(error), logging.ERROR)
            self.context.notify('后处理未开始', str(error), critical=True)

    def _on_background_upload_completed(self, plan, result):
        try:
            from model.TaskResultOrganizer import file_identity
            failed = {file_identity(item["local_file"]) for item in result.get("upload_failed_files", [])}
            for item in plan["files"].values():
                identity = file_identity(item["original"])
                if identity not in failed:
                    self._completed_versions.setdefault(identity, []).append(item["version"])
            while len(self._completed_versions) > 2048:
                self._completed_versions.pop(next(iter(self._completed_versions)))
            self.record_task_result_history(result)
            if result.get("uploaded_file_count"):
                if self.context.load_config().get("daily_quantity_sheet_url"):
                    self.plugin.refresh_daily_quantity()
                self.request_review_status_check()
            errors = len(result.get("upload_failed_files", []))
            self.context.log(f"后台上传结果：{plan['batch_date']} / {plan['batch_slot']}，"
                             f"成功 {result.get('uploaded_file_count', 0)}，未成功 {errors}。")
            if not errors and not result.get("cancelled_upload"):
                self.context.notify("任务上传完成", f"{plan['batch_date']} / {plan['batch_slot']}："
                                    f"已同步 {result.get('uploaded_file_count', 0)} 个文件。")
        except Exception:
            logging.getLogger(__name__).exception("保存后台上传界面汇总失败")
            self.context.log("后台上传已返回，但界面汇总更新失败，请查看日志与上传历史。", logging.ERROR)

    def _on_background_upload_finished(self, key):
        plan = self._upload_jobs.pop(key, None)
        if plan is not None:
            data_io.rmtree(plan["work_dir"], ignore_errors=True)
        self._update_upload_button()

    def _update_upload_button(self):
        if self.quick_actions is not None and self.task_result_thread is None:
            count = len(self._upload_jobs)
            self.quick_actions.organize_button.setText(
                f"整理任务结果（后台 {count} 批）" if count else "整理任务结果")

    def on_task_result_failed(self, message):
        QMessageBox.critical(
            self.window,
            "整理任务结果失败",
            f"{message}\n\n详细过程已写入主界面日志。",
        )

    def on_task_result_finished(self, thread=None):
        thread = thread if thread is not None else self.task_result_thread
        if thread is not self.task_result_thread:
            return
        if thread is not None and not thread.wait(0):
            QtCore.QTimer.singleShot(10, lambda current=thread: self.on_task_result_finished(current))
            return
        self.task_result_thread = None
        self._sync_compatibility_state()
        if self.quick_actions is not None:
            button = self.quick_actions.organize_button
            button.setEnabled(True)
            button.setText("整理任务结果")
            self.update_shortcut_tooltip()
            self._update_upload_button()
        if thread is not None:
            thread.deleteLater()

    def update_daily_links_button(self):
        if self.quick_actions is None:
            return
        today_key = date.today().isoformat()
        visible_links = merge_link_histories(
            self.daily_link_archive, self.daily_link_history
        )
        people_count, link_count = daily_link_counts(
            visible_links,
            today_key,
            retain_all=True,
        )
        failure_count = daily_task_sheet_failure_count(
            self.daily_link_history,
            today_key,
        )
        button = self.quick_actions.daily_links_button
        button.setText("查看每日链接")
        button.setToolTip(
            f"今天已保存 {people_count} 人、{link_count} 个批次链接；"
            f"任务提交表有 {failure_count} 个视频待核对。\n"
            "黄色表示今天已有链接，红色表示还有待核对视频。"
        )
        if failure_count:
            button.setStyleSheet("background-color:#FFD6D6;color:#202124;")
        elif link_count:
            button.setStyleSheet("background-color:#FFF1B8;color:#202124;")
        else:
            button.setStyleSheet("")

    def open_daily_links(self):
        normalized = normalize_daily_link_history(self.daily_link_history)
        if normalized != self.daily_link_history:
            self.daily_link_history = normalized
            self._sync_compatibility_state()
        history = merge_link_histories(self.daily_link_archive, normalized)
        # Keep the seven-day task-sheet-failure panel; the archive stores links only.
        for day, entry in normalized.items():
            display_entry = history.setdefault(day, {
                "updated_at": entry.get("updated_at", ""),
                "people": {}, "task_sheet_failures": {},
            })
            display_entry["task_sheet_failures"] = entry["task_sheet_failures"]
        return DailyLinksDialog(history, self.window).exec()

    def record_task_result_history(self, result):
        upload_date = result.get("upload_date") or date.today().isoformat()
        upload_slot = result.get("upload_slot") or "未标记"
        history = self.daily_link_history
        saved_link_count = 0
        failed_file_count = 0
        try:
            person_links = result.get("person_folder_links")
            if isinstance(person_links, dict) and person_links:
                history, saved_link_count = record_daily_person_links(
                    history,
                    upload_date,
                    upload_slot,
                    person_links,
                )
            history, failed_file_count = update_daily_task_sheet_results(
                history,
                upload_date,
                upload_slot,
                result.get("task_sheet_failed_files", ()),
                result.get("task_sheet_successful_files", ()),
            )
        except (TypeError, ValueError) as error:
            self.context.log(f"每日记录保存失败：{error}", logging.ERROR)
            return 0, 0
        if history == self.daily_link_history:
            return saved_link_count, failed_file_count
        self.daily_link_history = history
        self._sync_compatibility_state()
        if saved_link_count:
            try:
                self.daily_link_archive = save_daily_link_archive(
                    merge_link_histories(self.daily_link_archive, history)
                )
            except (OSError, ValueError) as error:
                self.context.log(f"每日链接长期归档失败：{error}", logging.ERROR)
        if not self.context.save_config():
            return 0, 0
        self.update_daily_links_button()
        if saved_link_count:
            self.context.log(
                f"每日链接已保存 {upload_date} / 批次 {upload_slot}："
                f"{saved_link_count} 人"
            )
        if failed_file_count:
            self.context.log(
                f"任务提交表有 {failed_file_count} 个视频未能确认填写成功，"
                "已加入每日待核对记录。"
            )
        try:
            self.plugin.sync_delivery_todos()
        except Exception as error:
            self.context.log(f"交付待办更新失败：{error}", logging.ERROR)
        return saved_link_count, failed_file_count

    def update_review_status_button(self, snapshot=None):
        if self.quick_actions is None:
            return
        snapshot = snapshot or review_history_snapshot()
        passed_count = int(snapshot.get("passed_count", 0) or 0)
        changes_count = int(snapshot.get("needs_changes_count", 0) or 0)
        button = self.quick_actions.review_status_button
        if changes_count:
            button.setText(f"审核提醒：需修改 {changes_count}")
        elif passed_count:
            button.setText(f"审核提醒：待发送 {passed_count}")
        else:
            button.setText("审核提醒")
        button.setToolTip(
            "审核通过待发送：{} 个；需要修改：{} 个。\n"
            "红色优先表示存在需要修改的视频，绿色表示有审核通过的视频待发送。\n"
            "双击列表中的视频可先打开检查。".format(
                passed_count,
                changes_count,
            )
        )
        if changes_count:
            button.setStyleSheet("background-color:#FFD6D6;color:#202124;")
        elif passed_count:
            button.setStyleSheet("background-color:#DDF3E4;color:#202124;")
        else:
            button.setStyleSheet("")

    def open_review_status(self):
        result = ReviewStatusDialog(parent=self.window).exec()
        self.update_review_status_button()
        return result

    def start_review_status_monitor(self, force_once=False):
        from model.OfflineMode import is_offline
        if is_offline():
            self.review_status_state = '离线暂停'
            self._sync_compatibility_state()
            self.update_review_status_button()
            return
        if not force_once and not self.review_status_settings.get("review_status_monitor_enabled"):
            self.review_status_state = "未启用"
            self._sync_compatibility_state()
            self.update_review_status_button()
            return
        if not str(self.review_status_config.get("review_sheet_url") or "").strip():
            self.review_status_state = "未配置审核表"
            self._sync_compatibility_state()
            self.update_review_status_button()
            return
        if self.review_status_thread is not None:
            return
        thread = ReviewStatusMonitorThread(
            self.review_status_config,
            parent=self.window,
            single_pass=force_once and not self.review_status_settings.get(
                "review_status_monitor_enabled"
            ),
        )
        thread.status.connect(self.on_review_status_monitor_status)
        thread.snapshot.connect(self.on_review_status_snapshot)
        thread.checked.connect(self.on_review_status_checked)
        thread.changed.connect(self.on_review_status_changed)
        thread.log.connect(lambda message: self.context.log(message))
        thread.finished.connect(lambda current=thread: self.on_review_status_monitor_finished(current))
        self.review_status_thread = thread
        self.review_status_state = "正在手动检查…" if thread.single_pass else "正在启动…"
        self._sync_compatibility_state()
        thread.start()

    def stop_review_status_monitor(self, wait_ms=10000):
        thread = self.review_status_thread
        if thread is None:
            return True
        if thread_is_active(thread):
            thread.stop()
            if not thread.wait(wait_ms):
                return False
        if self.review_status_thread is thread:
            self.review_status_thread = None
        self._sync_compatibility_state()
        thread.deleteLater()
        return True

    def restart_review_status_monitor(self):
        if not self.stop_review_status_monitor():
            QMessageBox.warning(
                self.window,
                "审核提醒",
                "后台表格检查仍在停止，请稍后再试。",
            )
            return False
        self.start_review_status_monitor()
        return True

    def request_review_status_check(self, manual=False):
        if manual:
            self._manual_review_quantity_refresh = True
        if self.review_status_thread is None:
            self.start_review_status_monitor(force_once=manual)
        if self.review_status_thread is not None:
            self.review_status_thread.request_check()
        elif manual:
            self._manual_review_quantity_refresh = False
            QMessageBox.warning(self.window, "审核检查", "请先在程序设置中填写审核表格链接。")

    def on_review_status_monitor_status(self, status):
        status = str(status)
        if status != self.review_status_state:
            self.review_status_state = status
            self._sync_compatibility_state()
            if status == "异常":
                self.context.log("监视器出现异常，请查看后续错误日志。", logging.ERROR)

    def on_review_status_snapshot(self, snapshot):
        self.update_review_status_button(snapshot)
        try:
            self.plugin.sync_delivery_todos(snapshot)
        except Exception as error:
            self.context.log(f"审核待办更新失败：{error}", logging.ERROR)
        delivery = getattr(self.window, "task_delivery_plugin", None)
        quantity_dialog = getattr(delivery, "daily_quantity_dialog", None)
        if quantity_dialog is not None and quantity_dialog.isVisible():
            quantity_dialog.refresh_review_statuses()

    def on_review_status_checked(self, _snapshot):
        if not self._manual_review_quantity_refresh:
            return
        self._manual_review_quantity_refresh = False
        delivery = getattr(self.window, "task_delivery_plugin", None)
        if delivery is not None and self.context.load_config().get("daily_quantity_sheet_url"):
            delivery.refresh_daily_quantity()

    def on_review_status_changed(self, result):
        passed = result.get("passed", [])
        needs_changes = result.get("needs_changes", [])
        self.on_review_status_snapshot(result.get("snapshot"))
        delivery = getattr(self.window, "task_delivery_plugin", None)
        if delivery is not None:
            delivery.refresh_daily_quantity_review_queue()
        if (result.get("items")
                and self.context.load_config().get("daily_quantity_sheet_url")):
            if delivery is not None:
                delivery.refresh_daily_quantity()
        if needs_changes:
            names = "、".join(
                str(item.get("name") or "未命名视频")
                for item in needs_changes[:3]
            )
            suffix = " 等" if len(needs_changes) > 3 else ""
            self.context.notify(
                "有视频需要修改",
                f"{len(needs_changes)} 个审核视频需要修改：{names}{suffix}",
                critical=True,
            )
            self.context.log(f"{len(needs_changes)} 个视频需要修改。")
        if passed:
            admins = {str(item.get("admin") or "未填写管理员") for item in passed}
            self.context.notify(
                "审核通过，待发送",
                f"{len(passed)} 个视频已通过，请发给对应的 {len(admins)} 位管理员。",
            )
            self.context.log(
                f"{len(passed)} 个视频已通过，待发给 {len(admins)} 位管理员。"
            )

    def on_review_status_monitor_finished(self, thread=None):
        thread = thread or self.review_status_thread
        if thread is None or self.review_status_thread is not thread:
            return
        if not thread.wait(0):
            QtCore.QTimer.singleShot(10, lambda current=thread: self.on_review_status_monitor_finished(current))
            return
        self.review_status_thread = None
        if thread is not None and thread.single_pass and self._manual_review_quantity_refresh:
            self._manual_review_quantity_refresh = False
            self.context.log("手动审核检查未完成；每日数量没有刷新，请查看审核监控错误日志。")
        if thread is not None and thread.single_pass and self.review_status_state == "运行中":
            self.review_status_state = "手动检查完成"
        self._sync_compatibility_state()
        if thread is not None:
            thread.deleteLater()
