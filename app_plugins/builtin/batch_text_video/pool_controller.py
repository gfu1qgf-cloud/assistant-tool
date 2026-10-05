"""Pool/preset UI orchestration, kept separate from rendering and the host."""
import copy
from pathlib import Path
from qt_compat import QtCore, QtGui, QtWidgets
from .pool_ui import ResourceTable, PairingDialog
from .pools import apply_pairs, bind_pool_layers, new_copy
from .layer_ui import ImageImportWorker
from .layers import MAX_IMAGES


class PoolController:
    def init_resources(self, music_page):
        self.current_copy_id = ""
        self.preview_video = ""
        self.image_import_worker = None
        self._discard_close_save = False
        self.resources = QtWidgets.QTabWidget()
        self.video_pool = ResourceTable(["背景视频"])
        self.copy_pool = ResourceTable(["文案 / 任务", "标题"])
        self.image_pool = ResourceTable(["图片"])
        self.resources.addTab(self.video_pool, "视频")
        self.resources.addTab(self.copy_pool, "文案")
        self.resources.addTab(self.image_pool, "图片")
        self.resources.addTab(music_page, "音乐")
        self.tabs.insertTab(0, self.resources, "资源池")
        self.copy_pool.setColumnWidth(1, 150)
        self.image_pool.setIconSize(QtCore.QSize(48, 48))
        self.copy_pool.currentCellChanged.connect(self.select_copy)
        self.video_pool.currentCellChanged.connect(self.select_video)
        for table, key in ((self.video_pool, "backgrounds"), (self.copy_pool, "copy_pool"),
                           (self.image_pool, "image_pool"), (self.music, "music"), (self.jobs, "jobs")):
            table.removeRequested.connect(lambda k=key: self.remove_resource(k))
            table.moveRequested.connect(lambda direction, k=key: self.move_resource(k, direction))
            table.customContextMenuRequested.connect(lambda pos, k=key: self.resource_menu(k, pos))

    def init_menus(self, layout):
        self.menu_bar = QtWidgets.QMenuBar(self)
        self.menu_bar.setNativeMenuBar(False)
        layout.insertWidget(0, self.menu_bar)
        resources = self.menu_bar.addMenu("资源")
        for name, callback in (("读取当前任务文案…", self.import_sheet_text), ("新增文案", self.add_manual_copy),
                               ("添加视频…", self.add_videos), ("添加图片…", self.choose_pool_images),
                               ("添加音乐…", self.add_music), ("导入任务人声…", self.add_voices),
                               ("导入任务目录…", self.add_task_folder)):
            action = resources.addAction(name, callback)
            shortcut = {"新增文案":"Ctrl+N","添加视频…":"Ctrl+O","读取当前任务文案…":"Ctrl+Shift+T"}.get(name)
            if shortcut:
                action.setShortcut(shortcut)
        resources.addSeparator()
        self.remove_action = resources.addAction("移出所选（不删除原文件）", self.remove_active)
        resources.addAction("清空当前池 / 队列…", self.clear_active)
        resources.addAction("全选", self.select_all_active)
        batch = self.menu_bar.addMenu("批量")
        batch.addAction("搭配资源，加入生成队列…", self.compose_queue)
        batch.addAction("当前文案应用到所选队列", self.copy_text)
        batch.addSeparator()
        self.quick_export_action = batch.addAction("快速导出当前预览", self.quick_export_current)
        self.quick_export_action.setShortcut("Ctrl+E")
        batch.addAction("生成待完成视频", lambda: self.start_batch(False))
        batch.addAction("生成所选队列视频", lambda: self.start_batch(True))
        batch.addAction("重置音乐轮换进度…", self.reset_music)
        view = self.menu_bar.addMenu("视图")
        view.addAction("资源池", lambda: self.tabs.setCurrentWidget(self.resources))
        view.addAction("生成队列", lambda: self.tabs.setCurrentWidget(self.jobs))
        view.addAction("图层与文字设置", self.edit_text_components)
        view.addAction("刷新预览", self.refresh_preview)
        view.addAction("打开当前成品目录", self.open_output_directory)
        self.profile_menu = self.menu_bar.addMenu("配置")
        self.profile_menu.aboutToShow.connect(self.rebuild_profile_menu)
        self.rebuild_profile_menu()
        self.layer_panel.copyRequested.connect(self.pick_layer_copy)

    def table_for(self, key):
        return {"backgrounds": self.video_pool, "copy_pool": self.copy_pool,
                "image_pool": self.image_pool, "music": self.music, "jobs": self.jobs}[key]

    def active_resource(self):
        if self.tabs.currentWidget() == self.resources:
            return ("backgrounds", "copy_pool", "image_pool", "music")[self.resources.currentIndex()]
        return "jobs" if self.tabs.currentWidget() == self.jobs else None

    def resource_menu(self, key, pos):
        if self.is_batch_busy():
            return
        table = self.table_for(key)
        item = table.itemAt(pos)
        if item and not item.isSelected():
            table.selectRow(item.row())
        menu = QtWidgets.QMenu(table)
        if key in {"backgrounds", "copy_pool"}:
            menu.addAction("批量搭配…", self.compose_queue)
        if key == "copy_pool":
            menu.addAction("新增文案", self.add_manual_copy)
            menu.addAction("复制所选文案", self.duplicate_copies)
        if key == "image_pool":
            menu.addAction("添加为固定图片图层", lambda: self.use_pool_images(False))
            menu.addAction("添加为轮换图片框", lambda: self.use_pool_images(True))
        menu.addAction("移出所选（Delete，不删除文件）", lambda: self.remove_resource(key)).setEnabled(bool(table.identifiers()))
        menu.addAction("上移（Alt+↑）", lambda: self.move_resource(key, -1))
        menu.addAction("下移（Alt+↓）", lambda: self.move_resource(key, 1))
        menu.addSeparator()
        menu.addAction("全选（Ctrl+A）", table.selectAll)
        menu.addAction("取消选择", table.clearSelection)
        menu.addAction("清空此列表…", lambda: self.clear_resource(key))
        menu.exec(table.viewport().mapToGlobal(pos))

    def refresh_pools(self):
        self.video_pool.populate([(path, [Path(path).name], path, "") for path in self.state["backgrounds"]])
        self.copy_pool.populate([(item["id"], [item.get("label") or item.get("name", "文案"), item.get("title", "")],
                                  "\n".join((item.get("title", ""), item.get("body", ""), item.get("task_dir", ""))), "")
                                for item in self.state["copy_pool"]])
        self.image_pool.populate([(item["id"], [item.get("name") or Path(item["path"]).name],
                                   item["path"], item.get("thumbnail", "")) for item in self.state["image_pool"]])

    def active_entry(self):
        if self.current_copy_id:
            source = next((item for item in self.state["copy_pool"] if item["id"] == self.current_copy_id), None)
            if source:
                return {**source, "path": self.copy_preview_path(source)}
        source = next((item for item in self.state["jobs"] if item["id"] == self.current_id), None)
        return source or {"path": self.preview_video}

    def copy_preview_path(self, source):
        # Preview immediately, without creating an implicit export pairing.
        if self.preview_video and self.preview_video in self.state["backgrounds"]:
            return self.preview_video
        return source.get("path") or (self.state["backgrounds"][0] if self.state["backgrounds"] else "")

    def select_copy(self, row, *_args):
        if self._loading or row < 0 or not self.copy_pool.item(row, 0):
            return
        self.edit_timer.stop()
        self._commit_edit()
        self.current_id = ""
        self.current_copy_id = self.copy_pool.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        source = next(item for item in self.state["copy_pool"] if item["id"] == self.current_copy_id)
        self._show_job({**source, "path": self.copy_preview_path(source)})

    def select_video(self, row, *_args):
        if self._loading or row < 0 or not self.video_pool.item(row, 0):
            return
        self.preview_video = self.video_pool.item(row, 0).data(QtCore.Qt.ItemDataRole.UserRole)
        if not self.current_copy_id:
            self._commit_edit()
            self.current_id = ""
            self.clear_editor()
        self.request_background(self.preview_video)
        self.update_preview()
        self.refresh_editor_baseline()

    def clear_editor(self):
        self.invalidate_playback()
        self._loading = True
        self.title.clear()
        self.body.clear()
        self.name.clear()
        for widget in (self.title,self.body,self.name):
            widget.setEnabled(False)
        self.title.setPlaceholderText("先选择文案池条目，或在“资源”菜单新增文案。固定文本框在“图层→内容”中填写。")
        self.voice_label.setText("当前人声：未指定")
        self._loading = False
        self._preview_path = None
        self._media = self._voice_duration = None
        self.preview.background = QtGui.QImage()
        self._background_error = ""
        self.update_preview()
        self.refresh_editor_baseline()

    def add_manual_copy(self):
        if self.is_batch_busy():
            return
        self._commit_edit()
        item = new_copy()
        self.state["copy_pool"].append(item)
        self.persist()
        self.refresh_tables()
        self.tabs.setCurrentWidget(self.resources)
        self.resources.setCurrentWidget(self.copy_pool)
        self.copy_pool.setCurrentCell(len(self.state["copy_pool"])-1, 0)
        self.title.setFocus()

    def duplicate_copies(self):
        if self.is_batch_busy():
            return
        import uuid
        self._commit_edit()
        selected = set(self.copy_pool.identifiers())
        for item in list(self.state["copy_pool"]):
            if item["id"] in selected:
                clone = copy.deepcopy(item)
                clone.update(id=uuid.uuid4().hex, name=item.get("name", "文案")+"_副本", label=item.get("label", "文案")+"_副本")
                # Manual variants keep an explicit output name, not a table-import identity.
                clone.pop("source_sheet", None)
                self.state["copy_pool"].append(clone)
        self.persist()
        self.refresh_tables()

    def remove_resource(self, key):
        if self.is_batch_busy():
            return
        selected = set(self.table_for(key).identifiers())
        if not selected:
            return
        self.edit_timer.stop()
        self._commit_edit()
        self.state[key] = [item for item in self.state[key] if (item if key == "backgrounds" else item["id"]) not in selected]
        clear = False
        if key == "copy_pool" and self.current_copy_id in selected:
            self.current_copy_id = ""
            clear = True
        if key == "jobs" and self.current_id in selected:
            self.current_id = ""
            clear = True
        if key == "backgrounds" and self.preview_video in selected:
            self.preview_video = ""
            self._preview_pending = None
            if self.current_copy_id or self.current_id:
                self._preview_path = None
                self.preview.background = QtGui.QImage()
                self.request_background(self.active_entry().get("path", ""))
            else:
                clear = True
        if key == "music" and self.state["next_music_id"] in selected:
            self.state["next_music_id"] = ""
        if clear:
            self.clear_editor()
        self.persist()
        self.refresh_tables()
        self.update_preview()
        self.status.setText(f"已移出{len(selected)}项；原文件未删除，既有搭配/成品未删除。")

    def remove_active(self):
        key = self.active_resource()
        if key:
            self.remove_resource(key)

    def select_all_active(self):
        key = self.active_resource()
        if key:
            self.table_for(key).selectAll()

    def clear_active(self):
        key = self.active_resource()
        if key:
            self.clear_resource(key)

    def clear_resource(self, key):
        if self.is_batch_busy() or not self.state[key]:
            return
        if QtWidgets.QMessageBox.question(self, "清空列表", "只清空此列表记录，不删除任何原文件或成品。是否继续？") == QtWidgets.QMessageBox.StandardButton.Yes:
            self.table_for(key).selectAll()
            self.remove_resource(key)

    def move_resource(self, key, direction):
        if self.is_batch_busy():
            return
        selected = set(self.table_for(key).identifiers())
        items = self.state[key]
        key_of = lambda item: item if key == "backgrounds" else item["id"]
        indices = range(len(items)) if direction < 0 else reversed(range(len(items)))
        for i in indices:
            target = i+direction
            if key_of(items[i]) in selected and 0 <= target < len(items) and key_of(items[target]) not in selected:
                items[i], items[target] = items[target], items[i]
        self.persist()
        self.refresh_tables()

    def compose_queue(self):
        if self.is_batch_busy() or not self.persist():
            return False
        dialog = PairingDialog(self.state["copy_pool"], self.state["backgrounds"],
                               self.copy_pool.identifiers(), self.video_pool.identifiers(), self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            dialog.deleteLater()
            return False
        try:
            added, updated = apply_pairs(self.state, dialog.pairs)
            if not self.persist():
                return False
            self.refresh_tables()
            self.tabs.setCurrentWidget(self.jobs)
            if self.jobs.rowCount():
                self.jobs.setCurrentCell(0, 0)
                self._selection_changed(0)
            self.status.setText(f"队列新增{added}个、更新{updated}个；重复搭配自动复用。可核对后批量生成。")
            return True
        except Exception as error:
            self._log("搭配失败："+str(error))
            return False
        finally:
            dialog.deleteLater()

    def choose_pool_images(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "添加图片到素材池", "",
            "图片 (*.png *.jpg *.jpeg *.jfif *.webp *.bmp *.tif *.tiff)")
        if paths:
            self.import_pool_images(paths)

    def import_pool_images(self, paths):
        if self.is_batch_busy():
            return False
        self.image_import_worker = ImageImportWorker(paths, self.store.directory, self)
        self.image_import_worker.ready.connect(self.pool_images_ready, QtCore.Qt.ConnectionType.QueuedConnection)
        self.image_import_worker.finished.connect(self.pool_images_finished)
        self._pool_image_result = None
        self._set_work_controls(True)
        self.image_import_worker.start()
        return True

    def pool_images_ready(self, result):
        self._pool_image_result = result

    def pool_images_finished(self):
        self.image_import_worker.deleteLater()
        self.image_import_worker = None
        self._set_work_controls(False)
        result = self._pool_image_result or {"layers": [], "errors": ["图片导入未返回结果。"]}
        if not result.get("cancelled"):
            known = {item["path"] for item in self.state["image_pool"]}
            for item in result["layers"]:
                if item["path"] not in known:
                    self.state["image_pool"].append(item)
                    known.add(item["path"])
            self.persist()
            self.refresh_tables()
            self.tabs.setCurrentWidget(self.resources)
            self.resources.setCurrentWidget(self.image_pool)
            self.status.setText(f"已导入{len(result['layers'])}张图片到池；右键可添加固定/轮换图片框。")
        for error in result.get("errors", []):
            self._log(error)

    def use_pool_images(self, rotating):
        if self.is_batch_busy():
            return
        selected = set(self.image_pool.identifiers())
        items = [item for item in self.state["image_pool"] if item["id"] in selected]
        if not items:
            return
        layers = self.layer_panel.values()
        count = sum(layer["kind"] == "image" for layer in layers)
        if count + (1 if rotating else len(items)) > MAX_IMAGES:
            self.status.setText(f"图片框最多{MAX_IMAGES}个，请先移除一些图层。")
            return
        import uuid
        first = next((i for i, layer in enumerate(layers) if layer["kind"] != "image"), len(layers))
        added = [copy.deepcopy(item) for item in (items[:1] if rotating else items)]
        for layer in added:
            layer.update(id=uuid.uuid4().hex, source="pool" if rotating else "fixed", sequence_cursor=0,
                         sequence_items=copy.deepcopy(self.state["image_pool"]) if rotating else [])
            if rotating:
                layer["name"] = "轮换图片框（图片池）"
        layers[first:first] = added
        self.layer_panel.set_layers(layers)
        self.persist()
        self.tabs.setCurrentWidget(self.layer_scroll)
        self.update_preview()

    def pick_layer_copy(self, identifier):
        if not self.state["copy_pool"]:
            self.status.setText("文案池为空，请先读取当前任务文案或新增文案。")
            return
        labels = [f"{i+1}. {item.get('label') or item.get('name', '')} — {item.get('title', '')[:55]}"
                  for i, item in enumerate(self.state["copy_pool"])]
        label, ok = QtWidgets.QInputDialog.getItem(self, "选择固定文案", "文案池", labels, 0, False)
        if not ok:
            return
        field, ok = QtWidgets.QInputDialog.getItem(self, "选择内容", "使用哪部分", ["完整文案", "标题", "正文"], 0, False)
        if not ok:
            return
        item = self.state["copy_pool"][labels.index(label)]
        value = item.get({"标题": "title", "正文": "body"}.get(field, ""), "") if field != "完整文案" else "\n".join(
            text for text in (item.get("title", ""), item.get("body", "")) if text)
        self.layer_panel.set_fixed_text(identifier, value)

    def rebuild_profile_menu(self):
        self.profile_menu.clear()
        self.profile_menu.addAction("保存当前配置", self.persist).setShortcut("Ctrl+S")
        self.profile_menu.addAction("新建空配置…", lambda: self.create_named_profile(False))
        self.profile_menu.addAction("另存为新配置（含资源和进度）…", lambda: self.create_named_profile(True)).setShortcut("Ctrl+Shift+S")
        self.profile_menu.addAction("重命名当前配置…", self.rename_profile)
        self.profile_menu.addSeparator()
        for profile in self.state["profiles"]:
            action = self.profile_menu.addAction(profile["name"], lambda _checked=False, p=profile["id"]: self.switch_profile(p))
            action.setCheckable(True)
            action.setChecked(profile["id"] == self.state["active_profile"])

    def create_named_profile(self, clone):
        if self.is_batch_busy():
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "另存配置" if clone else "新建配置", "名称（例如：祷告词 / 管理员姓名）")
        if ok:
            self.preview.cancel_drag()
        if ok and self.persist():
            try:
                self.state = self.store.create_profile(self.state, name, clone)
                self.apply_profile_widgets()
            except Exception as error:
                self._log("配置创建失败："+str(error))

    def switch_profile(self, identifier):
        if self.is_batch_busy() or identifier == self.state["active_profile"]:
            return
        self.preview.cancel_drag()
        if not self.persist():
            return
        try:
            self.state = self.store.select_profile(self.state, identifier)
            self.apply_profile_widgets()
        except Exception as error:
            self._log("切换配置失败："+str(error))

    def rename_profile(self):
        if self.is_batch_busy():
            return
        profile = next(item for item in self.state["profiles"] if item["id"] == self.state["active_profile"])
        name, ok = QtWidgets.QInputDialog.getText(self, "重命名配置", "名称", text=profile["name"])
        if not ok:
            return
        if not name.strip() or len(name.strip()) > 80 or any(p["name"] == name.strip() and p["id"] != profile["id"] for p in self.state["profiles"]):
            self.status.setText("配置名称不能为空、重复或超过80字。")
            return
        if self.persist():
            profile = next(item for item in self.state["profiles"] if item["id"] == self.state["active_profile"])
            profile["name"] = name.strip()
            self.persist()
            self.apply_profile_title()

    def apply_profile_title(self):
        profile = next(item for item in self.state["profiles"] if item["id"] == self.state["active_profile"])
        self.setWindowTitle("批量文案视频 · "+profile["name"])

    def apply_profile_widgets(self):
        self._batch_after_preview = None
        self.invalidate_playback()
        self.edit_timer.stop()
        self.geometry_preview_timer.stop()
        self._loading = True
        self.preview.cancel_drag()
        self.current_id = self.current_copy_id = self.preview_video = ""
        self._preview_pending = self._overlay_pending = None
        settings = self.state["settings"]
        for key, control in self.controls.items():
            if isinstance(control, QtWidgets.QAbstractSpinBox):
                control.setValue(settings[key])
            elif isinstance(control, QtWidgets.QCheckBox):
                control.setChecked(settings[key])
            elif isinstance(control, QtWidgets.QComboBox):
                control.setCurrentIndex(max(0, control.findData(settings[key])))
            else:
                control.setText(settings[key])
        self.scheme.setCurrentIndex(max(0, self.scheme.findData(settings["scheme"])))
        self.output.setText(settings["output_dir"])
        self.ffmpeg.setText(settings["ffmpeg_path"])
        self.show_bounds_checkbox.setChecked(settings["show_layer_bounds"])
        self.layer_panel.settings = copy.deepcopy(settings)
        self.layer_panel.units.setCurrentIndex(max(0, self.layer_panel.units.findData(settings["geometry_units"])))
        self.layer_panel.set_layers(settings["layers"])
        self.tint_preset.setCurrentIndex(0)
        for table in (self.jobs,self.music,self.video_pool,self.copy_pool,self.image_pool):
            table.clearSelection()
            table.selectionModel().setCurrentIndex(QtCore.QModelIndex(),QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        self._loading = False
        self.clear_editor()
        self.refresh_tables()
        self.tabs.setCurrentWidget(self.resources)
        self.apply_profile_title()
        self.status.setText("已切换配置；资源池、队列和轮换进度均相互独立。")
        self.reset_editor_history()
