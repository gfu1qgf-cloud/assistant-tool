import os
import sys

# The frozen executable doubles as the isolated Resolve worker. Dispatch
# before importing Qt/ML modules so one worker never starts the GUI.
if len(sys.argv) >= 3 and sys.argv[1] == "--davinci-worker":
    from davinci_remote_worker import main as davinci_worker_main

    sys.exit(davinci_worker_main(sys.argv[2:]))

from app_paths import APP_ROOT

# Keep legacy relative paths stable in source runs and PyInstaller builds.
os.chdir(APP_ROOT)

# 1. 解决 OpenMP 冲突 (常见于 PyTorch/Whisper)
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

from model.AppLogger import (
    configure_application_logging,
    install_exception_hooks,
    shutdown_application_logging,
)

app_logger, app_log_file = configure_application_logging()
install_exception_hooks()
app_logger.info("程序启动，Python：%s", sys.version.replace("\n", " "))

from globalValue import globalValue


from qt_compat import QtWidgets

from model.ComboBoxWheelGuard import ComboBoxWheelGuard
from model.AppTheme import apply_configured_ui_theme


from PYUI.main_pyui import MainDialog



def main():
    globalValue.get_whisper_model()

    app = QtWidgets.QApplication(sys.argv)
    apply_configured_ui_theme(app, APP_ROOT / "config.json")
    app.combo_box_wheel_guard = ComboBoxWheelGuard(app)
    app.installEventFilter(app.combo_box_wheel_guard)
    app.aboutToQuit.connect(shutdown_application_logging)
    Dialog = MainDialog()
    Dialog.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

