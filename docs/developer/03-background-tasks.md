# 后台任务、取消与线程生命周期

[返回目录](README.md)

## 首选宿主任务，不要每个按钮创建线程池

```python
def run(payload, context):
    for index, item in enumerate(payload['items']):
        context.check_canceled()
        # process(item) 需要自己的网络/子进程超时与 finally 清理。
        context.report(f'处理 {index + 1} 项', (index + 1) * 100 // len(payload['items']))
    return {'count': len(payload['items'])}

# GUI 线程：先校验 items 非空，固定所选项目和内容。
handle = plugin_context.submit_background_task(
    title='我的批量处理', runner=run, payload={'items': frozen_items},
    kind='media', execution='parallel', resources=(), retryable=False)
handle.completed.connect(controller.on_completed)
handle.failed.connect(controller.on_failed)
handle.finished.connect(controller.on_finished)
```

**参数顺序是 `runner(payload, TaskRunContext)`**，不是反过来。示例中的 `frozen_items` 是开发者准备的普通数据，不是宿主提供的变量。任务管理器深拷贝 payload 并拒绝其中的 QObject；这不能防止闭包捕获 GUI，所以 runner 推荐模块顶层函数。

### 调度参数

| 参数 | 含义 |
|---|---|
| `kind` | 分类配额；现有 `cpu / model / media / network`。陌生名称不要用来绕过限制 |
| `execution` | `parallel` 或 `exclusive`；普通耗时任务不应全部设独占 |
| `resources` | 共享资源锁标识；相同目录写入必须声明，纯读取不要无故锁整个项目 |
| `dedup_key` | 同一 owner 下活动任务去重；复用句柄时不要重复连接同一套回调 |
| `retryable` | 是否允许宿主重试；仅对幂等、可安全重新执行的任务开启 |

目录锁使用 `model.BackgroundTasks.directory_resource(path)` 或 `directory_tree_resource(path)`。细粒度并行不等于可以去掉模型锁或任务目录写锁；多个项目本不相关却共用一个锁名，会造成“多条运行，实际串行”。

当前默认总并发3、媒体2、网络2，设置中可调范围1–8；`cpu`、`model` 分类固定1。实际吞吐还受资源锁、插件内部并发和模型安全约束影响，不能只根据显示的“运行中”数判断。

### 上下文与句柄

worker 的 `TaskRunContext` 提供 `check_canceled()`、`is_canceled()`、`report(message, percent)`、`log(message)`、`report_item(id, percent)`、`report_item_stage(id, stage)`。百分比是0–100整数，未知进度不编造细小变化。

GUI 的 `BackgroundTaskHandle` 提供 `completed(result)`、`failed(message)`、`finished()`、进度信号、`requestInterruption()` 和 `isRunning()`。它不是 QThread，不使用 `connect_thread_finished`。完成结果由 GUI 控制器接收；`completed` 不等于原生线程已完全退出。清理运行中标记使用 `finished`，并核对发信句柄是不是当前这一轮。

需要关闭窗口后仍能查看结果时，研究 `handle.set_result_handler(callback, pending=True)` 和 `show_result()`。保存的是当前进程回调，不是可重启恢复的可执行代码；历史任务记录也不自动等于可恢复全部业务。

## 消除“一个无效东西卡住全部流程”

每个等待都应有退出条件：成功、明确失败、取消、超时。每个被占用的账号、锁、槽位、临时文件都要有可验证的释放路径。

- 网络设置连接/读取超时；退避等待可取消，重试有次数上限。
- 外部进程定期核对取消、退出码与有效输出；不能只阻塞读取一根永不结束的管道。
- 单任务错误默认隔离；环境相同错误暂停对应资源，不让后续任务反复送死。
- 提交可能已收费但响应不明时保留原任务身份，先查原结果；不能自动重新生成。
- `finally` 做清理，但成品成功提交、资源轮换或扣费结算只能在确认成功后执行。
- 取消是协作请求，不承诺能瞬间打断第三方原生模型或阻塞网络读取，更不能用 `QThread.terminate()` 强杀。

## 只有必须长期监听时才自管 QThread

先读 [`QtThreadLifecycle.py`](../../model/QtThreadLifecycle.py)。主线程注册结束回调：

```python
from model.QtThreadLifecycle import connect_thread_finished, thread_is_active

worker = MyWorker(parent=owner)
owner.worker = worker
connect_thread_finished(worker, owner, 'worker', owner.on_worker_finished,
                        pass_thread=True)
worker.start()
```

回调接收已捕获的那个线程；辅助函数核对 owner 字段身份、等待原生退出并负责 `deleteLater`。不要在结果信号到达时先把 `owner.worker = None`；也不要写 `finished.connect(lambda: self.worker.deleteLater())`，它可能误删后来启动的新线程。

GUI 退出阶段不能无限 `wait()`。线程未安全结束时保留 owner/引用并暂缓退出；`stop()` 返回 `False`，不要先销毁窗口导致 `QThread: Destroyed while thread is still running`。
