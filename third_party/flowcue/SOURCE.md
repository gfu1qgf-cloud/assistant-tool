来源：https://github.com/dedeyuyu/FlowCue

固定提交：e4e8f34ea6a0e0d7d9e0e87bbf7218e66d315ec2（上游应用及扩展 1.1.4，2026-10-18 发布）。
extension/ 是未修改的原件；原 LICENSE 与 LICENSE.LIST 一并保留。
上游 LICENSE 文件声明 GPL-3.0-only，但正文为自动生成的合规报告；当前用于用户本机测试，发布前仍需核对完整授权材料。

运行 tools/build_flow_bridge.py 组装 browser_extensions/flow_bridge（桥接版 1.1.18）。
桥接新增代码在 browser_extensions/flow_adapter，生成端保持上游上传、设置同步、任务媒体绑定和下载实现。
构建脚本校验固定文件哈希和唯一接入点；版本特定接入放在 tools/flow_bridge_patches.py。
更新上游后须审查接入点再更新固定版本，不能启动时自动执行最新源码。上游版本与桥接版本分别记录在生成目录 SOURCE.json 中。
