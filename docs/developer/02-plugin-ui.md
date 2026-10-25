# 插件注册与 UI 接口

[返回目录](README.md) · [完整示例](examples/sample_plugin.py)

## 生命周期

```text
构造插件 → install / register → 宿主挂载 UI → start_all
                                         ↓
                        设置保存 → apply_settings(config)
                                         ↓
                  can_close → stop（按安装逆序）→ 安全退出
```

| 方法 | 合约与注意事项 |
|---|---|
| `register(context)` | 接收该插件专属上下文；声明命令、UI、节点，不做网络/模型加载 |
| `start()` | 宿主启动已启用插件；耗时初始化也要异步，不阻塞 GUI |
| `apply_settings(config)` | 应用已保存配置，成功返回 `True`；失败不能被当作应用成功 |
| `update_config(config)` | 可选：将插件运行态写入传入配置，不能拿构造默认值覆盖未加载数据 |
| `can_close()` | 返回 `(allowed, message)`；正在处理不能假装可退出 |
| `stop()` | 停止监听/服务、释放资源；尚未安全结束返回 `False`，宿主暂缓退出 |

这些方法是鸭子类型接口，不要求继承插件基类；示例继承 `QObject` 是为提供明确的 GUI 线程信号接收者。不要从后台线程直接调用 UI 生命周期。

## 四种 UI 入口不要混淆

| 需求 | 注册接口 | 工厂 / 回调参数和返回 |
|---|---|---|
| 插件菜单 / 工具菜单 / 任务右键 | `PluginCommand` | 普通 `callback(rows)`；可勾选 `callback(rows, checked)` |
| 独立窗口 | 菜单回调自己持有并显示 `QDialog` | 没有额外的 `register_window` API；父对象用 `context.parent_widget` |
| 主界面快捷操作卡片 | `PluginMainWidget` | `factory(parent)` → `QWidget` 或含 `.widget` 的控制器 |
| 主窗口分页 | `PluginTabPage` | `factory(parent)` → **QWidget**，不返回普通控制器 |
| 程序设置分页 | `PluginSettingsPage` | `factory(dialog)` → `QWidget` 或含 `.widget` 的控制器 |

公共描述对象从 `app_plugins.api` 导入；所有工厂都应在 GUI 主线程创建控件。独立窗口要保留 `self.dialog`，否则局部变量窗口可能立即被回收。普通重开复用窗口，按需 `show()`、`raise_()`、`activateWindow()`；不要每点击一次就新建一个后台服务。

### 命令

```python
context.register_command(PluginCommand(
    command_id='open', title='我的工具…', callback=self.open_from_menu,
    locations=frozenset({MAIN_MENU}), order=50,
))
# def open_from_menu(self, rows): ...
```

`MAIN_MENU` 是插件菜单，`TOOLS_MENU` 是工具菜单，`TASK_CONTEXT_MENU` 是任务右键。宿主生成完整 ID `plugin_id.command_id`，重复会拒绝。`enabled(rows)`、`visible(rows)` 必须快速且无副作用；不能在菜单展开时查网络。`checkable=True` 时配 `checked(rows)` 和双参数回调。`submenu` 为菜单分组，`tooltip` 提供短说明。

右键命令收到的是行标识，不是冻结业务对象。**在 GUI 提交阶段**用 `context.task_targets(rows)` 获取任务目标快照，确认文案、目录和归属，再传给后台；不要让 worker 重新查当前选择。

### 主界面卡片和分页

```python
context.register_main_widget(PluginMainWidget('quick', self.make_quick,
                                              order=50, title='我的工具'))
context.register_tab_page(PluginTabPage('page', '我的工具', self.make_tab, order=50))
```

宿主管理卡片外框、顺序和拖动。插件只返回内容，不重挂主窗口既有任务表、日志或分割线。不要依赖 `MainDialog` 的私有布局或其他插件的历史别名；`main_widget_controller` 是宿主兼容适配，不是新增业务互相访问的首选。

### 程序设置页

```python
context.register_settings_page(PluginSettingsPage(
    'settings', '我的工具', MySettingsPage, order=50))
```

推荐返回独立控制器，公开以下方法：

| 方法 | 责任 |
|---|---|
| `load_config(config)` | 填充控件，不偷偷保存、不重启业务 |
| `validate()` | 无效时抛 `ValueError`，由宿主集中提示；不直接写配置 |
| `update_config(config)` | 只更新自己命名空间，保留其他插件字段 |
| `hotkey_fields()` | 可选：全局快捷键冲突检查；返回格式参照现有快捷键设置实现 |

不要在构造时把默认值立即保存。设置页关闭/取消不能写入用户配置。正常保存经宿主配置事务；快速修改少数字段时使用 `context.patch_config(updates, expected=旧值快照)`，明确处理失败或并发冲突，不拿旧整包配置覆写新 Key 状态。

## 布局和交互底线

- 内容来源和常用设置就近显示；复杂参数折叠但保留值。预设是参数配方，不是禁用用户参数的理由。
- 参数控件变更进入模型；选择节点、展开面板或刷新预览不能反向覆盖已保存参数。
- 运行中禁用重复提交入口，但提供取消、失败原因及重新查看结果入口。
- 关闭窗口是否继续执行必须明确。若任务继续，结果归宿主后台任务/队列，不归即将销毁的局部窗口。
- `QMessageBox` 只在 GUI 线程使用；后台计算节点不得弹窗死等用户。
