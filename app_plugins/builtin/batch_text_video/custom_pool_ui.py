"""User-created pools share one UI, instead of adding a page implementation per role."""
import copy
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets
from .component_ui import Combo
from .pool_ui import ResourceTable
from .custom_pools import (POOL_TYPES, MAX_POOL_ITEMS, create_pool, get_pool, add_pool_paths,
                           accepted_suffixes, pool_choices)
from .store import file_identity


class NewPoolDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建资源池")
        self.resize(420, 230)
        form = QtWidgets.QFormLayout(self)
        self.name = QtWidgets.QLineEdit()
        self.name.setMaxLength(80)
        self.name.setPlaceholderText("例如：语音池、片头素材、白色水印")
        self.category, self.kind = Combo(), Combo()
        self.category.addItems(["视觉元素", "听觉元素", "文字元素"])
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        form.addRow("名称", self.name)
        form.addRow("类别", self.category)
        form.addRow("用途", self.kind)
        form.addRow(self.note)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                             QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.category.currentIndexChanged.connect(self.update_kinds)
        self.kind.currentIndexChanged.connect(self.update_note)
        self.update_kinds()

    def update_kinds(self, *_args):
        self.kind.clear()
        for kind, (category, label, _note) in POOL_TYPES.items():
            if category == self.category.currentText():
                self.kind.addItem(label, kind)
        self.update_note()

    def update_note(self, *_args):
        self.note.setText(POOL_TYPES[self.kind.currentData()][2] if self.kind.currentData() else "")


class ImagePoolBindingDialog(QtWidgets.QDialog):
    def __init__(self, state, layer, parent=None):
        super().__init__(parent)
        self.state = state
        self.setWindowTitle("图片框 · 选择图片或绑定资源池")
        self.resize(460, 240)
        form = QtWidgets.QFormLayout(self)
        from .thumbnails import ThumbnailCombo
        self.pool, self.mode, self.image = Combo(), Combo(), ThumbnailCombo()
        for identifier, name in pool_choices(state, "image", "默认图片池"):
            self.pool.addItem(name, identifier)
        self.pool.setCurrentIndex(max(0, self.pool.findData(layer.get("pool_id", ""))))
        self.mode.addItem("逐视频轮换（跟随池更新）", "rotate")
        self.mode.addItem("固定使用其中一张", "fixed")
        self.mode.setCurrentIndex(0 if layer.get("source") == "pool" else 1)
        self.note = QtWidgets.QLabel()
        self.note.setWordWrap(True)
        form.addRow("资源池", self.pool)
        form.addRow("使用方式", self.mode)
        form.addRow("图片", self.image)
        form.addRow(self.note)
        self.buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok |
                                                QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        self.pool.currentIndexChanged.connect(self.refresh_images)
        self.mode.currentIndexChanged.connect(self.update_mode)
        self.refresh_images()
        self.image.setCurrentIndex(max(0, self.image.findData(layer.get("path", ""))))

    def items(self):
        identifier = self.pool.currentData()
        return get_pool(self.state, identifier, "image")["items"] if identifier else self.state["image_pool"]

    def refresh_images(self, *_args):
        self.image.clear()
        for item in self.items():
            from .thumbnails import ROLE, descriptor
            self.image.addItem(item.get("name") or Path(item["path"]).name, item["path"])
            self.image.setItemData(self.image.count()-1, descriptor(item['path'], item.get('thumbnail')), ROLE)
            self.image.setItemData(self.image.count()-1, item["path"], QtCore.Qt.ItemDataRole.ToolTipRole)
        self.update_mode()

    def update_mode(self, *_args):
        fixed = self.mode.currentData() == "fixed"
        self.image.setEnabled(fixed)
        self.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok).setEnabled(not fixed or self.image.count() > 0)
        self.note.setText("固定图片不随池变更。" if fixed else
            f"共 {len(self.items())} 张；按任务固定分配，用完循环，失败重试沿用。空池可以预先绑定，但需添加图片后才能导出。")


