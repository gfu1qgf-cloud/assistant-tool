import logging
import os
import weakref
from pathlib import Path

from qt_compat import QtWidgets
from qt_compat import QMessageBox

from app_plugins.api import (
    MAIN_MENU,
    TASK_CONTEXT_MENU,
    TOOLS_MENU,
    PluginCommand,
    PluginSettingsPage,
)

from .engine import (
    BREATH_SETTINGS_KEYS,
    SMART_VIDEO_EDITOR_CONFIG_KEY,
    SMART_VIDEO_PENDING_CONFIG_KEY,
    discover_task_videos,
    format_smart_video_export_blockers,
    normalize_smart_video_editor_settings,
    normalize_smart_video_pending_reviews,
    smart_video_jobs_require_model,
    smart_video_export_blockers,
    summarize_smart_video_export_blockers,
    update_smart_video_pending_reviews,
)
from .settings import SmartVideoEditorSettingsPage
from .background import analysis_resources, render_inputs, render_resources, refresh_inputs, run_smart_video_job
from .breath_editor import (
    BreathCutResultDialog,
    BreathCutReviewDialog,
    BreathCutSourceDialog,
)
from .ui import (
    SmartVideoExportResultDialog,
    SmartVideoPendingDialog,
    SmartVideoReviewDialog,
    SmartVideoSourceDialog,
)


