# 配置、存储与公共服务

[返回目录](README.md)

## 三类数据分别处理

| 数据 | 放置与访问 |
|---|---|
| 用户设置、书签、账号、历史、恢复记录 | 当前宿主私有存储；插件用 `context.storage` |
| 本次任务输入和交付成品 | 提交快照中的任务目录/用户指定目标，按原业务授权处理 |
| 可重建的公开缩略图或临时解码缓存 | 按该功能明确缓存策略；不能夹带私有链接/提示词/令牌后声称是公开缓存 |

不要写死 `Z:`、`F:` 或某位开发者的桌面路径。密盘盘符可变化，更重要的是同盘符未必还是同一个卷。使用受管存储，让宿主检查卷身份、可写状态和容量；失败应提示，不静默把私有副本写回外部目录。

```python
# GUI 准备阶段取得插件自己的受管目录；不要把完整 PluginContext 传入 worker。
directory = context.storage
directory.ensure()
state = directory.file('state.json')
value = state.read_json() if state.exists() else {'version': 1, 'items': []}
state.write_json(value)  # 使用受管原子写入，不自己另建明文备用文件
```

`context.storage` 返回 `ManagedDirectory`，不是 pathlib.Path。它提供 `file(name)`、`directory(name)`、`ensure()`、`temporary()` 等。`ManagedFile` 提供读写、`atomic_writer()`、受管 SQLite `connect()` 等；构造相对名称时仍要验证用户输入，不能传绝对路径或 `..`。

持久化 schema 应有版本、默认值和明确迁移。测试取消/失败时不破坏旧健康文件；原文件保留到新结果校验并原子提交成功。音频流程尤其保留供达芬奇使用的普通 `task_audio.srt`；动态时间戳文件只能增量增加，不能擅自替代。

## 配置更新

读取：`context.load_config()`。程序设置页用 `load_config / validate / update_config` 协议，宿主集中保存。局部即时保存用 `context.patch_config(updates, expected=...)`，只确认本插件字段，不重新保存全部陈旧配置；该方法仅 GUI 线程。

API Key 状态可能由后台同步更新，保存事务会合并状态。重置/删除必须具有明确语义，不能保存时又把磁盘旧状态合回来。插件自己的设置不得写进别的插件命名空间。

## 服务快查

| PluginContext 能力 | 使用边界 |
|---|---|
| `parent_widget` | 创建插件 UI 的父窗口；不要据此任意访问私有属性 |
| `selected_task_rows()` / `task_targets(rows)` | GUI 收集当前任务，立即固定快照 |
| `current_task_targets()` / `current_task_table_path()` | 当前业务上下文，不在后台反复查询 |
| `log(message, level=...)` | 宿主日志；不输出密钥、完整 token、Cookie、私有文案 |
| `notify(title, message, critical=False)` | 用户可理解的完成/异常通知，避免逐词/逐文件刷屏 |
| `gemini_keys` / `open_gemini_key_manager()` | 复用现有 Key 管理，不另起一套明文 Key 文件 |
| `ai_client` / `ai_workflows` / `prepare_ai_workflow(provider)` | 主线程准备服务，worker 使用已审核的纯服务快照，不能捕获窗口 |
| `prepare_elevenlabs()` | 复用宿主 ELE 服务准备链，按能力路由，不承诺所有 Key 都有所有权限 |
| `subtitle_generation_settings()` | 复用字幕设置；模型切换和原生库安全约束仍有效 |
| `whisper_model(...)` | 不代表可以在并发 worker 任意加载/切换模型；先看现有模型锁与生命周期 |

细节见 [`PluginContext`](../../app_plugins/host.py)。网络授权不是插件注册权限：新增外发服务要明确告知发送哪些内容，真实付费测试需另有授权。

## 浏览器扩展与外部进程

Flow 主程序插件、`browser_extensions/flow_adapter` 和桥接构建产物有各自版本及协议。维护时读 [`Flow视频助手.md`](../Flow视频助手.md)、桥接 SOURCE 记录与生成脚本；不要只修改生成目录后遗忘原始适配层，也不要未经核对合并第三方源码。

连接成功、页面响应、账号确认、提交确认、生成完成、正确媒体绑定、下载完成、任务目录归档是不同状态。不能把“有一个视频元素”当作本次结果；按请求批次和媒体身份归档，避免下错视频。断线恢复先核对原请求，不能自动再收费生成。

外部进程不能只靠 `kill` 一个父 PID 假设所有编码子进程都停了。使用项目现有媒体/进程生命周期工具，检查超时、取消、Windows 进程树和临时文件收尾。
