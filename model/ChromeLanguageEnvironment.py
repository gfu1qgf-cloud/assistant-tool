"""Normal Chrome language preferences in host-owned, isolated user-data roots.

Seed a new profile once. Never rewrite an existing Chrome Preferences/Local State
file, copy credentials from another profile, or alter OS/identity/network settings.
"""
from datetime import datetime, timezone
import re
import subprocess
import threading
import uuid

from model.AppDataStorage import application_storage
from model.OfflineMode import require_online


# (application locale, preferred web languages, spellcheck dictionary, label)
LANGUAGE_PRESETS = {
    "en-US": ("en-US", ("en-US", "en"), "en-US", "English (US)"),
    "en-GB": ("en-GB", ("en-GB", "en"), "en-GB", "English (UK)"),
    "it": ("it", ("it-IT", "it", "en"), "it-IT", "Italiano"),
    "fr": ("fr", ("fr-FR", "fr", "en"), "fr", "Français"),
    "de": ("de", ("de-DE", "de", "en"), "de-DE", "Deutsch"),
    "es": ("es", ("es-ES", "es", "en"), "es-ES", "Español"),
}
_CREATE_LOCK = threading.RLock()
_ID = re.compile(r"[0-9a-f]{32}\Z")


LANGUAGE_CHECK_HTML = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Browser language check</title>
<style>body{font:17px system-ui;background:#eef3f8;color:#172b43;margin:0;padding:8vh 24px}
main{max-width:760px;margin:auto;background:white;border-radius:20px;padding:32px;
box-shadow:0 8px 36px #243d5614}h1{margin-top:0}pre{white-space:pre-wrap;
background:#f3f7fb;padding:18px;border-radius:12px}p{line-height:1.6}</style>
<main><h1>Browser language check</h1>
<p>These are the browser's actual language preferences, not injected values.</p>
<pre id="result">Checking…</pre>
<p>This page is local. It does not send your results anywhere.</p>
<p>Website account preferences and organization policies may override language
choices. Your keyboard, location, IP address and timezone are unchanged.</p></main>
<script>document.getElementById('result').textContent = JSON.stringify({
language:navigator.language,languages:Array.from(navigator.languages),
formattingLocale:Intl.DateTimeFormat().resolvedOptions().locale}, null, 2);</script>
</html>"""


class ChromeLanguageEnvironmentStore:
    def __init__(self, storage=None):
        namespace = storage if storage is not None else application_storage().for_plugin("chrome_launcher")
        self.root = namespace.directory("language_environments")

    def _directory(self, environment_id):
        if not isinstance(environment_id, str) or not _ID.fullmatch(environment_id):
            raise ValueError("外语环境编号无效，请使用配置生成器创建环境。")
        return self.root.directory(environment_id)

    def _read(self, environment_id):
        directory = self._directory(environment_id)
        try:
            record = directory.file("environment.json").read_json()
            valid = (isinstance(record, dict) and record.get("schema_version") == 1
                     and record.get("id") == environment_id
                     and record.get("language") in LANGUAGE_PRESETS
                     and isinstance(record.get("name"), str) and record["name"].strip()
                     and isinstance(record.get("created_at"), str))
            if not valid:
                raise ValueError("配置格式不正确")
        except (OSError, ValueError, TypeError) as error:
            raise ValueError(f"外语环境配置无法读取（{environment_id}）：{error}。未覆盖原数据。") from error
        return record

    def list_environments(self):
        if not self.root.path.exists():
            return []
        records = []
        for path in sorted(self.root.path.iterdir()):
            if _ID.fullmatch(path.name) and path.is_dir():
                # Uncommitted creation attempts are not usable environments.
                if self._directory(path.name).file("environment.json").exists():
                    records.append(self._read(path.name))
        return sorted(records, key=lambda item: (item["created_at"], item["id"]))

    def create(self, name, language="en-US"):
        if language not in LANGUAGE_PRESETS:
            raise ValueError("请选择支持的外语语言。")
        name = str(name).strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError("环境名称须为 1–80 个字符，不能包含换行等控制字符。")
        with _CREATE_LOCK:
            if any(item["name"].casefold() == name.casefold() for item in self.list_environments()):
                raise ValueError("已有同名环境，请直接打开，或使用另一个名称。")
            environment_id = uuid.uuid4().hex
            directory = self._directory(environment_id)
            # Reserve a fresh namespace; no existing profile can be overwritten.
            directory.ensure(exist_ok=False)
            user_data = directory.directory("User Data")
            profile = user_data.directory("Default")
            locale, languages, dictionary, native_name = LANGUAGE_PRESETS[language]
            local_state = {"intl": {"app_locale": locale}}
            preferences = {
                # Chromium derives accept_languages from selected_languages.
                "intl": {"selected_languages": ",".join(languages)},
                "browser": {"enable_spellchecking": True},
                "spellcheck": {"dictionaries": [dictionary], "use_spelling_service": False},
                "profile": {"name": native_name},
                "translate_recent_target": languages[-1] if language.startswith("en-") else language,
            }
            record = {"schema_version": 1, "id": environment_id, "name": name,
                      "language": language, "created_at": datetime.now(timezone.utc).isoformat()}
            files = [user_data.file("Local State"), profile.file("Preferences"),
                     directory.file("language-check.html"), directory.file("environment.json")]
            try:
                files[0].write_json(local_state, indent=2)
                files[1].write_json(preferences, indent=2)
                files[2].write_text(LANGUAGE_CHECK_HTML)
                # Manifest is the commit point; never launch a partial seed.
                files[3].write_json(record, indent=2)
            except Exception:
                for file in reversed(files):
                    file.remove(missing_ok=True)
                for folder in (profile, user_data, directory):
                    if folder.path.exists():
                        folder.remove()
                raise
            return record

    def environment_directory(self, environment_id):
        self._read(environment_id)
        return self._directory(environment_id).path

    def launch_command(self, chrome_path, environment_id, destination="newtab"):
        record = self._read(environment_id)
        directory = self._directory(environment_id)
        user_data = directory.directory("User Data")
        if not user_data.file("Local State").exists() or not user_data.file("Default/Preferences").exists():
            raise ValueError("此环境的 Chrome 配置文件缺失，已停止启动，避免意外创建空白环境。")
        if destination == "newtab":
            target = "chrome://newtab/"
        elif destination == "languages":
            target = "chrome://settings/languages"
        elif destination == "check":
            check = directory.file("language-check.html")
            if not check.exists():
                # This is our static diagnostic, not a Chrome-managed file.
                check.write_text(LANGUAGE_CHECK_HTML)
            target = check.path.as_uri()
        else:
            raise ValueError("不支持的环境打开方式。")
        locale = LANGUAGE_PRESETS[record["language"]][0]
        return [str(chrome_path), f"--user-data-dir={user_data.path}",
                "--profile-directory=Default", f"--lang={locale}", "--new-window",
                "--no-first-run", "--no-default-browser-check", "--disable-sync", target]

    def launch(self, chrome_path, environment_id, destination="newtab"):
        if not chrome_path:
            raise ValueError("未找到 Chrome，请先安装 Google Chrome。")
        require_online("启动外语 Chrome 环境")
        command = self.launch_command(chrome_path, environment_id, destination)
        # Native, unmodified Chrome. Keep the normal sandbox and TLS checks.
        return subprocess.Popen(command)
