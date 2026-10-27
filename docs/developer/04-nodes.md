# 节点和工作流扩展

> 并非所有使用节点画布的窗口都是工作流。任务表结构编辑器只复用画布交互，使用“筛选器→最终数据结构”两类 UI 描述，最后编译回原 `task_table_schema`，不注册平台节点、不创建可运行工作流。实现见 [`task_schema_canvas.py`](../../PYUI/task_schema_canvas.py)、[`TaskSchemaMapping.py`](../../model/TaskSchemaMapping.py)，用户说明见[节点式结构编辑](../任务表格-节点式结构.md)。

[返回目录](README.md) · [节点示例](examples/sample_plugin.py)

## 优先从纯函数节点开始

```python
from app_plugins.api import NodePort, node_function

@node_function(kind='my_tool.uppercase', title='转大写',
    inputs=(NodePort('text', '文字', 'text'),),
    outputs=(NodePort('result', '结果', 'text'),), category='文字处理')
def uppercase(text):
    return text.upper()

# 在 MyToolPlugin.register(context) 中：
context.register_function(uppercase)
```

函数参数名须匹配输入和参数 key，不能有同名冲突；单输出返回标量，多输出返回 key 完全匹配的 dict。必须同步函数，不能靠保存的 JSON 指定任意 import、eval 或 Python 源码。

## 显式 NodeDefinition

```python
from app_plugins.api import NodeDefinition, NodeParameter, NodePort

def build(inputs, params, context):
    context.check_canceled()
    return {'result': inputs['text'] + params['suffix']}

definition = NodeDefinition(
    kind='my_tool.append', title='追加文字', execution='compute',
    inputs=(NodePort('text', '文字', 'text'),),
    outputs=(NodePort('result', '结果', 'text'),),
    parameters=(NodeParameter('suffix', '后缀', default='!'),),
    build=build, version=1, category='文字处理')
# 插件 register 内：context.register_node(definition)
```

`kind` 必须为 `plugin_id.node_name`。`version` 是该节点持久化契约版本，不是插件显示版本；变更端口含义、参数语义或输出类型时应评估迁移，不能只改 UI 标题掩盖不兼容。

### 声明命名互斥锁

注册时给 `NodeDefinition` 添加 `mutex='my_shared_resource'`，或给 `@node_function(...)` 添加相同关键字，即可自动保护该节点的 `build()`。默认空字符串不加锁；同一程序实例的所有项目、注册表和插件共用 `dict[str, RLock]`，名称完全相同（区分大小写）就互斥，不自动添加插件前缀。同线程可以重入，不同线程仍互斥。名称最多120字，不允许首尾空白或控制字符。

画布标题右侧显示矢量锁图标，悬停可查看名称。它表示执行互斥，不会禁用参数编辑。等待时显示“等待互斥锁：名称”，每100ms检查取消；等待发生在当前后台工作线程，不会弹窗或阻塞GUI。异常、取消及输出错误不会留下已持有的锁；不可强制终止持锁线程或从UI线程提前释放。

![同名锁标记与无锁节点对照，真实Qt隔离示例](assets/node-mutex.png)

锁只覆盖 **输入计算完成后的 `build()` 调用**，不覆盖 `prepare()`、上游计算、后续渲染及延迟编译。不得给只构造计划的节点贴锁图标，就声称后续模型、编码或文件写入已受保护；实际底层保护和输出目录资源声明仍然保留。当前标记 `smart_video_editor.edit`（`whisper`），保护整个智能剪辑分析节点；同名分析节点会串行，包括节点内部的缓存／探针工作，其他无锁节点与编码不因此全局串行。复用的Whisper底层细粒度保护仍有效，普通字幕及压缩计划节点不额外加锁。

名称由可信实现声明，不放入用户参数或保存锁对象，不改工作流格式。此机制不提供跨进程互斥，也不实现互斥区域、多锁调度或跨线程重入；使用不同名称的嵌套锁时，开发者仍需统一顺序以避免死锁。

### 参数与端口