class CustomPoolController:
    def init_custom_pools(self):
        self.custom_tables, self.custom_pages = {}, {}
        self._image_pool_target = None
        self.sync_custom_pools()

    def sync_custom_pools(self):
        pools = self.state.get("resource_pools", [])
        ids = {pool["id"] for pool in pools}
        for identifier in list(self.custom_pages):
            if identifier not in ids:
                page = self.custom_pages.pop(identifier)
                self.custom_tables.pop(identifier)
                self.resources.removeTab(self.resources.indexOf(page))
                page.deleteLater()
        for pool in pools:
            identifier, kind = pool["id"], pool["kind"]
            if identifier not in self.custom_pages:
                key = "pool:" + identifier
                page = QtWidgets.QWidget()
                page.setProperty("resource_key", key)
                layout = QtWidgets.QVBoxLayout(page)
                table = ResourceTable(["文件", "续用位置", "上次使用"] if kind == "music" else ["文件"])
                table.setAcceptDrops(True)
                table.removeRequested.connect(lambda k=key: self.remove_resource(k))
                table.moveRequested.connect(lambda direction, k=key: self.move_resource(k, direction))
                table.customContextMenuRequested.connect(lambda pos, k=key: self.resource_menu(k, pos))
                table.filesDropped.connect(lambda paths, i=identifier: self.add_to_custom_pool(i, paths))
                # A named pool can supply an overlay or an explicit background
                # pairing. Browsing its rows must never change the base video.
                if kind == "image":
                    table.setIconSize(QtCore.QSize(48, 48))
                layout.addWidget(table, 1)
                note = QtWidgets.QLabel(POOL_TYPES[kind][2] + "。\n上方“添加文件…”导入，“使用素材”选择用途；也可拖入文件。")
                note.setWordWrap(True)
                layout.addWidget(note)
                self.custom_pages[identifier], self.custom_tables[identifier] = page, table
                self.resources.addTab(page, pool["name"])
            page, table = self.custom_pages[identifier], self.custom_tables[identifier]
            self.resources.setTabText(self.resources.indexOf(page), pool["name"])
            self.resources.setTabToolTip(self.resources.indexOf(page), " / ".join(POOL_TYPES[kind][:2]))
            rows = []
            for row, item in enumerate(pool["items"]):
                values = [item.get("name") or Path(item["path"]).name]
                if kind == "music":
                    prefix = "→ " if item["id"] == pool["next_id"] or not pool["next_id"] and row == 0 else ""
                    values = [prefix + values[0], f"{item.get('cursor', 0):.1f}秒", item.get("last_used") or "未使用"]
                rows.append((item["id"], values, item["path"],
                             (item['path'], item.get('thumbnail', '')) if kind in {'image', 'video', 'visual'} else ''))
            table.populate(rows)
        if hasattr(self, "controls") and "background_pool_id" in self.controls:
            combo = self.controls["background_pool_id"]
            current = combo.currentData() or self.state["settings"].get("background_pool_id", "")
            blocker = QtCore.QSignalBlocker(combo); combo.clear()
            combo.addItem("按任务已搭配背景", ""); combo.addItem("无底层背景（使用控件合成）", "none"); combo.addItem("默认背景池（图片／视频）", "builtin:background")
            for pool in pools:
                if pool["kind"] in {"image", "video", "visual"}: combo.addItem(pool["name"], pool["id"])
            if current and combo.findData(current) < 0: combo.addItem("失效的背景池（请重新选择）", current)
            combo.setCurrentIndex(max(0, combo.findData(current))); del blocker
        if hasattr(self, "controls") and "music_pool_id" in self.controls:
            combo = self.controls["music_pool_id"]
            blocker = QtCore.QSignalBlocker(combo)
            current = self.state["settings"].get("music_pool_id", "")
            combo.clear()
            for identifier, name in pool_choices(self.state, "music", "默认音乐池"):
                combo.addItem(name, identifier)
            if current and combo.findData(current) < 0:
                combo.addItem("失效的音乐池（请重选）", current)
            combo.setCurrentIndex(max(0, combo.findData(current)))
            del blocker
        for card in getattr(self, "audio_cards", {}).values():
            card.sync_pools(self.state)

    def resource_items(self, key):
        return get_pool(self.state, key[5:])["items"] if key.startswith("pool:") else self.state[key]

    def set_resource_items(self, key, items):
        if key.startswith("pool:"):
            pool = get_pool(self.state, key[5:])
            pool["items"] = items
            if pool["next_id"] not in {item["id"] for item in items}:
                pool["next_id"] = ""
        else:
            self.state[key] = items

    def new_resource_pool(self):
        if self.is_batch_busy():
            return
        dialog = NewPoolDialog(self)
        try:
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted or not self.persist():
                return
            pool = create_pool(self.state, dialog.name.text(), dialog.kind.currentData())
            if self.persist():
                self.refresh_tables()
                self.tabs.setCurrentWidget(self.resources)
                self.resources.setCurrentWidget(self.custom_pages[pool["id"]])
                self.status.setText("已创建资源池；点击左上方“添加文件…”或拖入文件添加素材。")
        except Exception as error:
            self.status.setText("创建资源池失败：" + str(error))
            self._log("创建资源池失败：" + str(error))
        finally:
            dialog.deleteLater()

    def choose_custom_files(self):
        if self.is_batch_busy():
            return
        key = self.active_resource()
        if key == "backgrounds":
            self.add_videos()
            return
        if not key or not key.startswith("pool:"):
            self.status.setText("请先选中一个自建资源池分页。")
            return
        pool = get_pool(self.state, key[5:])
        pattern = " ".join("*" + suffix for suffix in sorted(accepted_suffixes(pool["kind"])))
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加到 " + pool["name"], "", "支持的文件 (" + pattern + ")")
        if paths:
            self.add_to_custom_pool(pool["id"], paths)

    def add_to_custom_pool(self, identifier, paths):
        if self.is_batch_busy() or not self.persist():
            return False
        try:
            pool = get_pool(self.state, identifier)
            if pool["kind"] == "image":
                known = {file_identity(item["path"]) for item in pool["items"]}
                paths = list(dict.fromkeys(str(Path(path).resolve()) for path in paths
                    if Path(path).is_file() and Path(path).suffix.casefold() in accepted_suffixes("image")
                    and file_identity(path) not in known))
                if len(paths) + len(pool["items"]) > MAX_POOL_ITEMS:
                    raise ValueError(f"每个池最多 {MAX_POOL_ITEMS} 项。")
                if paths:
                    self.import_pool_images(paths, target=identifier)
                else:
                    self.status.setText("没有新图片；重复路径或不支持的文件已忽略。")
                return bool(paths)
            added = add_pool_paths(pool, paths)
            if not self.persist():
                return False
            self.refresh_tables()
            self.invalidate_playback()
            self.status.setText(f"{pool['name']}：新增 {added} 项；重复路径和不支持的文件已忽略。")
            return True
        except Exception as error:
            self.status.setText("资源池导入失败：" + str(error))
            self._log("资源池导入失败：" + str(error))
            return False

    def rename_resource_pool(self):
        key = self.active_resource()
        if self.is_batch_busy() or not key or not key.startswith("pool:"):
            self.status.setText("请先选中自建资源池。")
            return
        pool = get_pool(self.state, key[5:])
        name, ok = QtWidgets.QInputDialog.getText(self, "重命名资源池", "名称", text=pool["name"])
        if not ok:
            return
        name = name.strip()
        if not name or len(name) > 80 or any(p["name"] == name and p["id"] != pool["id"] for p in self.state["resource_pools"]):
            self.status.setText("名称不能为空、重复或超过 80 字。")
            return
        pool["name"] = name
        if self.persist():
            self.refresh_tables()

    def delete_resource_pool(self):
        key = self.active_resource()
        if self.is_batch_busy() or not key or not key.startswith("pool:"):
            self.status.setText("默认池保留；请选中要删除的自建资源池。")
            return
        pool = get_pool(self.state, key[5:])
        if QtWidgets.QMessageBox.question(self, "删除资源池", f"删除“{pool['name']}”的池记录？\n原文件及逐行指定的人声保留；图片框保留为自定义轮换。音乐控件会改回默认池；人声控件若绑定此池，会提示失效，需重新选择。") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        if not self.persist():
            return
        identifier = pool["id"]
        layers = copy.deepcopy(self.state["settings"]["layers"])
        from .components import walk_components
        for layer in walk_components(layers):
            if layer.get("pool_id") == identifier:
                layer.update(source="sequence")
                layer.pop("pool_id", None)
        self.layer_panel.set_layers(layers)
        self.state["resource_pools"] = [p for p in self.state["resource_pools"] if p["id"] != identifier]
        if self.controls["music_pool_id"].currentData() == identifier:
            self.controls["music_pool_id"].setCurrentIndex(0)
        if self.persist():
            self.refresh_tables()
            self.invalidate_playback()

    def use_custom_music(self, identifier):
        if self.is_batch_busy():
            return
        self.controls["music_pool_id"].setCurrentIndex(self.controls["music_pool_id"].findData(identifier))
        self.controls["music_enabled"].setChecked(True)
        self.persist()
        self.status.setText("已绑定背景音乐控件；避让人声可在“混音细节”中调整。")

    def use_custom_voices(self, identifier):
        if self.is_batch_busy():
            return
        selected = set(self.custom_tables[identifier].identifiers())
        voices = [item for item in get_pool(self.state, identifier, "voice")["items"] if item["id"] in selected]
        targets = [job for job in self.state["jobs"] if job["id"] in self.selected_ids()]
        if not targets:
            key, current = ("copy_pool", self.current_copy_id) if self.current_copy_id else ("jobs", self.current_id)
            targets = [item for item in self.state[key] if item["id"] == current]
        if not voices or not targets:
            self.status.setText("先选择人声和生成队列条目；也可选一条文案后指定人声。")
            return
        if any(not Path(item["path"]).is_file() for item in voices):
            self.status.setText("所选人声文件已不存在，请检查路径。")
            return
        for i, target in enumerate(targets):
            target.update(voice_path=voices[i % len(voices)]["path"], voice_override=True,
                          status="待生成", match_error="", error="")
        self.controls["use_voice"].setChecked(True)
        self.persist()
        self.refresh_tables()
        self.invalidate_playback()
        self.status.setText(f"已给 {len(targets)} 项指定人声；按所选人声的池内顺序轮换。")

    def pick_image_pool(self, identifier):
        if self.is_batch_busy():
            return
        layers = self.layer_panel.values()
        layer = next((layer for layer in layers if layer["id"] == identifier and layer["kind"] == "image"), None)
        if layer is None:
            return
        dialog = ImagePoolBindingDialog(self.state, layer, self)
        try:
            if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
                return
            if dialog.mode.currentData() == "fixed":
                selected = next(item for item in dialog.items() if item["path"] == dialog.image.currentData())
                if not Path(selected["path"]).is_file():
                    raise ValueError("图片文件已不存在，请重新选择。")
                layer.update(source="fixed", path=selected["path"], thumbnail=selected.get("thumbnail", ""),
                             sequence_items=[], sequence_cursor=0)
                layer.pop("pool_id", None)
                layer.pop("pool_name", None)
            else:
                layer.update(source="pool", pool_id=dialog.pool.currentData(), sequence_items=[], sequence_cursor=0)
            from .pools import bind_pool_layers
            self.layer_panel.set_layers(bind_pool_layers(layers, self.state))
            self.layer_panel.select_layer_id(identifier)
            self.persist()
            self.update_preview()
        except Exception as error:
            self.status.setText("绑定图片池失败：" + str(error))
            self._log("绑定图片池失败：" + str(error))
        finally:
            dialog.deleteLater()
