"""Original ORB video assignment worker, extracted without algorithm changes."""
import os
import traceback
from qt_compat import QtCore, pyqtSignal
from model.AppDataStorage import data_io
from model.VideoHelper import FeatureMatcher


class VideoAssignmentThread(QtCore.QThread):
    log = pyqtSignal(str)
    completed = pyqtSignal(int, int)
    failed = pyqtSignal(str, str)

    def __init__(self, today_dir, video_root_dir, match_ratio_threshold=0.03, parent=None):
        super().__init__(parent)
        self.today_dir = str(today_dir)
        self.video_root_dir = str(video_root_dir)
        self.match_ratio_threshold = float(match_ratio_threshold)
        self._interruption_requested = False

    def requestInterruption(self):
        self._interruption_requested = True
        super().requestInterruption()

    def isInterruptionRequested(self):
        return self._interruption_requested or super().isInterruptionRequested()

    def run(self):
        try:
            if self.isInterruptionRequested():
                self.log.emit("视频分拣已在启动前取消。")
                self.completed.emit(0, 0)
                return

            self.log.emit(f"正在建立图片特征库 (ORB模式): {self.today_dir} ...")
            matcher = FeatureMatcher()

            img_db = FeatureMatcher.scan_images_recursively(self.today_dir, matcher)
            if self.isInterruptionRequested():
                self.log.emit("视频分拣已由用户取消。")
                self.completed.emit(0, 0)
                return

            if not img_db:
                self.log.emit(f"任务目录下未找到有效图片特征库: {self.today_dir}")
                self.completed.emit(0, 0)
                return

            self.log.emit(f"开始扫描视频: {self.video_root_dir} ...")

            VIDEO_EXTS = ['.mp4', '.mkv', '.avi', '.mov', '.flv', '.wmv', '.ts']
            processed = 0
            moved = 0
            cancelled = False

            for root, dirs, files in os.walk(self.video_root_dir):
                if self.isInterruptionRequested():
                    cancelled = True
                    break

                for file in files:
                    if self.isInterruptionRequested():
                        cancelled = True
                        break

                    file_path = os.path.join(root, file)
                    ext = os.path.splitext(file)[1].lower()
                    if ext not in VIDEO_EXTS:
                        continue

                    self.log.emit(f"正在分析: {file} ...")

                    frame = FeatureMatcher.get_video_frame_clean(file_path)
                    if frame is None:
                        continue

                    if self.isInterruptionRequested():
                        cancelled = True
                        break

                    _, video_desc = matcher.get_features(frame)
                    if video_desc is None:
                        continue

                    best_score = 0.0
                    best_match = None

                    for img_data in img_db:
                        if self.isInterruptionRequested():
                            cancelled = True
                            break
                        score = matcher.match(img_data['desc'], video_desc)
                        if score > best_score:
                            best_score = score
                            best_match = img_data

                    if cancelled:
                        break

                    if best_score >= self.match_ratio_threshold:
                        target_dir = best_match['folder']
                        if os.path.abspath(root) == os.path.abspath(target_dir):
                            processed += 1
                            continue

                        target_path = os.path.join(target_dir, file)
                        if os.path.exists(target_path):
                            base, ex = os.path.splitext(file)
                            target_path = os.path.join(target_dir, f"{base}_match{ex}")
                            suffix = 2
                            while os.path.exists(target_path):
                                target_path = os.path.join(target_dir, f"{base}_match{suffix}{ex}")
                                suffix += 1

                        try:
                            self.log.emit(
                                f"\n[匹配成功] {file}\n"
                                f"         目标图片: {best_match['name']}\n"
                                f"         特征重合度: {best_score:.2%}"
                            )
                            data_io.move(file_path, target_path)
                            moved += 1
                        except Exception as e:
                            self.log.emit(f"\n[错误] 移动文件失败: {e}")

                    processed += 1

            if cancelled or self.isInterruptionRequested():
                self.log.emit("\n" + "=" * 30)
                self.log.emit(f"视频分拣已被用户中止。已处理: {moved}/{processed}")
            else:
                self.log.emit("\n" + "=" * 30)
                self.log.emit(f"处理完成。归类: {moved}/{processed}")

            self.completed.emit(moved, processed)

        except (Exception, SystemExit) as error:
            self.failed.emit(
                f"{type(error).__name__}: {error}",
                traceback.format_exc(),
            )
