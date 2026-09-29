import logging
import os
from pathlib import Path

from qt_compat import QtCore, QtWidgets
from qt_compat import QMessageBox

from app_plugins.api import (
    MAIN_MENU,
    TASK_CONTEXT_MENU,
    TOOLS_MENU,
    PluginCommand,
    PluginSettingsPage,
)

from .engine import (
    SMART_VIDEO_EDITOR_CONFIG_KEY,
    SMART_VIDEO_PENDING_CONFIG_KEY,
    SmartVideoEditorThread,
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
    required_api_version = 1

    def __init__(self):
        self.context = None
        self.settings = normalize_smart_video_editor_settings({})
        self.pending_reviews = []
        self.worker = None
        self.phase = None
        self.result = None
        self.error = None
        self._settings_dirty = False

    @property
    def parent(self):
        return self.context.parent_widget

    def register(self, context):
        self.context = context
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
            locations=frozenset({MAIN_MENU, TASK_CONTEXT_MENU}),
            tooltip="使用任务语音文案核对视频片段、压缩过长气口，并生成对应 SRT",
            order=40,
            enabled=lambda rows: bool(rows),
        ))
        context.register_command(PluginCommand(
            command_id="breath_cut",
            title="剪辑气口…",
            callback=self.start_breath_editor,
            locations=frozenset({MAIN_MENU, TASK_CONTEXT_MENU}),
            tooltip="不依赖任务文案，按实际音量检测气口并在时间线上预览后导出",
            order=41,
            enabled=lambda _rows: True,
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

    def start(self):
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
        if self.worker is None or not self.worker.isRunning():
            return True, ""
        self.worker.requestInterruption()
        return (
            False,
            "已请求停止视频处理。正在进行的分析或编码步骤结束后即可关闭程序；"
            "原视频不会被改动。",
        )

    def stop(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.requestInterruption()

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

    def _is_running(self, title="视频处理"):
        if self.worker is None or not self.worker.isRunning():
            return False
        QMessageBox.information(
            self.parent,
            title,
            "智能剪辑或气口剪辑正在分析/导出，请等待当前任务完成。",
        )
        return True

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
        if self._is_running():
            return
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
        if self._is_running("剪辑气口"):
            return
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
        # Standalone mode is explicitly for removing pauses.  Enable internal
        # pause compression for this session only; the visible timeline controls
        # still let the user turn it off before export.
        settings = dict(settings)
        settings["compress_internal_pauses"] = True
        settings["internal_pause_mode"] = "experimental"
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
        if self._is_running("待处理智能剪辑"):
            return
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
        self,
        phase,
        settings,
        jobs=None,
        model=None,
        model_loader=None,
        bundle=None,
        files=None,
    ):
        thread = SmartVideoEditorThread(
            phase,
            settings,
            jobs=jobs,
            model=model,
            model_loader=model_loader,
            bundle=bundle,
            files=files,
            parent=self.parent,
        )
        thread.log.connect(self._on_log)
        thread.completed.connect(self._on_completed)
        thread.failed.connect(self._on_failed)
        thread.finished.connect(self._on_finished)
        self.worker = thread
        self.phase = phase
        self.result = None
        self.error = None
        thread.start()

    def _on_log(self, message):
        self.context.log(message)

    def _on_completed(self, result):
        self.result = result

    def _on_failed(self, message, details):
        self.error = (message, details)
        prefix = "气口剪辑错误" if str(self.phase).startswith("breath_") else "智能剪辑错误"
        self.context.log(
            f"[{prefix}] {message}\n{details}", level=logging.ERROR
        )

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

    def _on_finished(self):
        thread = self.worker
        phase = self.phase
        result = self.result
        error = self.error
        self.worker = None
        self.phase = None
        self.result = None
        self.error = None
        if thread is not None:
            thread.deleteLater()
        if error is not None:
            title = "气口剪辑失败" if str(phase).startswith("breath_") else "智能剪辑失败"
            QMessageBox.critical(
                self.parent,
                title,
                f"{error[0]}\n\n详细堆栈已写入程序日志。",
            )
            return
        if result is None:
            return
        if phase == "analyze":
            QtCore.QTimer.singleShot(
                0, lambda value=result: self._handle_analysis(value)
            )
        elif phase == "breath_analyze":
            QtCore.QTimer.singleShot(
                0, lambda value=result: self._handle_breath_analysis(value)
            )
        elif phase == "breath_export":
            self._handle_breath_export_result(result)
        else:
            self._handle_export_result(result)

    def _handle_breath_analysis(self, bundle):
        summary = bundle.get("summary", {})
        self.context.log(
            f"[气口剪辑] 分析完成：{summary.get('file_count', 0)} 个视频，"
            f"计划删除约 {summary.get('removed_seconds', 0)} 秒。"
        )
        reviewed = BreathCutReviewDialog.get_reviewed_bundle(
            bundle, self.parent
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

    def _handle_breath_export_result(self, result):
        completed = result.get("completed", [])
        failed = result.get("failed", [])
        skipped = result.get("skipped", [])
        self._log_export_failures("气口剪辑", failed)
        self.context.log(
            f"[气口剪辑] 导出结束：成功 {len(completed)}，"
            f"跳过 {len(skipped)}，失败 {len(failed)}。",
            level=logging.ERROR if failed else logging.INFO,
        )
        BreathCutResultDialog(result, self.parent).exec()

    def _remember_pending_reviews(self, bundle):
        self.pending_reviews = update_smart_video_pending_reviews(
            self.pending_reviews, bundle
        )
        self._update_pending_command()
        self.context.save_config()

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

    def _handle_analysis(self, bundle):
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
            self.settings or bundle.get("settings")
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
        if summary.get("needs_review") or not settings.get("auto_export_clean"):
            reviewed = SmartVideoReviewDialog.get_reviewed_bundle(
                bundle,
                self.parent,
                save_subtitle_defaults=self._save_timeline_subtitle_defaults,
            )
            if reviewed is None:
                self._remember_pending_reviews(bundle)
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
        if not self._model_ready(settings):
            return
        self.context.log("[智能剪辑] 核对已确认，开始生成视频与 SRT。")
        self._start_worker(
            "export",
            settings,
            bundle=bundle,
            model_loader=self.context.whisper_model,
        )

    def _handle_export_result(self, result):
        completed = result.get("completed", [])
        failed = result.get("failed", [])
        skipped = result.get("skipped", [])
        self._log_export_failures("智能剪辑", failed)
        self.context.log(
            f"[智能剪辑] 导出结束：成功 {len(completed)}，"
            f"缺段暂缓 {len(skipped)}，失败 {len(failed)}。",
            level=logging.ERROR if failed else logging.INFO,
        )
        SmartVideoExportResultDialog(result, self.parent).exec()