- `NodePort(key, title, data_type, required=True, multiple=False, capabilities=(), type_group='')`。
- `NodeParameter(key, title, kind='text', default='', minimum=0, maximum=10000, choices=(), required=True)`；可用 UI kind 以现有编辑器实现为准，不自造字符串假装有控件。
- 常见类型：`text / number / integer / boolean / directory / files / audio / video / subtitles / operations / render_plan`。复杂数据要查看值契约和运行时验证，不把普通字典伪装成媒体。
- `any` 必须有 `type_group`，同节点同组推导为一个具体类型；固定类型不能填写组名。不是绕过类型验证的逃生口。
- `capabilities=('seek',)` 声明需要可定位资源；流不能定位时明确要求缓存，不偷偷当作本地文件。

## 提交准备与执行分开

`prepare(task, params, services)` 在提交阶段固定纯配置和资源声明；业务数据应经显式端口进入，独立文件工作流的 task 可以为 `None`。返回可 JSON 序列化的 dict；`resources` 声明资源锁，`relative_writes` 声明输入目录下的写入子目录，其他纯字段由执行时的 `context.node_prepared` 读取。相对写入不能等于输入根目录或逃出目录；自定义目录提供者须自行声明相应 resources。完整准备流程在 [`tasks.py`](../../model/node_workflow/tasks.py) 的 `prepare_job`，不要绕开其快照、类型检查与资源约束。

`build(inputs, params, context)` 在后台执行，只使用本节点输入和准备好的普通数据。不得读取 UI 当前行、调用主窗口弹窗或等待用户修正。`execution='plan'` 创建延迟计划，`'compute'` 计算；计划构造不应提前编码完整中间视频。

`inspector_factory`、`validate_parameters`、`compile_operation`、`compile_source`、`input_factory`、`output_factory` 是进阶可信注册回调；使用前查看实际调用者与对应测试，不能把 Qt 对象或这些 callable 放进工作流 JSON。

## 媒体和任务归属

[`contracts.py`](../../model/node_workflow/contracts.py) 中的关键值：

| 值 | 用途 |
|---|---|
| `TaskSnapshot` | 提交时任务身份和字段快照，不是可变 UI 行 |
| `DirectoryRef / FileList` | 显式目录和有顺序的文件清单 |
| `ResourceRef` | 文件签名或会话资源身份，不是已打开文件句柄 |
| `SubtitleTrack / VideoTimeline` | 字幕轨和延迟视频时间线，保持来源关系 |
| `FilterOperation / RenderPlan` | 延迟操作与最终渲染计划 |
| `ColorValue` | 明确的 sRGB 0–1 RGBA，不是 QColor |

`ResourceRef` 的 `storage` 可为 file/memory/stream。通过 `context.media.info(ref)`、`open_reader(ref)` 访问，不能把 `resource_id` 当路径，也不能把 BytesIO、进程或生成器塞进端口。源文件排队后改变应被签名验证拒绝；写成品先核对任务归属，禁止标题来自任务A、正文来自任务B。

预览不应推进资源池游标、修改用户参数、覆盖成品或默认调用付费 AI。恢复预览缓存不是重新提交生成任务的授权。

## 注册独有工作流类型

`context.register_workflow_type(WorkflowTypeDefinition(...))` 来自 `app_plugins.api`。先注册该插件的入口和终点节点；类型 ID、entry_kind、output_kind 都使用本插件命名空间。

必需：有效版本、标题、已注册入口/终点、可调用 `example_factory`。`preview_factory` **可以不提供**；提供时必须可调用。`execution_mode` 为 `detail` 或 `line`；line 类型若提供预览，还需要 `preview_task_factory`。`preview_plan_factory` 依赖有效预览接口。完整条件见 [`workflow_types.py`](../../model/node_workflow/workflow_types.py)。

用 `context.open_workflow_editor(workflow_id=..., workflow_type=...)` 打开宿主独立编辑器，仅 GUI 线程。用户工作流保存参数、端口和资源引用，不保存密钥、绑定当前任务、窗口或回调。迁移应幂等、去重连线、保留输入覆盖，在内存完成，不擅自重写用户原 JSON。
