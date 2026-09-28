# 应用插件接口

当前插件接口版本为 `1`。内置插件由主程序显式安装，暂不从任意目录自动执行第三方 Python 文件，避免未知代码在启动时获得业务配置和本地文件权限。

插件可以注册四类扩展：

- `main_menu`：显示在主窗口的“插件”菜单中，也支持可勾选的开关命令；
- `tools_menu`：追加到主窗口现有的“工具”菜单中；
- `task_context_menu`：显示在任务列表右键菜单中，回调会收到选中行；
- `PluginSettingsPage`：显示在“程序设置”中，由页面控制器负责加载、校验和保存配置。
- `PluginTabPage`：由主程序挂载到已有或新增的主界面标签页；插件只提供 QWidget，不直接修改主窗口布局。

插件生命周期为 `register(context) -> start() -> apply_settings(config) -> can_close() -> stop()`。主程序通过 `PluginContext` 提供日志、桌面通知、配置读写、选中任务行和安全的任务目录解析，不要求插件直接依赖 `MainDialog` 的内部字段。

新内置插件放在 `app_plugins/builtin/`，并在主窗口安装。插件 ID、命令 ID 和设置页 ID 必须稳定且唯一；需要更高接口版本时应声明 `required_api_version`，不能静默降级。

当前内置插件包括库存与素材管理、图片智能分类、Chrome 启动器、切分音频和智能剪辑。图片智能分类插件通过后台懒加载 CLIP 模型先生成可人工修改的分类预览，再由用户确认复制或移动；设置保存在 `image_classifier_settings`，模型不得在程序启动阶段加载。切分音频插件在任务右键菜单中处理所选任务目录，并在“工具”菜单提供支持文件/文件夹拖拽的批量窗口；参数统一保存到 `audio_splitter` 配置节。Chrome 插件只接管窗口生命周期、菜单入口和全局快捷键；继续使用历史配置键 `chrome_preset_websites`、`chrome_profile_groups`、`chrome_profile_iterator` 和 `chrome_next_global_hotkey`，升级时不得清空或另建平行配置。

“智能搜图”插件索引库存管理、人物素材和在插件设置中指定的额外图片库文件夹，不复制原图，并递归读取子目录。首次使用时后台加载固定版本的中文 CLIP 安全权重；之后用文件大小和纳秒修改时间增量计算，路径、缩略图和特征缓存在本机 `SmartImageSearch/`（Git 忽略）。图片移出后立即注销对应索引项，不重算其他图片。配置键为 `smart_image_search`；更换模型版本必须重新生成该版本的特征。插件启动阶段不扫描图片、不加载模型。

搜图覆盖当前索引内的全部可用图片，并按 `result_limit` 每批展示，默认每批 100 张；该设置不再截断总结果。界面会分别显示首次模型加载、条件编码和索引检索的耗时，缩略图按原始宽高比居中呈现。

缩略图缓存使用版本化的高清文件（最大 400×600，适合竖图预览）。旧缓存会在后台增量扫描时升级，但不重新计算模型特征；模型索引及原图保持不变。

搜索结果支持选中多图后拖到资源管理器，默认提出移动，按 Ctrl 可复制。只有目标确认移动后才移除原图及对应索引；Windows 的 `TargetMoveAction` 由目标自行移动，来源不重复删除。

智能剪辑插件拥有分析/导出线程、核对窗口、待处理记录和设置页。宿主只提供任务路径、字幕参数、共享 Whisper 模型与日志服务；历史配置键 `smart_video_editor`、`smart_video_pending_reviews` 以及旧的 `model.SmartVideoEditor`、`PYUI.smart_video_editor_pyui` 导入路径继续兼容。

达芬奇遥控器通过 `PluginTabPage` 复用主界面的遥控器标签。三个旧脚本的处理函数保存在 `davinci_legacy/`，其 Fusion UI 入口不由软件调用；插件用 PyQt 显示参数、进度与字幕异常列表，并通过 `main.py --davinci-worker` 在独立进程连接当前 Resolve。达芬奇和当前项目/时间线必须处于可用状态。对轨道清空、添加水印、自动渲染等可能改变时间线或任务队列的操作，界面会先确认。
