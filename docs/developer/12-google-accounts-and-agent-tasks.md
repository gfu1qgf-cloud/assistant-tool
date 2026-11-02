# Google 账号管理与 Agent 任务交接

## 已实现的主程序接口

入口：**程序设置 → Google 账号**。不依赖某个插件启用。

1. 导入 Google Cloud 下载的“桌面应用”OAuth 客户端 JSON。服务账号文件、Web 客户端和非 Google 授权地址不接受。Google 账号本身不能代替 OAuth 应用客户端；客户端文件仅需配置一次，可登录多个账号。
2. 勾选需要的表格／网盘／文档权限，点击“登录 / 补充授权”，在系统浏览器选择账号。使用 PKCE、随机回调路径、state 验证和仅绑定 127.0.0.1 的临时端口。等待最多三分钟，令牌兑换、身份核验和后续 API 请求各有网络超时。
3. 选择各功能使用的账号，点击“保存功能分配”。这些操作独立、立即保存；取消程序设置不会撤销已经完成的账号操作。主设置窗口的 config.json 不持有令牌。

## 插件注册授权用途（API 2）

账号管理 UI 不再维护插件用途或服务列表。各插件在 `register(context)` 中声明自己的用途；
表格监视也已独立为 `sheet_monitor` 插件，主程序不再维护具体业务用途声明。注册阶段没有登录、网络或 Token 读取。

```python
from app_plugins.api import GoogleAuthorization, load_google_service

def run_job(payload, run):
    run.check_canceled()
    service = load_google_service(payload['google_account'])
    # 在 worker 内使用 service；不要跨线程共享同一个 service 实例。
    # 例：service.tasklists().list(maxResults=10).execute()
    return {'message': '完成'}

class MyPlugin:
    plugin_id = 'my_plugin'
    required_api_version = 2

    def register(self, context):
        self.context = context
        context.register_google_authorization(GoogleAuthorization(
            purpose_id='tasks', title='我的插件 · 任务列表',
            api='tasks', version='v1',
            scopes=('https://www.googleapis.com/auth/tasks.readonly',),
        ))

    def submit(self):
        # GUI 提交时冻结账号，仅两个标识和声明指纹，不携带凭据。
        request = self.context.prepare_google_account('tasks')
        self.context.submit_background_task(
            title='读取任务列表', runner=run_job,
            payload={'google_account': request}, kind='network',
        )
```

- 完整用途 ID 自动为 `my_plugin.tasks`；其他插件使用相同局部 ID 也不冲突。重复声明、兼容别名冲突、无效 API/版本或非 Google OAuth 权限地址会被拒绝。
- `api`、`version`、`scopes` 由插件声明，不限于 Sheets/Drive/Docs。权限为不可变 tuple，支持一个用途需要多个权限；UI 仅允许权限齐全的账号被分配。API 是否已在 Cloud 项目启用、账号是否有云端资源权限，仍由 Google 决定。
- `context.prepare_google_account` 只允许在 GUI 主线程使用，返回普通可序列化 dict；后台使用 `load_google_service`。排队后账号发生变化、用途未注册/被禁用、API/版本/权限声明改变时拒绝执行，不自动换号或申请权限。
- 禁用插件后隐藏其用途，但不删除账号或绑定；重新启用恢复。插件注册抛错时，宿主回滚它的授权声明，避免留下幽灵条目。已有 API 1 插件仍可运行；使用新接口的插件应声明 API 2。
- `allow_legacy=True` 仅改变未分配选项的说明，供确实保留旧授权入口的插件使用，不会自动借用其他账号。新插件通常使用默认值 `False`。
- `legacy_bindings=('旧标识',)` 只用于已有功能的兼容，主程序读取旧绑定但不擅自迁移文件；保存新分配后使用规范用途 ID，清除该用途的旧别名，避免取消绑定后又恢复旧号。新插件不需要此参数。插件是可信代码扩展，不是权限隔离沙箱；不能把注册接口当作对恶意插件的安全边界。
- 各用途的所需权限随绑定保存到加密库；即使插件暂时卸载，重新登录也不能悄悄缩减其已绑定账号的权限。原客户端和 Token 文件保持原样，注册声明不写入业务 config.json。

