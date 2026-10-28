来源：https://github.com/dedeyuyu/FlowCue

固定提交：a5a4a3607e55c85c9c0c2e8e4700f2140eefb36d（上游应用 1.1.8，附带扩展 1.1.6，2026-10-24 发布）。
extension/ 是未修改的原件；原 LICENSE 与 LICENSE.LIST 一并保留。
上游 LICENSE 文件声明 GPL-3.0-only，但正文为自动生成的合规报告；当前用于用户本机测试，发布前仍需核对完整授权材料。

运行 tools/build_flow_bridge.py 组装 browser_extensions/flow_bridge（桥接版 1.1.60）。
桥接新增代码在 browser_extensions/flow_adapter，生成端保持上游上传、设置同步、任务媒体绑定和下载实现。
构建脚本校验固定文件哈希和唯一接入点；版本特定接入放在 tools/flow_bridge_patches.py。
更新上游后须审查接入点再更新固定版本，不能启动时自动执行最新源码。上游版本与桥接版本分别记录在生成目录 SOURCE.json 中。

2026-10-28 复审：网页状态存储白名单、本机图片精确路径授权、禁止网页关闭全部窗口的补丁位于 tools/flow_security_patches.py 和 flow_adapter/page_boundary.js。上游原件不修改。
packaging/package_flow_extension.py 从明确列出的公开输入快照构建独立安装包、完整桥接对应源码和 SHA-256 清单，并检查其与主程序包内扩展逐字节一致。不复制正在使用的扩展目录、用户配置或整个仓库。
GPL-3.0.txt 另附标准 GPLv3 许可正文；保留上游原始 LICENSE/清单并不等于第三方授权已获法律审查。
