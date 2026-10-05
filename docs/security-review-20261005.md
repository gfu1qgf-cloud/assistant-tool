# 2.0 发布安全核对（2026-10-05）

本记录是应用审查摘要，不是第三方平台通过证明，也不是“零漏洞”保证。

## 范围与措施

- Python / PyQt6 桌面程序；检查依赖、代码输入边界、模型加载、下载路径、日志、插件和 GitHub 工作流。
- 使用应用独立环境及固定版本依赖，不修改系统 Python。OpenCV-contrib 4.14.0.94 / NumPy 2.2.6 已通过隔离图片/视频读取、模型结果兼容及测试验证。
- 源码、可见 Git 历史和本地候选包按已知本机凭据比对，未发现相同私人凭据；大于 8 MiB 的历史 blob 不在该历史扫描范围。上游官方网页的公开地图标识不视为用户私人密钥。
- 发布包禁止配置、OAuth Token、日志、业务历史、库存/索引及锁文件；完整性检查要求模板、图标、播放器和 FFmpeg。
- 发布使用隔离测试、CodeQL security-extended 和 GitHub 构建证明；具体运行结果以对应版本 Actions 为准。包内 BUILD_INFO.json 记录源码提交与实际依赖。

## 保留风险与已作决定

- PyTorch 2.10 的 .pt2 与 torch.jit.script 条件性公告仍保留；应用当前源码未使用这两个入口。用户已决定保留验证过的 torch/torchaudio 配套版本，只加载可信模型。
- 本机旧 Python 基础解释器迁移单独安排；虚拟环境不会修补基础解释器。GitHub 构建使用 setup-python 提供的 Python 3.10，包内记录实际补丁版本。
- 本机凭据格式保持兼容，不擅自轮换/删除密钥；避免分享本机配置或敏感旧归档。
- 插件与 Fusion 预设不是沙箱，只使用可信来源。stable-ts 已归档，算法替换需要另做字幕质量验证。
- OpenCV 内嵌第三方原生库不受 pip-audit 完整覆盖；新 libpng 1.6.57 越过先前 1.6.54 / 1.6.56 的修复版本，但仍在另一条 1.6.59 修复公告的版本范围。该公告要求不完整解码时提前 png_read_end；已核对 OpenCV 4.14 普通 PNG 先读取图像后 read_end，APNG 使用 progressive API，初步不符合该调用条件，不是所有输入路径的形式化证明。
- OpenCV 内嵌 FFmpeg 与独立运行库是两份组件；已核对 4.14 官方源码固定的第三方二进制提交及 FFmpeg 库版本，仍不能仅凭版本号保证所有上游补丁存在。

## 主要来源

- [OpenCV 4.14 wheel](https://pypi.org/project/opencv-contrib-python/4.14.0.94/)
- [libpng 公告及利用条件](https://github.com/pnggroup/libpng/security/advisories/GHSA-qvg3-h654-xq3j)
- [OpenCV 4.14 PNG 实现](https://github.com/opencv/opencv/blob/4.14.0/modules/imgcodecs/src/grfmt_png.cpp)
- [OpenCV 固定 FFmpeg 二进制来源](https://github.com/opencv/opencv/blob/4.14.0/3rdparty/ffmpeg/ffmpeg.cmake)
- [PyTorch .pt2 公告](https://github.com/advisories/GHSA-33x2-ppm4-v46v)
- [PyTorch JIT 公告](https://github.com/advisories/GHSA-rrmf-rvhw-rf47)

未触发真实云端表格写入、付费音频生成或用户 Resolve 项目导出；不能把连线测试当作所有远程服务已经成功。