以下旧入口仅供历史适配，不作为新插件开发方式。

若 Google Cloud 应用仍处于外部“Testing”状态，登录账号需加入测试用户，相关离线令牌可能在七天后过期；不能把这个限制误报成程序保存失败。各 API 还需在对应 Cloud 项目启用。公开分发自有 OAuth 客户端时，应另行核对 Google 对所申请权限的验证／发布要求，不在程序里绕过。

`model.GoogleAccounts.GoogleAccountManager` 管理客户端、账号、权限、绑定和刷新结果。`snapshot()` 仅返回 UI 所需的账号标识、邮箱、权限、客户端名称和绑定，不返回密钥。所有新数据保存在宿主数据根的 `GoogleAccounts.json`，整个内容先经过当前 Windows 用户 DPAPI 加密，再通过宿主存储原子写入。临时文件同样只有密文；启用 VeraCrypt 时，文件还在已验证的加密数据卷内。该文件列入 Git 忽略和发行包隐私检查。

DPAPI 授权绑定当前 Windows 用户环境；复制密盘到另一台电脑不保证可解密，需重新登录。它防止离线明文泄露，不隔离同一 Windows 用户下运行的恶意代码。原始导入文件由用户管理，本功能不擅自移动或删除它；旧版独立授权文件也没有自动删除。不要声称历史明文凭据已全部清除。

移除账号会将其原绑定功能标为“停用”，不会自动回退到旧版授权。用户可以主动选择“暂不接管（保持原授权）”恢复旧模式；这与没有配置过新管理器的兼容路径不同。

`load_managed_service('sheets'|'drive'|'docs', profile='历史用途标识')` 先解析注册声明及其兼容别名，按声明取得 API 和权限；未注册的旧调用保留原三种服务适配。不把项目路径作为标识。无绑定返回 `None`，由旧入口兼容原授权。有绑定但续期失败、权限不足、存储不可用或文件损坏时抛错，绝不回退到另一个账号。请求取得账号快照后不会因为 UI 切换绑定而中途换号；续期保存核对账号授权版本，删除／重登录后的旧响应不能复活或覆盖账号。

现有 `GoogleSheetsHelper.load_sheets_service(config, config_prefix)` 按前缀分别使用审核表、任务提交表和表格监视的绑定；`GoogleDriveHelper.load_drive_service(profile='task_delivery')` 对应任务交付网盘，不使用 AI Agent 的账号；文档格式化使用 `document_formatter.docs`。旧配置中按表格前缀保存的授权路径在未接管时继续生效，网盘浏览器自身的独立只读授权未改变。

账号页调用 `integrate_legacy()`：在当前宿主存储中读取原有网盘、表格两套客户端／Token，以及明确配置的其他表格前缀授权；校验客户端匹配后，将授权副本加密登记到列表。不发网络请求、不猜邮箱、不删除或改写原文件，也不自动将这些副本绑定给原功能。原功能默认仍读取原有独立文件；需集中管理时，用户可为某个用途单独选择导入副本。列表明确标注“账号未核验”，不能把本地导入当成 Google UserInfo 身份核验。重复打开不重复导入，移除后不自动复活；导入的是当时的授权副本，日后补充授权应按当前使用模式操作。

兼容此前新增账号页的错误全局分配：旧全局 `sheets` 只由 AI Agent 插件声明为自身的兼容别名，已有明确的新绑定或停用优先，不应用到原有审核、提交、监视和上传功能；旧全局网盘／文档绑定不再覆盖原授权。原审核和任务提交表的绑定分别由任务交付插件声明兼容别名；表格监视插件使用 `sheet_monitor.sheets`，兼容之前的 `core.sheet_monitor` 绑定，不在账号页写死插件列表。

权限按用途选择，目前为现有功能使用的表格读写、完整网盘权限、文档编辑；网盘完整权限包含管理和上传，不只是下载。显示的邮箱必须来自 Google UserInfo 的已验证身份；原授权导入只显示来源标签。普通网络请求不能自动弹浏览器索取更多权限；应明确提示用户回到设置登录。OAuth 权限不等于目标文件夹的访问权限，`insufficientParentPermissions` 表示当前请求账号不能在指定父目录写入；不应通过换号重试、改变共享或创建其他目标目录来隐藏错误。

