# 聊天能力适配（1.6）

入口：AI 模型试用台顶部「参数 / 联网」。这是当前聊天入口的常用参数适配，不意味着每个 API 的全部能力，也不等于 ChatGPT 网页版功能全集。

| 服务商 | 已适配的特殊参数 | 联网 |
|---|---|---|
| OpenAI | 分模型思考等级、支持的推理模式、回答详略；温度／Top P 与推理互斥 | 支持的模型使用官方 web_search，默认关闭，可选必须搜索与调用上限 |
| Gemini | 2.5 思考预算；已核实3.x系列的思考等级 | 暂禁用：尚未实现Google要求的完整Search Suggestions展示，不仅用来源链接冒充完整适配 |
| Mistral | 已核实 Small、Medium、Large 4 等模型的 reasoning_effort | 当前Chat Completions入口未接入网页搜索 |
| Groq | GPT-OSS、Qwen 3.8等已核实模型的等级 | 当前入口未接入，不能假设所有聊天模型自带搜索 |
| OpenRouter | 根据模型目录 supported_parameters 开放思考／详略，平台可能映射等级 | 显式选择后使用web插件，可另收费；“只用免费模型”开启时禁用联网 |
| Cloudflare | 现有文字／图片聊天与通用采样 | 未确认的专属思考、联网参数保持禁用，不猜测兼容性 |

等级与预算控制不是公开模型完整内部思维过程的承诺。各项是否能用还取决于账号、项目权限与供应商实际接口；能力表不是权限授权。

## 参数与费用

- 思考、联网、Pro、输出上限可能明显增加耗时和费用。默认保留模型原始参数，联网默认关闭，Pro不自动开启。
- OpenAI内置工具调用默认上限3，可调1–20；搜索动作数取自返回记录，不把工具上限当美元上限。OpenRouter的结果数限制不是调用总数／美元预算。
- 不支持的控制灰显并保留原值；实际请求只取当前启用项。脚本／节点直接传入不支持的特殊参数会在发送前报错，而非静默忽略。
- 温度与Top P通常选一个调节；OpenAI推理模型不支持采样时两者均不发送。Gemini2.5 Pro不能用预算0关闭思考。
- 选模型、切换参数、打开设置不会生成回答；自动模型目录查询只有只读GET。一次发送只发一次生成请求，不自动换模型或失败重发。
- 模型接入后不会自动获得本机操作权限；没有开启远程Shell、代码执行、任意URL下载、账号管理或本地目录工具。

## 开发维护

`model/AIChatCapabilities.py`集中管理能力和参数组装，`advanced_options.py`负责界面；不要在主界面为某个模型写隐形默认覆盖。

新增模型先核对厂商模型卡与具体端点，不能凭API全局枚举就认定模型支持。未知能力先禁用并解释；只更新已核实家族／快照或目录元数据，并补上有效／无效参数、切换保留与请求组装用例。

`AIChatSources.py`只处理有界引用数据，禁止自动打开引用。Qt呈现层对来源标题、URL及模型正文分别转义，拒绝file/javascript/带凭据链接；不载入来源远程图片或HTML脚本。正文与引用元数据分别存入既有聊天库，无需迁移旧对话。

协议依据：[OpenAI推理](https://developers.openai.com/api/docs/guides/reasoning)、[OpenAI联网](https://developers.openai.com/api/docs/guides/tools-web-search)、[Gemini思考](https://ai.google.dev/gemini-api/docs/thinking)、[Mistral推理](https://docs.mistral.ai/inference/conversations/reasoning)、[Groq推理](https://console.groq.com/docs/reasoning)、[OpenRouter推理](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)、[OpenRouter联网](https://openrouter.ai/docs/guides/features/plugins/web-search)。
