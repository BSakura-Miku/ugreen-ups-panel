# 贡献指南

欢迎改进可读性、兼容性、协议证据和测试。请先阅读 [README](README.md)、[协议字段](docs/fields.md)及[校准说明](docs/calibration.md)，避免重新引入已经撤回的电流、负载率或续航假设。

## 本地验证

以下步骤用于开发，不属于普通 Docker 安装流程。在仓库目录中创建 Python 环境并构建前端；本地前端构建需要 Node.js 22.12+。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend run build
```

无需 UPS 即可回放测试样本。演示模式有明确标识，不查询 NUT。在两个终端分别运行：

```sh
# 终端一
.venv/bin/python -m ups_panel.collector --replay fixtures/online.hex \
  --output runtime/latest.json --calibration-config runtime/calibration.json

# 终端二
UPS_SNAPSHOT=runtime/latest.json UPS_DATABASE=runtime/history.sqlite \
  UPS_CALIBRATION_CONFIG=runtime/calibration.json .venv/bin/uvicorn ups_panel.app:app --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765`。两端使用同一 `runtime/calibration.json`，可在演示数据上验证网页保存和采集器生效状态；本地数据不提交到仓库。需要前端热更新时另行执行 `npm --prefix frontend run dev`，开发代理会将 API 请求转发到 `8765`。

```sh
.venv/bin/python -m pytest -q
npm --prefix frontend run build
docker compose config --quiet
```

源码 Docker 构建可使用 `docker build -t ugreen-ups-panel:dev .`，再执行 `python3 scripts/smoke-image.py --image ugreen-ups-panel:dev` 检查镜像。`compose.build.yaml` 仅供已经准备好快照和数据目录的开发环境覆盖使用，通过 `docker compose -f docker-compose.yaml -f compose.build.yaml up -d --build` 加载。

一个数据库只运行一个后端 worker。不要为测试停止 NAS 的 UPS 保护服务、解绑 USB、写入 UPS 指令或自动切换供电。需要硬件操作的兼容性问题，请把操作方案与纯软件复现分开说明。

## 提交问题

请提供项目版本、CPU 架构、Linux 内核、UPS 型号、原驱动名称/版本、预期与实际行为，以及最小必要日志。出现数值问题时，说明测量位置、供电模式、适配器额定电压和参考数据更新时间。

v0.8.0 可先在「诊断与说明」下载白名单 JSON，或在宿主采集器源码目录执行 `python3 -m ups_panel.doctor`；两者默认排除设备标识和自由文本。A/B 问题可附短时观察 CSV。说明当前操作与实际现象，缺少信息时保留未知，不用推断值填充。字段范围和只读自检见[诊断说明](docs/diagnostics.md)。

提交前移除用户名、主机名、内网地址、序列号、设备 ID、令牌及个人路径。完整历史可推测日常用电与设备活动；优先给出小段脱敏样本。不要上传整个 usbmon 总线转储，其中可能含同总线其他设备的流量。

## 协议或校准变更

每个新解释需写清报告偏移、端序、单位、适用模式、证据和不确定性。区分设备实读、数学派生与经验模型；器件具备某项功能不是 USB 已公开该字段的证据。

修改公式时保留原始字段，为模型或历史指标定义新身份并覆盖切换/无数据行为。给出独立验证数据，不能只提交拟合样本上的误差。不要把贴合交流插座的系数命名为输出电流系数。

网页校准变更需要覆盖输入边界、非法数值、原子保存失败、配置动态重载和估算窗口重建。保存状态与采集器生效状态分别验证，旧采集器和过期快照不能确认生效。相同配置应派生相同 `calibration_revision`，不同系数的历史不得混算；保留原始字段和既有历史，`custom` 始终标记未独立验证。

有针对性地补充会防止回归的测试；截图、文字或可逆样式调整可用构建和页面验证。Pull Request 说明最终行为、验证方式和兼容性影响，不提交开发过程中的私有档案。

## 文档与素材

对外使用正式、明确的文案；证据和实验细节放在文档或高级诊断。硬件数据给出原始来源，保留“资料规格”和“本机读取”的区别。请仅提交有权分发的素材，第三方拆解照片和厂商资料优先链接来源。

提交的贡献应适用项目的 [MIT 许可证](LICENSE)。第三方代码、依赖或素材的许可不因引用本项目许可而改变。

## 发布流程

面板镜像发布到 Docker Hub，Compose 和采集器安装脚本随 GitHub 仓库提供。GitHub Release 记录版本变化、镜像标签、升级步骤与验证结果；后续版本不再额外制作或上传源码压缩包、部署压缩包及其校验清单。已经发布的旧版附件保留，避免破坏历史下载链接。

发布前检查本次提交的公开文件，避免将个人研究档案、本地配置或运行数据提交到仓库：

```sh
python3 scripts/check-public.py --require-git
```

新增公开文件时同步更新检查清单。检查脚本只读文件，CI 直接在仓库中运行测试、验证 Compose、构建镜像并检查容器启动及接口，不再生成发布包。清单与基础敏感信息扫描不能替代人工核查、素材授权检查或 Git 历史检查。

一次版本发布按以下顺序完成：

1. 更新版本号、更新记录及双语说明，提交并通过 CI。默认 Compose 继续使用 `latest`，用户无需为每次更新手改版本配置。
2. 从该提交构建 `linux/amd64` 与 `linux/arm64` 镜像，分别运行 `scripts/smoke-image.py` 验证，再发布对应 Docker Hub 版本标签并同步 `latest`。版本标签对应固定发布内容。
3. 在同一提交创建 Git 标签和 GitHub Release，说明变更、镜像 digest、升级步骤和验证范围。只有采集器或系统服务配置变化时，才要求用户重新安装宿主机采集器。

首次安装按 README 下载仓库源码、安装宿主机采集器，再在 Docker 中创建项目。安装脚本准备数据目录，Compose 自动拉取镜像。需要升级采集器时，用 GitHub 对应版本的自动源码归档在临时目录展开，安装器通过 `--data-dir` 指向已有部署数据目录，安装后清理临时源码；不另做发布附件。日常面板更新执行 `docker compose pull && docker compose up -d`；数据库继续保留在 `./data`。

更新前端依赖后，先 `npm --prefix frontend ci`，再运行 `python3 scripts/update-third-party-notices.py`，检查并提交第三方许可汇总。脚本保留包内的许可证与 NOTICE 原文；缺失许可文件会要求人工核查。