class SmartVideoEditorPlugin:
    """Own the complete smart-edit workflow outside the main window.

    The host supplies task selection, project paths, subtitle preferences,
    logging and the shared Whisper model.  Analysis, review state, worker
    lifetime and export UI stay inside this plugin.
    """

    plugin_id = "smart_video_editor"
    display_name = "智能剪辑"
    version = "1.2"
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.settings = normalize_smart_video_editor_settings({})
        self.pending_reviews = []
        self._analysis_jobs = {}
        self._review_open = False
        self._settings_dirty = False
        self._render_handles = {}
        self._front_results = None

    @property
    def parent(self):
        return self.context.parent_widget

    def register(self, context):
        self.context = context
        register_node = getattr(context, "register_node", None)
        if callable(register_node):
            from .workflow_nodes import edit_node
            register_node(edit_node(self))
        config = context.load_config()
        self.settings = normalize_smart_video_editor_settings(
            config.get(SMART_VIDEO_EDITOR_CONFIG_KEY)
        )
        self.pending_reviews = normalize_smart_video_pending_reviews(
            config.get(SMART_VIDEO_PENDING_CONFIG_KEY)
        )
        context.register_command(PluginCommand(
            command_id="start",
            title="智能剪辑并生成 SRT…",
            callback=self.start_editor,
            locations=frozenset({TASK_CONTEXT_MENU}),
            tooltip="使用任务语音文案核对视频片段、压缩过长气口，并生成对应 SRT",
            order=40,
            enabled=lambda rows: bool(rows),
        ))
        context.register_command(PluginCommand(
            command_id="breath_cut",
            title="剪辑气口…",
            callback=self.start_breath_editor,
            locations=frozenset({TASK_CONTEXT_MENU}),
            tooltip="不依赖任务文案，按实际音量检测气口并在时间线上预览后导出",
            order=41,
            enabled=lambda _rows: True,
        ))
        context.register_command(PluginCommand(
            command_id="about",
            title="智能剪辑",
            callback=self.show_about,
            locations=frozenset({MAIN_MENU}),
            tooltip="查看插件版本；从任务列表右键开始剪辑",
            order=40,
        ))
        context.register_command(PluginCommand(
            command_id="pending",
            title=self._pending_title(),
            callback=self.open_pending_reviews,
            locations=frozenset({TOOLS_MENU}),
            tooltip="查看上次未处理或暂缓的缺段任务，并重新加载视频分析",
            order=80,
        ))
        context.register_settings_page(PluginSettingsPage(
            page_id="settings",
            title="智能剪辑",
            factory=SmartVideoEditorSettingsPage,
            order=80,
        ))

    def show_about(self, _rows=()):
        QMessageBox.information(self.parent, "关于智能剪辑",
            f"{self.display_name} · 插件版本 {self.version}\n\n"
            "核对任务视频、剪辑气口并生成字幕。\n"
            "使用入口：选中任务 → 右键 → 智能剪辑。")

    def start(self):
        if self._front_results is not None:
            self._front_results.resume()
        self._update_pending_command()

    def apply_settings(self, config):
        self.settings = normalize_smart_video_editor_settings(
            config.get(SMART_VIDEO_EDITOR_CONFIG_KEY)
        )
        self._settings_dirty = False
        return True

    def update_config(self, config):
        # Main-window autosaves (including pending-review changes) must not
        # replace a newer settings-dialog choice with stale runtime settings.
        if self._settings_dirty or SMART_VIDEO_EDITOR_CONFIG_KEY not in config:
            config[SMART_VIDEO_EDITOR_CONFIG_KEY] = (
                normalize_smart_video_editor_settings(self.settings)
            )
        config[SMART_VIDEO_PENDING_CONFIG_KEY] = (
            normalize_smart_video_pending_reviews(self.pending_reviews)
        )
        return config

    def can_close(self):
        # A close probe must not disable presentation if another plugin or
        # foreground service subsequently refuses to close the application.
        if not self._active_handles():
            return True, ""
        self.stop()
        if not self._active_handles():
            return True, ""
        return (
            False,
            "已请求停止视频处理。正在进行的分析或编码步骤结束后即可关闭程序；"
            "原视频不会被改动。",
        )

    def stop(self):
        if self._front_results is not None:
            self._front_results.stop()
        handles = self._active_handles()
        if handles:
            # Cancel queued jobs as one operation, so canceling one cannot
            # dispatch the next batch while the application is shutting down.
            self.context.background_tasks.cancel_many([handle.task_id for handle in handles])

    def _active_handles(self):
        handles = [state["handle"] for state in self._analysis_jobs.values()]
        return [handle for handle in handles + list(self._render_handles.values()) if handle.isRunning()]

    def _pending_title(self):
        count = len(self.pending_reviews)
        return f"待处理智能剪辑（{count}）" if count else "待处理智能剪辑"

    def _update_pending_command(self):
        self.context.update_command("pending", title=self._pending_title())

    def _effective_settings(self):
        config = self.context.load_config()
        settings = normalize_smart_video_editor_settings(
            config.get(SMART_VIDEO_EDITOR_CONFIG_KEY, self.settings)
        )
        if settings.get("use_main_subtitle_settings", True):
            settings.update(self.context.subtitle_generation_settings())
        if not settings.get("ffmpeg_path"):
            settings["ffmpeg_path"] = str(
                config.get("shana_ffmpeg_path") or ""
            ).strip()
        self.settings = settings
        return settings

    def _model_ready(self, settings):
        loaded = self.context.loaded_whisper_model_name()
        selected = str(settings.get("whisper_model_size") or "base")
        if not loaded or loaded == selected:
            return True
        message = (
            f"当前运行的是 {loaded}，设置选择的是 {selected}。"
            "为避免模型切换时程序崩溃，请先重启程序；本次没有开始处理。"
        )
        self.context.log(f"[智能剪辑] {message}")
        QMessageBox.information(self.parent, "模型切换需重启", message)
        return False

    def start_editor(self, rows=()):
        selected_rows = list(rows or self.context.selected_task_rows())
        targets = self.context.task_targets(selected_rows)
        if not targets:
            return
        settings = self._effective_settings()
        jobs = []
        for target in targets:
            task = target["task"]
            task_dir = Path(target["target_dir"])
            sources = discover_task_videos(task_dir, settings)
            task_id = str(getattr(task, "task_id", "") or "")
            task_name = str(getattr(task, "task_name", "") or "").strip()
            jobs.append({
                "task_id": task_id,
                "task_name": task_name,
                "label": target.get("label") or (
                    f"{task_id} | {task_name}" if task_name else task_id
                ),
                "task_dir": str(task_dir),
                "script": str(getattr(task, "task_audio_text", "") or "").strip(),
                "language": self.context.task_language(task),
                "sources": [str(path) for path in sources],
            })
        self._confirm_and_analyze(jobs, settings)

    def start_breath_editor(self, rows=()):
        initial_sources = []
        if rows:
            targets = self.context.task_targets(list(rows))
            settings = self._effective_settings()
            for target in targets:
                initial_sources.extend(
                    str(path)
                    for path in discover_task_videos(
                        Path(target["target_dir"]), settings
                    )
                )
        else:
            settings = self._effective_settings()
        selected = BreathCutSourceDialog.get_sources(initial_sources, self.parent)
        if selected is None:
            self.context.log("[气口剪辑] 用户取消了视频选择。")
            return
        # Keep the standalone first-use behavior, but never override a saved
        # switch (including a deliberately saved False).
        settings = dict(settings)
        saved_settings = self.context.load_config().get(SMART_VIDEO_EDITOR_CONFIG_KEY)
        if not isinstance(saved_settings, dict) or "compress_internal_pauses" not in saved_settings:
            settings["compress_internal_pauses"] = True
            settings["internal_pause_mode"] = "enabled"
        self.context.log(f"[气口剪辑] 开始分析 {len(selected)} 个视频。")
        self._start_worker(
            "breath_analyze", settings, files=selected
        )

    def _confirm_and_analyze(self, jobs, settings):
        if not jobs:
            QMessageBox.information(self.parent, "智能剪辑", "没有可以分析的任务。")
            return
        if not self._model_ready(settings):
            return
        selected_jobs = SmartVideoSourceDialog.get_jobs(jobs, self.parent)
        if selected_jobs is None:
            self.context.log("[智能剪辑] 用户取消了视频片段选择。")
            return
        model_loader = None
        if smart_video_jobs_require_model(selected_jobs, settings):
            model_loader = self.context.whisper_model
        else:
            self.context.log(
                "[智能剪辑] 所选视频缓存完整，无需重新加载 Whisper 模型。"
            )
        self.settings = settings
        self.context.log(
            f"[智能剪辑] 使用 {settings.get('whisper_model_size', 'base')} 模型，"
            f"开始核对 {len(selected_jobs)} 个任务的视频片段。"
        )
        self._start_worker(
            "analyze",
            settings,
            jobs=selected_jobs,
            model_loader=model_loader,
        )

    def open_pending_reviews(self, _rows=()):
        while True:
            if not self.pending_reviews:
                QMessageBox.information(
                    self.parent, "待处理智能剪辑", "目前没有待处理的缺段任务。"
                )
                return
            dialog = SmartVideoPendingDialog(self.pending_reviews, self.parent)
            if dialog.exec() != QtWidgets.QDialog.Accepted:
                return
            selected = list(dialog.selected_records)
            if dialog.action == "remove":
                remove_ids = {
                    str(item.get("record_id") or "") for item in selected
                }
                self.pending_reviews = [
                    item for item in self.pending_reviews
                    if str(item.get("record_id") or "") not in remove_ids
                ]
                self._update_pending_command()
                self.context.save_config()
                continue
            if dialog.action != "reanalyze":
                return

            settings = self._effective_settings()
            jobs = []
            for record in selected:
                task_dir = Path(record.get("task_dir") or "")
                sources = []
                seen = set()
                discovered = [
                    str(path) for path in discover_task_videos(task_dir, settings)
                ]
                for source in list(record.get("sources", [])) + discovered:
                    key = os.path.normcase(os.path.abspath(str(source)))
                    if key in seen or not Path(source).is_file():
                        continue
                    seen.add(key)
                    sources.append(str(source))
                jobs.append({
                    "task_id": str(record.get("task_id") or ""),
                    "task_name": str(record.get("task_name") or ""),
                    "label": str(
                        record.get("label") or record.get("task_id") or "任务"
                    ),
                    "task_dir": str(task_dir),
                    "script": str(record.get("script") or ""),
                    "language": str(record.get("language") or ""),
                    "sources": sources,
                })
            self._confirm_and_analyze(jobs, settings)
            return

    def _start_worker(
        self, phase, settings, jobs=None, model=None, model_loader=None,
        bundle=None, files=None, *, analysis_receiver=None,
    ):
        try:
            return self._submit_worker(phase, settings, jobs, model, model_loader, bundle, files,
                                       analysis_receiver=analysis_receiver)
        except Exception as error:
            # This method is also called by Qt slots after a review dialog closes.
            # A missing source or invalid payload must not escape into Qt.
            logging.getLogger(__name__).exception("无法提交视频后台任务")
            self.context.log(f"视频任务未启动：{type(error).__name__}: {error}", logging.ERROR)
            self.context.notify("视频任务未启动", str(error), critical=True)
            return None

    def _submit_worker(
        self,
        phase,
        settings,
        jobs=None,
        model=None,
        model_loader=None,
        bundle=None,
        files=None,
        *, analysis_receiver=None,
    ):
        rendering = phase in {"export", "breath_export"}
        if analysis_receiver is not None and (phase != "analyze" or not callable(analysis_receiver)):
            raise ValueError("原窗口刷新回调只用于智能剪辑分析，且必须可调用。")
        if not self.context.background_tasks.is_accepting():
            self.context.log("程序正在退出，未提交新视频任务；已核对的智能剪辑可从待处理列表重新打开。")
            return None
        load_model = bool(model_loader) and not (phase == "export" and settings.get("export_mode") == "segments")
        resources = render_resources(bundle) if rendering else analysis_resources(phase, settings, jobs, files)
        count = len((bundle or {}).get("tasks", [])) if rendering else len(jobs or files or [])
        label = {"analyze": "智能剪辑分析", "breath_analyze": "气口分析",
                 "export": "智能剪辑导出", "breath_export": "气口剪辑导出"}[phase]
        if analysis_receiver is not None:
            label = "智能剪辑刷新"
        items = (bundle or {}).get("tasks", []) if rendering else jobs or files or []
        names = [str(item.get("task_id") or item.get("label") or "任务") if isinstance(item, dict)
                 else Path(item).stem for item in items[:3]]
        description = "、".join(names)[:60] + ("…" if len(items) > 3 else "")
        handle = self.context.submit_background_task(
            title=f"{label} · {count} 个任务" + (f" · {description}" if description else ""), runner=run_smart_video_job,
            payload={"phase": phase, "settings": settings, "jobs": jobs or [],
                     "files": files or [], "bundle": bundle, "load_model": load_model,
                     "source_versions": render_inputs(bundle) if rendering else refresh_inputs(jobs)},
            # Only colliding output directories serialize. Shared Whisper is
            # guarded inside native recognition/alignment, not during encoding.
            kind="media", resources=resources, retryable=False)
        if rendering:
            self._render_handles[handle.task_id] = handle
            handle.completed.connect(lambda result, job=handle, mode=phase: self._render_completed(job, mode, result))
            handle.failed.connect(lambda message: self.context.notify("视频导出未完成", message, critical=True))
            handle.finished.connect(lambda key=handle.task_id: self._render_handles.pop(key, None))
            self.context.log(f"[{label}] 已加入主程序后台任务，可以关闭核对窗口并继续下一批。")
            return handle
        self._analysis_jobs[handle.task_id] = {"handle": handle, "phase": phase, "result": None,
                                             "receiver": analysis_receiver}
        handle.completed.connect(lambda result, key=handle.task_id: self._analysis_completed(key, result))
        handle.failed.connect(lambda message, job=handle, mode=phase: self._analysis_failed(job, mode, message))
        handle.finished.connect(lambda key=handle.task_id: self._analysis_finished(key))
        self.context.log(f"[{label}] 已加入后台队列；独立目录可并行，识别时等待共享字幕模型。排队／执行期间均可取消。")
        return handle

    def _render_completed(self, handle, phase, result):
        try:
            if phase == "breath_export":
                self._handle_breath_export_result(result, show_dialog=False)
                factory = BreathCutResultDialog
            else:
                self._handle_export_result(result, show_dialog=False)
                factory = SmartVideoExportResultDialog
            handle.set_result_handler(lambda: self._foreground_results().show(
                handle.task_id, lambda: factory(result, self.parent).exec()), pending=True)
            self._foreground_results().enqueue(handle.task_id, handle.show_result)
            if not result.get("failed"):
                self.context.notify("视频导出完成", f"成功 {len(result.get('completed', []))}，"
                                    f"暂缓 {len(result.get('skipped', []))}；结果窗口将自动打开。")
        except Exception:
            logging.getLogger(__name__).exception("处理后台视频结果失败")
            self.context.log("视频任务已返回，但结果面板更新失败，请检查后台任务详情。", logging.ERROR)

    def _on_log(self, message):
        self.context.log(message)

    def _analysis_completed(self, task_id, result):
        state = self._analysis_jobs.get(task_id)
        if state is not None:
            state["result"] = result

    def _analysis_failed(self, handle, phase, message):
        if self.context.background_tasks.get(handle.task_id)["status"] == "canceled":
            self.context.log("[视频分析] 已取消本批任务，未打开核对窗口、未提交导出。")
            return
        prefix = "气口剪辑错误" if phase == "breath_analyze" else "智能剪辑错误"
        self.context.log(f"[{prefix}] {message}\n详细堆栈见后台任务详情与日志。", level=logging.ERROR)

    def _log_export_failures(self, label, failed):
        for item in failed:
            task = str(item.get("task_id") or item.get("label") or "未知任务")
            task_dir = str(item.get("task_dir") or "")
            detail = str(item.get("traceback") or "").strip()
            message = f"[{label}导出失败] 任务 {task}: {item.get('error') or '未知错误'}"
            if task_dir:
                message += f"\n任务目录：{task_dir}"
            if detail:
                message += f"\n{detail}"
            self.context.log(message, level=logging.ERROR)

    def _analysis_finished(self, task_id):
        state = self._analysis_jobs.pop(task_id, None)
        if state is None:
            return
        handle, phase, result = state["handle"], state["phase"], state["result"]
        receiver = state.get("receiver")
        if self.context.background_tasks.get(task_id)["status"] != "succeeded" or result is None:
            return
        try:
            handle.set_result_handler(lambda: self._foreground_results().show(
                handle.task_id, lambda: self._open_analysis_result(phase, result, force_review=receiver is not None)), pending=True)
            if phase == "analyze":
                self._remember_pending_reviews(result)
                if receiver is not None:
                    try:
                        delivered = bool(receiver(result))
                    except Exception:
                        logging.getLogger(__name__).exception("刷新任务结果无法在原窗口应用")
                        delivered = False
                        self.context.notify("刷新结果未能应用", "分析报告已保存，原核对结果仍保留；关闭旧窗口后可打开新结果。", critical=True)
                    if delivered:
                        handle.set_result_handler(lambda: self._foreground_results().show(
                            handle.task_id, lambda: self._open_analysis_result(phase, result, force_review=True)), pending=False)
                        self.context.log("[智能剪辑] 当前任务已在原核对窗口刷新，其他任务不变。")
                        return
                settings = normalize_smart_video_editor_settings(result.get("settings") or self.settings)
                if receiver is None and self.context.background_tasks.is_accepting() and settings.get("auto_export_clean") \
                        and not result.get("summary", {}).get("needs_review") and not smart_video_export_blockers(result):
                    # Preserve the existing clean-task automatic export setting.
                    # This path cannot open a review dialog, so other batches
                    # may finish safely while the user reviews another result.
                    self._handle_analysis(result)
                    handle.set_result_handler(None)
                    return
            if self.context.background_tasks.is_accepting():
                self._foreground_results().enqueue(handle.task_id, handle.show_result)
                self.context.notify("视频分析完成", "核对界面将自动打开；已有窗口打开时，按完成顺序等待。")
        except Exception:
            logging.getLogger(__name__).exception("保存视频分析结果失败")
            self.context.log("视频分析已完成，但保存核对入口失败，请检查程序日志和任务目录中的分析报告。", logging.ERROR)

    def _open_analysis_result(self, phase, result, *, force_review=False):
        if not self.context.background_tasks.is_accepting() or self._review_open:
            return False
        self._review_open = True
        try:
            if phase == "analyze":
                if force_review:
                    self._handle_analysis(result, force_review=True)
                else:
                    self._handle_analysis(result)
            else:
                self._handle_breath_analysis(result)
        except Exception as error:
            logging.getLogger(__name__).exception("打开视频核对失败")
            self.context.notify("视频核对未完成", str(error), critical=True)
            return False
        finally:
            self._review_open = False
        return True

    def _foreground_results(self):
        if self._front_results is None:
            from .foreground_results import ForegroundResults
            self._front_results = ForegroundResults(self.parent,
                self.context.background_tasks.is_accepting, lambda: self._review_open)
        return self._front_results

    def _handle_breath_analysis(self, bundle):
        if not self.context.background_tasks.is_accepting():
            self.context.log("程序正在退出，本次气口分析未提交导出；原视频未改动。")
            return
        summary = bundle.get("summary", {})
        self.context.log(
            f"[气口剪辑] 分析完成：{summary.get('file_count', 0)} 个视频，"
            f"计划删除约 {summary.get('removed_seconds', 0)} 秒。"
        )
        reviewed = BreathCutReviewDialog.get_reviewed_bundle(
            bundle, self.parent,
            save_breath_defaults=self._save_timeline_breath_defaults,
        )
        if reviewed is None:
            self.context.log("[气口剪辑] 用户取消导出，原视频未改动。")
            return
        self.context.log("[气口剪辑] 时间线已确认，开始导出。")
        self._start_worker(
            "breath_export",
            reviewed.get("settings", self.settings),
            bundle=reviewed,
        )

    def _handle_breath_export_result(self, result, show_dialog=True):
        completed = result.get("completed", [])
        failed = result.get("failed", [])
        skipped = result.get("skipped", [])
        self._log_export_failures("气口剪辑", failed)
        self.context.log(
            f"[气口剪辑] 导出结束：成功 {len(completed)}，"
            f"跳过 {len(skipped)}，失败 {len(failed)}。",
            level=logging.ERROR if failed else logging.INFO,
        )
        if show_dialog:
            BreathCutResultDialog(result, self.parent).exec()

    def _remember_pending_reviews(self, bundle):
        self.pending_reviews = update_smart_video_pending_reviews(
            self.pending_reviews, bundle
        )
        self._update_pending_command()
        self.context.save_config()

    def _save_timeline_breath_defaults(self, values):
        values = values if isinstance(values, dict) else {}
        # Merge the latest saved settings, not an older analysis bundle.
        config = self.context.load_config()
        merged = dict(config.get(SMART_VIDEO_EDITOR_CONFIG_KEY) or self.settings)
        merged.update({key: values[key] for key in BREATH_SETTINGS_KEYS if key in values})
        previous_settings, previous_dirty = self.settings, self._settings_dirty
        self.settings = normalize_smart_video_editor_settings(merged)
        self._settings_dirty = True
        try:
            saved = self.context.save_config()
        except Exception:
            self.settings, self._settings_dirty = previous_settings, previous_dirty
            self.context.log("[智能剪辑] 保存气口默认失败。", level=logging.ERROR)
            raise
        if not saved:
            self.settings, self._settings_dirty = previous_settings, previous_dirty
            return False, "保存失败，请查看程序日志"
        self._settings_dirty = False
        self.context.log("[智能剪辑] 已从时间线保存气口默认参数。")
        return True, "已保存气口默认，下次分析生效；当前任务需点击“检测并应用”更新。"

    def _save_timeline_subtitle_defaults(self, values):
        values = values if isinstance(values, dict) else {}
        merged = dict(self.settings)
        merged.update(values)
        # These values are also written to the host's global subtitle controls,
        # so later smart-editor runs and the regular subtitle generator agree.
        merged["use_main_subtitle_settings"] = True
        self.settings = normalize_smart_video_editor_settings(merged)
        self.context.set_subtitle_generation_settings(self.settings)
        self._settings_dirty = True
        saved = self.context.save_config()
        if saved:
            self._settings_dirty = False
            self.context.log(
                "[智能剪辑] 已从时间线保存全局字幕参数。"
            )
            loaded = self.context.loaded_whisper_model_name()
            selected = self.settings["whisper_model_size"]
            if loaded and loaded != selected:
                return True, f"已保存 {selected}；请重启程序后生效（当前仍为 {loaded}）"
            return True, "已保存为全局默认"
        return False, "保存失败，请查看程序日志"

    def _reanalyze_review_task(self, dialog, task_index):
        """GUI side only: select fresh sources, submit plain data, deliver on GUI signals."""
        from PyQt6 import sip
        from .review_reanalysis import refresh_job
        if not self.context.background_tasks.is_accepting():
            raise RuntimeError("程序正在退出，不能刷新分析。")
        task = dialog.bundle.get("tasks", [])[task_index]
        settings = normalize_smart_video_editor_settings(dialog.bundle.get("settings") or self.settings)
        job = refresh_job(task, settings)
        if not self._model_ready(settings):
            return None
        selected = SmartVideoSourceDialog.get_jobs([job], dialog, notice=(
            "本次只刷新当前任务；确认后重新核对排序、缺段和裁切计划，当前任务的旧人工决定会重置。"
            "其他任务的修改保留。取消选择不更改原结果；原视频不会修改。"))
        if selected is None:
            return None
        # Collect pending edits only AFTER selection was accepted, but before
        # launching a worker. Invalid drafts remain visible for correction.
        dialog.collect()
        loader = self.context.whisper_model if smart_video_jobs_require_model(selected, settings) else None
        reference, generation = weakref.ref(dialog), dialog._task_refresh_generation
        def receive(result):
            view = reference()
            if view is None or sip.isdeleted(view):
                return False
            return view.apply_task_reanalysis(task_index, result, generation)
        return self._start_worker("analyze", settings, jobs=selected, model_loader=loader, analysis_receiver=receive)

    def _handle_analysis(self, bundle, *, force_review=False):
        if not self.context.background_tasks.is_accepting():
            self._remember_pending_reviews(bundle)
            self.context.log("程序正在退出，分析结果已保存到待处理智能剪辑；未开始导出。")
            return
        summary = bundle.get("summary", {})
        self.context.log(
            "[智能剪辑] 核对完成：通过 {green}，需核对 {orange}，"
            "严重异常 {pink}，未匹配文案 {missing} 段；共标出 "
            "{problems} 个具体问题、{cuts} 个自动裁切区间，"
            "自动排除重复片段 {duplicates} 个。".format(
                green=summary.get("green_count", 0),
                orange=summary.get("orange_count", 0),
                pink=summary.get("pink_count", 0),
                missing=summary.get("missing_count", 0),
                problems=summary.get("problem_count", 0),
                cuts=summary.get("cut_decision_count", 0),
                duplicates=summary.get("duplicate_count", 0),
            )
        )
        if summary.get("task_cache_count"):
            self.context.log(
                f"[智能剪辑] 已整项复用 {summary['task_cache_count']} 个任务的分析结果。"
            )
        elif summary.get("transcription_cache_count"):
            self.context.log(
                "[智能剪辑] 已复用 "
                f"{summary['transcription_cache_count']} 个视频的识别缓存，"
                "并按当前文案/参数重新核对。"
            )
        settings = normalize_smart_video_editor_settings(
            bundle.get("settings") or self.settings
        )
        blockers = smart_video_export_blockers(bundle)
        if blockers:
            issue_summary = summarize_smart_video_export_blockers(blockers)
            self.context.log(
                f"[智能剪辑待核对] {issue_summary}，等待人工处理。",
                level=logging.ERROR,
            )
            QMessageBox.warning(
                self.parent,
                "检测到片段问题，需要人工决定",
                f"检测到{issue_summary}。缺段可试听后确认完整或暂缓；"
                "疑似多余片段可在时间线右键排除，或试听后确认保留。\n\n"
                + format_smart_video_export_blockers(blockers, limit=8),
            )
        if force_review or summary.get("needs_review") or not settings.get("auto_export_clean"):
            last_review_bundle = [bundle]
            def keep_latest_review(value):
                last_review_bundle[0] = value
            reviewed = SmartVideoReviewDialog.get_reviewed_bundle(
                bundle,
                self.parent,
                save_subtitle_defaults=self._save_timeline_subtitle_defaults,
                save_breath_defaults=self._save_timeline_breath_defaults,
                reanalyze_task=self._reanalyze_review_task,
                on_closed_bundle=keep_latest_review,
            )
            if reviewed is None:
                # An in-place refresh may already have replaced one task.
                # Closing must not put the pre-refresh task back into pending.
                self._remember_pending_reviews(last_review_bundle[0])
                self.context.log(
                    "[智能剪辑] 已取消导出；分析报告、识别缓存和待处理任务已保留。"
                )
                return
            bundle = reviewed
            settings = normalize_smart_video_editor_settings(
                bundle.get("settings", settings)
            )
            bundle["settings"] = settings
        self._remember_pending_reviews(bundle)
        if settings.get("export_mode") != "segments" and not self._model_ready(settings):
            return
        self.context.log("[智能剪辑] 核对已确认，开始导出分段视频。" if settings.get("export_mode") == "segments"
                         else "[智能剪辑] 核对已确认，开始生成视频与 SRT。")
        self._start_worker(
            "export",
            settings,
            bundle=bundle,
            model_loader=self.context.whisper_model,
        )

    def _handle_export_result(self, result, show_dialog=True):
        completed = result.get("completed", [])
        failed = result.get("failed", [])
        skipped = result.get("skipped", [])
        self._log_export_failures("智能剪辑", failed)
        self.context.log(
            f"[智能剪辑] 导出结束：成功 {len(completed)}，"
            f"缺段暂缓 {len(skipped)}，失败 {len(failed)}。",
            level=logging.ERROR if failed else logging.INFO,
        )
        if show_dialog:
            SmartVideoExportResultDialog(result, self.parent).exec()
