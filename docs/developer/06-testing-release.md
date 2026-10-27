# 验证、截图与发布

[返回目录](README.md)

## 可重复的最小验证

在已准备好的项目 venv 中、仓库根目录执行：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m unittest -v tests.test_developer_documentation
.\.venv\Scripts\python.exe -m unittest -v tests.test_plugin_host tests.test_background_tasks
```

完整隔离模块测试入口是 `python packaging/run_tests_isolated.py`，耗时可能较长，且旧问题可能导致失败；不能把某一个新测试通过写成全仓全部通过。它按模块新进程运行，降低 Qt/原生库相互污染，但**不是自动隔离真实账号、生产配置或付费网络**。业务测试必须自行用临时数据、替身和禁网策略。

测试至少覆盖：成功、空输入、非法参数、源文件不存在、网络失败、取消、超时、重试、重复点击、窗口关闭重开、旧回调迟到、资源释放与多任务隔离。必须验证失败后的下一项仍能继续，而不只是断言抛出异常。

## 示例与截图

[`test_developer_documentation.py`](../../tests/test_developer_documentation.py) 载入文档中的插件，不将其安装进正式主窗口；验证真实宿主注册、设置页协议、节点计算和后台结果。只使用临时存储，不读取真实任务和 Key。

生成本目录示例截图：

```powershell
$env:QT_QPA_PLATFORM='offscreen'
$env:LZX_DOC_SCREENSHOTS='1'
.\.venv\Scripts\python.exe -m unittest -v tests.test_developer_documentation
Remove-Item Env:LZX_DOC_SCREENSHOTS
```

这是显式的开发动作，会覆盖 `docs/developer/assets/plugin-*.png`。正常测试不写截图。截图来自宿主实际挂载的 Qt 控件，标明隔离示例，不从生产界面复制账号/Key。字体/DPI不同可能有像素差异，更新后肉眼检查溢出和截断，不要求逐像素相同。

## 提交边界

- 文档、公开示例、截图、测试与源码可以提交；配置、凭据、历史、临时项目、个人待办不提交。
- 先看 `git status`、逐文件审查 diff，当前工作区可能有其他开发者未提交修改。
- 文档引用使用仓库相对路径，不使用开发者绝对路径或过期行号。
- 修改公共 API 时同步更新文档、完整示例及测试，记录兼容/迁移条件。
- 新增依赖需验证许可证、固定策略、原生运行栈与发行包，不为写文档引入网站构建依赖。

## 打包排除

当前 [`AssistantTool.spec`](../../packaging/AssistantTool.spec) 是数据资源显式白名单，不打包整个仓库。发行工作流只另外复制根 README、CHANGELOG、示例配置与必要图标；开发手册不在资源清单。文档示例不放进 `app_plugins` 包，因此 `collect_submodules('app_plugins')` 不会把它作为内置插件收集。

[`check_release_artifact.py`](../../packaging/check_release_artifact.py) 会拒绝项目开发文档路径；防止以后改成整目录复制时悄悄把手册、截图和示例带给用户。第三方依赖自带的合法文档/许可证不应被通用的“删所有 README”规则误删。

组内分享配置是独立的私有分发资料，不随公开源码或 EXE 发布。检查器拒绝任何层级的“组内分享配置”目录／ZIP（含日期后缀）、密盘启动绑定和配置备份；即使已经清理凭据，也不能把组内列名、分类和业务设置顺带公开。更新私有分享包时必须核对 ZIP 与展开目录一致，避免只修改目录却分发旧 ZIP。此检查只覆盖已知路径，不替代内容审查。

本机固定入口为程序根目录 `本地分发/`：内部版保留内部工作配置；外部版只维护通用基础列名，各自有 `当前配置/`、配置 ZIP 和 `配套资源/`。旧版本只进入 `历史归档/`，`分发清单.json` 记录当前包。整个根目录由 Git 忽略且被公开发行包检查拒绝；不能以“外部版”或“通用配置”为理由强行加入 Git。维护脚本必须使用清单／固定路径，不再扫描、猜测多个 config 副本。

Flow 扩展是运行必需的数据资源，不是开发文档。spec 在 `COLLECT` 后通过 [`flow_bridge_resources.py`](../../packaging/flow_bridge_resources.py) 从固定上游及桥接源码重新构建，并仅复制已校验的15项文件到 EXE 同级 `browser_extensions/flow_bridge/`。不要直接复制开发者正在使用的扩展目录，更不要复制整个仓库。主程序要求版本、构建器版本、manifest 与来源清单必须一致；缺文件、额外配置、空文件或危险资源引用均阻止打包。上游新增资源时，须同步审查资源白名单及发行包必需文件测试。

冻结程序从 EXE 同级查找扩展；`_internal` 中的 Python 模块路径不作为 Chrome 安装路径。完整发行包已带构建后的资源，运行时不需要 `tools/` 或上游源码；若 EXE 与扩展版本不匹配，应恢复完整包，不在用户机器上临时运行源码构建工具。`test_flow_release_bundle` 会真实执行本地固定构建器、检查模拟冻结目录和 ZIP 缺项；它不启动 Chrome、不发起生成，也不等于完成真实 EXE 验收。

发行包隐私检查同时覆盖节点工作流、AI 试用台、素材分配／视频检索缓存、卷内数据副本及迁移暂存。`test_release_privacy_inventory` 将源码私有目录清单与拦截规则对照，避免增加新数据目录却忘记更新发布保护；依赖自己的深层 `config/` 资源和内置公开日历仍允许携带。

```powershell
python packaging/check_release_artifact.py dist_final/AssistantTool-Windows.zip
```

打包与发布分开：本地修改或 commit 不等于授权创建 tag、推送、Release 或 PR。正式版本还需要 CHANGELOG 独立记录、原生媒体依赖、构建身份和发行包隐私检查。
