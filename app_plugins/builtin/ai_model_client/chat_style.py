"""Local chat styling which follows the application's light/dark palette."""
from qt_compat import QtGui, QtWidgets


def apply_chat_style(window):
    dark = QtWidgets.QApplication.palette().color(QtGui.QPalette.ColorRole.Window).lightness() < 128
    window._chat_dark = dark
    font_px = getattr(window, 'chat_font_px', 15)
    c = dict(bg='#181b21', panel='#20242c', side='#14171c', text='#edf0f5',
             muted='#a5afbf', border='#343b48', hover='#30394a', user='#253447', accent='#88b4ff') if dark else dict(
             bg='#f5f7fb', panel='#ffffff', side='#edf1f7', text='#202c40',
             muted='#66758a', border='#dfe5ee', hover='#e4ecfa', user='#edf4ff', accent='#315dd8')
    c['font_px'] = font_px
    window.setStyleSheet('''
        QDialog#aiChatWindow { background: %(bg)s; color: %(text)s; }
        #chatSidebar { background: %(side)s; border-right: 1px solid %(border)s; }
        #sidebarBrand { color: %(muted)s; font-size: 14px; font-weight: 600; padding: 0 4px 10px; }
        #chatHistory { background: transparent; border: none; outline: none; }
        #chatHistory::item { padding: 12px 10px; border-radius: 9px; margin: 3px 0; color: %(text)s; }
        #chatHistory::item:selected { background: %(hover)s; color: %(text)s; }
        #chatHistory::item:hover { background: %(hover)s; }
        #chatHeader { background: %(panel)s; border-bottom: 1px solid %(border)s; }
        #conversationTitle { font-size: 18px; font-weight: 600; color: %(text)s; }
        #chatCanvas, #chatTranscript { background: %(bg)s; }
        #messageCard { background: %(panel)s; border: 1px solid %(border)s; border-radius: 13px; }
        #messageCard[speaker="user"] { background: %(user)s; }
        #messageCard QTextBrowser { background: transparent; border: none; color: %(text)s; font-size: %(font_px)spx; selection-background-color: %(hover)s; }
        #messageTitle { font-weight: 600; color: %(text)s; }
        #chatAvatar { background: %(hover)s; color: %(accent)s; border-radius: 8px; font-size: 12px; font-weight: 600; }
        #muted { color: %(muted)s; font-size: 12px; }
        #emptyChat { color: %(muted)s; font-size: 19px; }
        #chatComposer { background: %(panel)s; border-top: 1px solid %(border)s; }
        #chatInput { background: %(panel)s; color: %(text)s; border: none; padding: 6px; font-size: %(font_px)spx; }
        #chatStatus { background: %(panel)s; color: %(muted)s; padding: 6px 20px; font-size: 12px; border-top: 1px solid %(border)s; }
        #aiChatWindow QPushButton, #aiChatWindow QToolButton { padding: 7px 10px; border: 1px solid %(border)s; border-radius: 7px; background: %(panel)s; color: %(text)s; }
        #aiChatWindow QPushButton:hover, #aiChatWindow QToolButton:hover { background: %(hover)s; }
        #aiChatWindow QPushButton:disabled, #aiChatWindow QToolButton:disabled { color: %(muted)s; }
        #aiChatWindow QPushButton#sendChat { background: %(accent)s; color: %(panel)s; border: none; padding: 9px 20px; font-weight: 600; }
        #aiChatWindow QPushButton#newChat { text-align: left; padding: 11px; font-weight: 600; }
        #aiChatWindow QComboBox, #aiChatWindow QLineEdit { padding: 7px; border: 1px solid %(border)s; border-radius: 7px; background: %(panel)s; color: %(text)s; }
        #aiChatWindow QScrollBar:vertical { width: 10px; background: transparent; }
        #aiChatWindow QScrollBar::handle:vertical { background: %(border)s; border-radius: 4px; min-height: 28px; }
        #aiChatWindow QScrollBar::add-line:vertical, #aiChatWindow QScrollBar::sub-line:vertical { height: 0; }
    ''' % c)
