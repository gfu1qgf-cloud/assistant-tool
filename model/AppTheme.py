import json
from pathlib import Path

from qt_compat import QtCore, QtGui, QtWidgets


UI_THEME_CONFIG_KEY = "ui_theme"
THEME_LIGHT = "light"
THEME_DARK = "dark"
THEME_SYSTEM = "system"
DEFAULT_UI_THEME = THEME_LIGHT
THEME_CHOICES = (
    ("浅色（推荐）", THEME_LIGHT),
    ("深色", THEME_DARK),
    ("跟随系统", THEME_SYSTEM),
)


def normalize_ui_theme(value):
    value = str(value or "").strip().lower()
    return value if value in {THEME_LIGHT, THEME_DARK, THEME_SYSTEM} else DEFAULT_UI_THEME


def load_configured_ui_theme(config_path):
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return DEFAULT_UI_THEME
    if not isinstance(config, dict):
        return DEFAULT_UI_THEME
    return normalize_ui_theme(config.get(UI_THEME_CONFIG_KEY))


def _set_colors(palette, colors):
    roles = QtGui.QPalette.ColorRole
    for role_name, color in colors.items():
        palette.setColor(getattr(roles, role_name), QtGui.QColor(color))


def _light_palette():
    palette = QtGui.QPalette()
    _set_colors(palette, {
        "Window": "#F5F6F7",
        "WindowText": "#202124",
        "Base": "#FFFFFF",
        "AlternateBase": "#F0F2F5",
        "ToolTipBase": "#FFFDE7",
        "ToolTipText": "#202124",
        "Text": "#202124",
        "Button": "#F1F3F4",
        "ButtonText": "#202124",
        "BrightText": "#FFFFFF",
        "Link": "#0B57D0",
        "LinkVisited": "#681DA8",
        "Highlight": "#0B57D0",
        "HighlightedText": "#FFFFFF",
        "PlaceholderText": "#6B7280",
        "Mid": "#5F6368",
        "Accent": "#0B57D0",
    })
    disabled = QtGui.QPalette.ColorGroup.Disabled
    roles = QtGui.QPalette.ColorRole
    palette.setColor(disabled, roles.Text, QtGui.QColor("#777C85"))
    palette.setColor(disabled, roles.WindowText, QtGui.QColor("#777C85"))
    palette.setColor(disabled, roles.ButtonText, QtGui.QColor("#777C85"))
    return palette


def _dark_palette():
    palette = QtGui.QPalette()
    _set_colors(palette, {
        "Window": "#202124",
        "WindowText": "#F1F3F4",
        "Base": "#151618",
        "AlternateBase": "#292A2D",
        "ToolTipBase": "#303134",
        "ToolTipText": "#F1F3F4",
        "Text": "#F1F3F4",
        "Button": "#303134",
        "ButtonText": "#F1F3F4",
        "BrightText": "#FFFFFF",
        "Link": "#8AB4F8",
        "LinkVisited": "#C58AF9",
        "Highlight": "#8AB4F8",
        "HighlightedText": "#202124",
        "PlaceholderText": "#AEB4BE",
        "Mid": "#BDC1C6",
        "Accent": "#8AB4F8",
    })
    disabled = QtGui.QPalette.ColorGroup.Disabled
    roles = QtGui.QPalette.ColorRole
    palette.setColor(disabled, roles.Text, QtGui.QColor("#9AA0A6"))
    palette.setColor(disabled, roles.WindowText, QtGui.QColor("#9AA0A6"))
    palette.setColor(disabled, roles.ButtonText, QtGui.QColor("#9AA0A6"))
    return palette


def _effective_dark(app, theme):
    if theme == THEME_DARK:
        return True
    if theme == THEME_LIGHT:
        return False
    return app.styleHints().colorScheme() == QtCore.Qt.ColorScheme.Dark


def apply_ui_theme(app, theme):
    """Apply a high-contrast Qt Widgets theme and return its normalized name."""
    if app is None:
        return normalize_ui_theme(theme)
    theme = normalize_ui_theme(theme)
    style = QtWidgets.QStyleFactory.create("Fusion")
    if style is not None:
        app.setStyle(style)

    hints = app.styleHints()
    if theme == THEME_SYSTEM:
        if hasattr(hints, "unsetColorScheme"):
            hints.unsetColorScheme()
    elif hasattr(hints, "setColorScheme"):
        scheme = (
            QtCore.Qt.ColorScheme.Dark
            if theme == THEME_DARK
            else QtCore.Qt.ColorScheme.Light
        )
        hints.setColorScheme(scheme)

    dark = _effective_dark(app, theme)
    app.setPalette(_dark_palette() if dark else _light_palette())
    tooltip_bg = "#303134" if dark else "#FFFDE7"
    tooltip_fg = "#F1F3F4" if dark else "#202124"
    tooltip_border = "#5F6368" if dark else "#AEB4BE"
    app.setStyleSheet(
        "QToolTip {"
        f" color:{tooltip_fg}; background-color:{tooltip_bg};"
        f" border:1px solid {tooltip_border}; padding:3px;"
        "}"
    )
    app.setProperty("uiTheme", theme)
    return theme


def apply_configured_ui_theme(app, config_path):
    return apply_ui_theme(app, load_configured_ui_theme(config_path))

