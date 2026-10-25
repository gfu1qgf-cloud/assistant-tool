import os
import sys
import gc

# Dispatch without private storage/configuration initialization or Qt imports.
if len(sys.argv) == 2 and sys.argv[1] == '--node-python-worker':
    from model.node_workflow.code_worker import main as node_code_main
    sys.exit(node_code_main())

from app_paths import APP_ROOT
from model.OfflineMode import configure_offline, is_offline, install_qt_network_guard

# Install before Qt, cloud SDKs, models and plugins (also in Resolve workers).
_protected_start = (APP_ROOT / 'storage_bootstrap.json').exists() or '--configure-veracrypt' in sys.argv
# Never read the former private configuration before selecting the bound VC.
configure_offline(APP_ROOT / ('__vc_startup_no_config__' if _protected_start else 'config.json'))

# The frozen executable doubles as the isolated Resolve worker. Dispatch
# before importing Qt/ML modules so one worker never starts the GUI.
if len(sys.argv) >= 3 and sys.argv[1] == "--davinci-worker":
    from davinci_remote_worker import main as davinci_worker_main

    sys.exit(davinci_worker_main(sys.argv[2:]))

# Keep legacy relative paths stable in source runs and PyInstaller builds.
os.chdir(APP_ROOT)

# 1. 解决 OpenMP 冲突 (常见于 PyTorch/Whisper)
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from qt_compat import QtCore, QtGui, QtNetwork, QtWidgets

install_qt_network_guard(QtCore, QtGui, QtNetwork)

def main():
    app = QtWidgets.QApplication(sys.argv)
    storage = None
    try:
        from PYUI.first_run_pyui import configure_first_use
        first_use = configure_first_use(APP_ROOT, force_vc='--configure-veracrypt' in sys.argv)
        if first_use is None:
            return 0
    except Exception as error:
        QtWidgets.QMessageBox.critical(None, '首次配置未就绪', str(error))
        return 2
    try:
        from PYUI.protected_storage_pyui import configure_startup
        storage = configure_startup(APP_ROOT,
            force='--configure-veracrypt' in sys.argv or first_use.mode == 'veracrypt',
            initial_config=first_use.initial_config)
    except Exception as error:
        # No private logger, old config or plugin has been initialized yet.
        QtWidgets.QMessageBox.critical(None, '加密存储未就绪', str(error))
        return 2
    from app_paths import DATA_ROOT
    configure_offline(DATA_ROOT / 'config.json')
    # The first-use wizard / selected VC may have enabled offline mode after
    # the early guard was installed. This call is idempotent.
    install_qt_network_guard(QtCore, QtGui, QtNetwork)
    from model.AppLogger import (configure_application_logging, install_exception_hooks,
        install_qt_message_logging, shutdown_application_logging, remove_qt_message_logging)
    from model.ComboBoxWheelGuard import ComboBoxWheelGuard
    from model.AppTheme import apply_configured_ui_theme
    from model.AppIcon import application_icon
    from globalValue import globalValue
    from PYUI.main_pyui import MainDialog
    app_logger, app_log_file = configure_application_logging()
    install_exception_hooks()
    app_logger.info('程序启动，Python：%s', sys.version.replace('\n', ' '))
    model_error = None
    try:
        globalValue.get_whisper_model()
    except Exception as error:
        if not is_offline():
            app_logger.exception('[启动] Whisper 字幕模型初始化失败')
            from PYUI.startup_failure import exit_after_whisper_failure
            return exit_after_whisper_failure(error, app=app, storage=storage,
                config_path=DATA_ROOT / 'config.json', log_path=app_log_file)
        model_error = str(error)
        app_logger.exception("[离线模式] 本地字幕模型未就绪")

    app.setWindowIcon(application_icon(APP_ROOT))
    install_qt_message_logging()
    apply_configured_ui_theme(app, DATA_ROOT / "config.json")
    app.combo_box_wheel_guard = ComboBoxWheelGuard(app)
    app.installEventFilter(app.combo_box_wheel_guard)
    Dialog = MainDialog()
    if storage:
        from PYUI.protected_storage_pyui import StorageMonitor
        Dialog.storage_monitor = StorageMonitor(Dialog, storage)
    Dialog.show()
    if model_error:
        QtCore.QTimer.singleShot(0, lambda: QtWidgets.QMessageBox.warning(
            Dialog, "离线模式：字幕模型不可用",
            "离线模式不会下载模型。本地字幕模型未就绪，其他本地功能仍可使用。\n"
            "请联网下载所选模型后，再使用离线模式。\n\n" + model_error,
        ))
    exit_code = app.exec()
    shutdown_requested = bool(getattr(getattr(Dialog, 'completion_shutdown', None), 'requested', False))
    # Keep QApplication alive until the window and installed event filter have
    # been destroyed. Otherwise SIP may finalize their wrappers in the opposite
    # order during interpreter shutdown, after the Qt application is gone.
    app.removeEventFilter(app.combo_box_wheel_guard)
    if not Dialog.isVisible():
        Dialog.deleteLater()
    app.combo_box_wheel_guard.deleteLater()
    QtCore.QCoreApplication.sendPostedEvents(
        None, QtCore.QEvent.Type.DeferredDelete
    )
    del Dialog
    del app.combo_box_wheel_guard
    gc.collect()
    # Explicitly tear down Qt while Python and logging are still usable,
    # rather than leaving QApplication to interpreter/module destruction.
    from PyQt6 import sip
    sip.delete(app)
    remove_qt_message_logging()
    shutdown_application_logging(close=bool(storage))
    if storage: storage.close()
    if shutdown_requested:
        from model.CompletionShutdown import request_windows_shutdown, report_shutdown_failure
        try:
            request_windows_shutdown()
        except Exception as error:
            report_shutdown_failure(error)
            return 3
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

