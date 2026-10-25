# 架构与开发环境

[返回目录](README.md)

## 职责分层

```text
main.py                         启动、日志、Qt 和安全退出入口
  PYUI/main_pyui.py              宿主窗口：组装、任务选择、公共服务协调
    app_plugins/host.py          注册、挂载、启停、后台队列和公共上下文
      app_plugins/builtin/...    插件业务、窗口、配置适配、专属节点
        model/...               无 GUI 的模型、数据、媒体、网络与工作流服务
QTUI/...                        界面定义；不放耗时业务
packaging/...                   依赖、构建、发行包检查
docs/developer/...              开发手册与示例，不随 EXE 发布
```

这是现有架构的职责地图，不表示旧模块已全部完成迁移。例如主窗口仍有历史兼容入口。新增功能优先使用扩展接口，不继续扩大这些入口。

## 环境与启动

发行 CI 当前使用 **Python 3.10**，Qt 使用 **PyQt6**，业务层统一从 `qt_compat` 导入 Qt。第三方包提示将来弃用 3.10，不代表可以单独升级解释器：Torch、音视频库、Qt 和原生 DLL 必须一起验证。

在仓库根目录执行；以下命令会安装依赖，只在准备开发环境时运行：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

使用同一套 Python / venv 测试，不把其他解释器的 `site-packages` 拼进生产启动路径。需要打包时另读 [`packaging/AssistantTool.spec`](../../packaging/AssistantTool.spec) 和[测试发布章节](06-testing-release.md)。首次启动可能涉及已有配置和密盘流程；不要为了测试复制生产凭据到仓库。

## 新功能放在哪里

小插件可用单个模块；复杂插件建议：

```text
app_plugins/builtin/my_tool/
  __init__.py       仅导出插件类
  plugin.py         注册与生命周期、持有控制器
  ui.py             Qt 窗口、控件，尽量薄
  settings.py       配置规范化和版本适配
  engine.py         纯业务，接收快照和取消上下文
  workflow_nodes.py 可选：专属节点注册
  README.md         用户使用说明
tests/test_my_tool.py
```

无 GUI 的跨插件能力才进入 `model`。不要让核心工作流模型反向 import 某个插件窗口，也不要让插件互相偷读对方对话框字段。需要共享能力时先确认宿主已有服务；确实缺少再增加小而明确的公共接口。

## 安装与兼容性

首次启动由 `PYUI/first_run_pyui.py` 和 `model/FirstRun.py` 在私有服务之前处理。缺配置且没有旧私有数据时，用户明确选择创建方式；发现旧数据或既有 VC 绑定则走恢复/原绑定，不自动替换为空配置。新 VC 配置通过 `ProtectedStorage.initialize_fresh` 在验证卷内创建，不借道外部明文文件；原 `migrate` 仍要求已有主配置。Whisper 在线加载失败由 `PYUI/startup_failure.py` 提示并安全退出，已有配置保留；原离线模式本地功能继续可用的策略保持不变。

当前没有扫描任意文件夹自动执行插件、pip 热安装插件或进程隔离插件的机制。内置插件先在 [`builtin/__init__.py`](../../app_plugins/builtin/__init__.py) 导出，再在宿主安装区显式 `self.plugin_host.install(MyToolPlugin())`。这是新增插件通常需要的少量宿主接线，不把业务实现写进去。

典型接线如下（`MyToolPlugin` 是你自行编写的类，不是已存在的内置插件）：

```python
# app_plugins/builtin/__init__.py：按现有导出风格添加
from .my_tool.plugin import MyToolPlugin

# PYUI/main_pyui.py：在现有插件导入及安装区分别添加
from app_plugins.builtin import MyToolPlugin
# MainDialog 初始化、plugin_host 已建立后：
self.my_tool_plugin = self.plugin_host.install(MyToolPlugin())
```

若该导出文件维护 `__all__`，同步加入新类。首次接入后正常重启，检查插件菜单、程序设置和节点注册；不要在运行中的模块上手动反复调用 register。

`plugin_id` 必须唯一并长期稳定，建议小写下划线；存储命名空间还有格式校验。宿主拒绝要求更高 API 版本的插件。`register()` 失败会撤回它新增的登记，但**不会撤销插件自行发出的网络请求或文件写入**，所以注册阶段只声明接口，不做业务副作用。

设置中禁用插件通常需要正常重启；不要承诺任意插件能安全热卸载。关闭插件窗口不等于停止宿主任务；退出程序才统一协调停止。