## Agent 协作表设计（方案，尚未接入自动领取／下载）

以 **一行一个可交付文件** 为单位；同一业务任务可以有多行，不能把多个成品塞在一个单元格。程序创建不可变的作业 ID，本地保存作业 ID → 业务任务目录的映射；表格排序、插行后仍按 ID 找任务，不能按行号关联。

| 列 | 写入方 | 用途 |
|---|---|---|
| 作业 ID | 程序 | UUID，唯一且不可修改，不等于任务编号 |
| 所属任务编号 | 程序／用户 | 对应现有任务，例如 1002；不同项目可能重复，不能单独做主键 |
| 任务名称 | 程序／用户 | 便于人工通览 |
| 任务类型 | 程序／用户 | 图片／视频／音频／文案等 |
| 任务说明／提示词 | 程序／用户 | 明确要做什么，可人工填写 |
| 素材 Drive 链接 | 程序／用户 | 输入素材；若多个素材，使用明确清单或专门素材表 |
| 输出要求 | 程序／用户 | 时长、画幅、分辨率、格式等；机器协议确定后再结构化 |
| 优先级 | 程序／用户 | 高／普通／低 |
| 执行状态 | 协调程序／Agent | 待领取／已领取／执行中／已完成／失败／已取消 |
| 执行 Agent | 协调程序 | 已确认领取者，不以自由填写的名字实现锁 |
| 成品 Drive 链接 | Agent | 一个文件链接或文件 ID，不填本地路径或整个文件夹 |
| 处理备注／错误 | Agent／程序 | 可读失败原因、待确认事项，不放 Token 或私密授权信息 |
| 归档状态 | 程序 | 未下载／下载中／待重试／待复核／已归档 |
| 最后更新时间 | 各自操作方 | 使用带时区的时间 |

折叠的协调字段：执行批次 ID、领取令牌、领取时间、租约到期时间、心跳时间、期望文件名／版本、已归档文件 ID／校验和。它们防止旧 Agent 回报覆盖新一次重试；不是让用户手工维护的配置。

可另设“交付记录”工作表保存每次尝试，主表保持当前状态，避免失败／重做覆盖历史。不要将 OAuth 密钥、Windows 绝对路径、密盘盘符放进共享表。

## 领取与下载的可靠性约束

- 表格不是具有比较交换语义的任务数据库。多个 Agent 先读“空闲”再各自写名字，会发生重复领取；批量写入的原子性不等于“条件成立才领取”。必须通过统一协调入口（主程序，或使用 LockService 的 Apps Script）发放领取令牌，再执行。实际采用哪种入口应根据 Agent 能力确认。
- Agent 完成仅表示成品已上传，不表示交付已完成。写入成品 Drive 文件 ID／链接并保留执行批次；程序核对作业 ID、批次和文件权限后下载。
- 目标目录来自本地主程序的任务快照，不接受表格中的任意绝对路径或 `../`。采用 `.part` 下载、校验、同目录原子提交；已存在的合格成品不覆盖，不因网络重试重新生成。
- 下载使用有权读取成品的已绑定 Google 账号。Agent 必须将文件分享给该账号，或放到该账号可访问的共享目录；“浏览器里能看见”不等于此 API 账号有权下载。不要求无条件“任何人可访问”。
- 文件不存在／尚未授权先标明原因，只阻塞该作业；有限重试和退避，不占住全部队列。下载后检查文件类型、大小，必要时核对时长／画幅／分辨率，异常进入“待复核”。
- 租约超时先检查原 Agent 和原成品；生成是否成功不明时不得自动重新提交可能收费的生成。过期回报不能改变新批次，但应保留供找回。
- 本设计尚未创建在线表格、运行 Agent、调用真实账号、下载真实成品或安装持续监视任务；账号管理就绪不等于协作调度器已经实现。

## 官方参考

- [OAuth 安全实践](https://developers.google.com/identity/protocols/oauth2/resources/best-practices)
- [桌面应用 OAuth](https://developers.google.com/identity/protocols/oauth2/native-app)
- [Sheets 批量请求](https://developers.google.com/workspace/sheets/api/guides/batch)
