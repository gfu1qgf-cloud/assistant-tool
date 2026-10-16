from .audio_splitter import AudioSplitterPlugin
from .ai_model_client import AIModelClientPlugin
from .batch_text_video import BatchTextVideoPlugin
from .video_stitch import VideoStitchPlugin
from .chrome_launcher import ChromeLauncherPlugin
from .codex_account_switcher import CodexAccountSwitcherPlugin
from .cooking_assistant import CookingAssistantPlugin
from .daily_tasks import DailyTasksPlugin
try:
    from .facebook_contact_sheet import FacebookContactSheetPlugin
except ModuleNotFoundError as exc:
    # This experimental built-in can be removed without disabling the host.
    if exc.name != "app_plugins.builtin.facebook_contact_sheet":
        raise
    FacebookContactSheetPlugin = None
from .davinci_remote import DaVinciRemotePlugin
from .inventory import InventoryPlugin
# Legacy imports remain compatible; the host only installs InventoryPlugin.
from .image_classifier import ImageClassifierPlugin
from .material_organizer import MaterialOrganizerPlugin
from .music_ducker import MusicDuckerPlugin
from .smart_video_editor import SmartVideoEditorPlugin
from .smart_image_search import SmartImageSearchPlugin
from .smart_music_search import SmartMusicSearchPlugin
from .smart_search import SmartSearchPlugin
from .video_prompt_assistant import VideoPromptAssistantPlugin
from .script_segmenter import ScriptSegmenterPlugin
from .document_formatter import DocumentFormatterPlugin
from .google_drive_browser import GoogleDriveBrowserPlugin
from .task_audio_subtitle import TaskAudioSubtitlePlugin
from .task_delivery import TaskDeliveryPlugin
from .waste_reminder import WasteReminderPlugin

__all__ = [
    "AIModelClientPlugin",
    "AudioSplitterPlugin",
    "BatchTextVideoPlugin",
    "VideoStitchPlugin",
    "ChromeLauncherPlugin",
    "CodexAccountSwitcherPlugin",
    "DailyTasksPlugin",
    "FacebookContactSheetPlugin",
    "DaVinciRemotePlugin",
    "InventoryPlugin",
    "MusicDuckerPlugin",
    "SmartVideoEditorPlugin",
    "SmartImageSearchPlugin",
    "SmartMusicSearchPlugin",
    "SmartSearchPlugin",
    "VideoPromptAssistantPlugin",
    "ScriptSegmenterPlugin",
    "DocumentFormatterPlugin",
    "GoogleDriveBrowserPlugin",
    "TaskAudioSubtitlePlugin",
    "TaskDeliveryPlugin",
    "WasteReminderPlugin",
]
