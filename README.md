# 辅助小工具

一个面向 Windows 的本地桌面效率工具。它提供任务表读取、字幕与语音处理、浏览器顺序启动、表格链接监视、库存提醒，以及可选的结果整理和云端写回功能。

## 隐私与配置

仓库和发布包不包含任何真实 API Key、OAuth 凭据、Token、表格链接、客户数据或历史运行记录。首次使用时，将 `config.example.json` 复制为 `config.json`，再通过程序设置填写本机参数。

Google 服务需要用户自行创建 OAuth 桌面客户端，并把相应凭据文件放在程序目录中。程序生成的 `config.json`、Token、监视状态、库存状态和提交日志均已被 Git 忽略。

可选的 AI 视频检测目标、说明、规则和结果复核关键词全部由本机配置提供，源码不包含具体业务判定规则。

## 运行环境

- Windows 10/11
- Python 3.10（从源码运行时）
- Google Chrome（仅浏览器启动功能需要）
- 可选的外部视频编码器及预设（仅启用视频压缩时需要）
- 首次使用字幕识别时需要联网下载所选 Whisper 模型

从源码运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

## 发布包

正式发布由 GitHub Actions 在 Windows runner 上使用 PyInstaller 构建。Release 中的 ZIP 由 `github-actions[bot]` 上传，并带有 GitHub Artifact Attestation。

下载后解压整个目录，复制并填写 `config.example.json`，然后运行 `AssistantTool.exe`。不要只移动 EXE；旁边的 `_internal` 目录是运行所需依赖。

时间线播放器使用固定版本的官方 mpv Windows 运行库。发布构建会从官方
GitHub Release 下载并校验 SHA-256；校验失败或文件缺失时会直接终止打包，
不会生成缺少播放器的发布包。

验证发布包来源：

```powershell
gh attestation verify AssistantTool-Windows.zip --repo gfu1qgf-cloud/assistant-tool
```

如果从其他仓库下载，请将上面的仓库替换为实际下载来源。
构建证明必须属于下载来源仓库，不能跨仓库复制证明。

发布包根目录的 `BUILD_INFO.json` 记录版本标签、源码提交和实际依赖版本。
核对其中的 `source_commit` 与 Release 标签指向的提交，可确认源码和安装包对应。
v2.0.0 使用通过隔离媒体读取及模型兼容测试的 OpenCV 4.14 / NumPy 2.2；
不会改动系统 Python 或其他软件环境。原生第三方库不属于 pip 漏洞扫描的完整覆盖范围，
不能据此宣称零漏洞；基础 Python 迁移仍单独安排。

## 安全检查与发布

GitHub 在提交和 Pull Request 上运行 CodeQL（security-extended）；每周继续扫描。
Dependabot 跟踪依赖与 Actions 更新，仓库启用密钥扫描与推送保护。
创建新的 `v*` 标签会自动运行完整隔离测试、构建、发布包隐私检查、生成构建证明和发布。
发布完成后再运行一次 CodeQL，确保扫描结果晚于新 Release 的发布时间。
不手工上传发布附件，不公开本机配置、Token、日志或模型缓存。

当前 PyTorch 2.10 配套版本仍有已记录的条件性公告。程序不直接使用公告涉及的
`.pt2` 加载与 `torch.jit.script`；保留已验证版本，不自动跳过测试或升级不兼容依赖。
请只加载可信模型、插件和 Fusion 预设。扫描通过不代表不存在安全风险。

## 更新日志

在主界面点击“关于 → 更新日志”查看按发布版本整理的功能和修复，也可阅读发布包中的 [CHANGELOG.md](CHANGELOG.md)。仅为修复构建或测试而产生的版本会明确标记。

每次发布前，必须在 `CHANGELOG.md` 为对应标签补充具体改动条目。发布工作流会验证该版本记录，用它作为 GitHub Release 正文，并把完整更新日志放进发布包；缺失或仍含占位文字时停止发布。尚未发布的本地改动不得补算到旧版本。

## 测试

```powershell
.\.venv\Scripts\python.exe packaging/run_tests_isolated.py
```

## 许可

除非仓库所有者另行提供许可证，否则本项目保留全部权利。
