# US3000 电力监控

**简体中文** · [English](README.en.md)

<p align="center"><img src="frontend/src/assets/us3000-logo.png" width="80" height="80" alt="US3000 Monitor Logo" /></p>

![US3000 电力监控面板预览：演示数据](docs/assets/dashboard-demo.png)

*预览使用本地演示数据，设备标识为 DEMO，不包含实际 NAS 数据或序列号。*

为 UGREEN US3000 提供供电状态、电量、电芯、电压与历史趋势。宿主机通过 Linux usbmon 观察原 UPS 驱动已经产生的数据，Docker 容器提供页面和只读 API。页面运行时不依赖智能插座、Home Assistant 或外部 CDN。

这是社区项目，与 UGREEN 无隶属或认证关系。**NAS 原有 UPS 服务继续负责断电保护与关机；本项目不承担电源保护。**

## 可以查看什么

| 内容 | 数据性质 |
| --- | --- |
| 外部供电、充电、电池供电状态 | 设备状态，已在一台 US3000 上观察到对应切换 |
| 电量、输入/输出电压、电池组及四节电芯电压 | 设备报告值，经过范围校验 |
| 电芯压差 | 最高与最低电芯电压之差，不等同于电池健康度 |
| 电池充放电电流及功率 | 私有协议候选解释，测点或比例仍有不确定性 |
| 交流输入与电池供电功率估算 | 可选的单机经验模型，默认关闭 |
| 历史曲线、供电与连接事件、CSV | 本地 SQLite 聚合历史；缺失数据保留为空 |
| 硬件档案与高级诊断 | 产品/拆解资料、来源链接、原始字段及 NUT 状态 |

不提供远程关机、插座控制、UPS 参数写入、自动断电测试。未经验证的温度、负载率、剩余续航和健康度不会作为有效指标展示。功率估算不是直流功率表读数，详情见[校准说明](docs/calibration.md)。

## 兼容性与安装前提

- Linux x86_64、systemd、Python 3.10+、Docker Compose v2；目前硬件验证范围为单台 US3000 与 19 V 适配器，其他架构/固件尚需验证。
- 内核提供 usbmon，且可创建 `/dev/usbmonN`。本项目使用二进制接口，无需为页面挂载 debugfs 或 USB 设备。
- 原有 NUT/系统 UPS 驱动已经连接设备，并持续读取完整的私有 `0x71` 报告。**NUT 能显示电量，不一定意味着它会读取这些报告。** 本项目不会主动发起私有读取。
- 默认识别 USB VID:PID `2b89:ffff`。同时连接多台匹配设备时，需要通过序列号明确选择。
- 页面和 API 没有内置登录；默认只绑定 `127.0.0.1`。局域网访问请限制到受信任网络，远程访问使用带认证的反向代理或 VPN。

## 部署

先下载[发行版本](https://github.com/BSakura-Miku/ugreen-ups-panel/releases)，或克隆仓库：

```sh
git clone https://github.com/BSakura-Miku/ugreen-ups-panel.git
cd ugreen-ups-panel
```

在项目根目录执行以下命令。采集器安装需要 root；下列脚本只安装、重启本项目的服务。

```sh
# 1. 安装宿主机采集器。验证窗口内没有新帧时安装失败；升级时尝试恢复旧采集器。
sudo sh scripts/install-collector.sh
systemctl status ugreen-ups-collector.service

# 2. 构建并启动页面。默认仅在 NAS 本机监听。
cp .env.example .env
docker compose up -d --build
docker compose ps
```

默认端口为 `9086`。需要局域网访问时，编辑 `.env` 中的 `UPS_BIND_IP` 为 NAS 的实际局域网地址，再执行 `docker compose up -d`。`.env` 只用于 Compose；采集器参数由宿主机的 `/etc/ugreen-ups-panel.env` 管理。

首次安装默认校准配置为 `none`，不套用开发样机的功率系数；升级时保留已有配置。需要选择 USB 序列号或修改 NUT 目标时，在 `/etc/ugreen-ups-panel.env` 设置对应参数，再重启本项目采集器。不要把序列号或本地配置提交到仓库。

```ini
UPS_CALIBRATION_PROFILE=none
# UPS_SERIAL=your-device-serial
# UPS_NUT_TARGET=your-ups-name@localhost
```

```sh
sudo systemctl restart ugreen-ups-collector.service
curl --fail http://127.0.0.1:9086/api/health
```

`service: ok` 表示 HTTP 服务可响应，`capture_fresh` 才表示采集是否新鲜。页面会单独提示采集、NUT 查询和历史存储状态。不要用容器健康状态替代 NAS 的 UPS 保护状态。

容器使用 UID `10001`、只读根文件系统、移除全部 capabilities，仅读取快照目录并写入自己的数据卷。采集器需要访问宿主机 usbmon，采用受 systemd 限制的 root 服务。usbmon 在内核中观测所在 USB 总线，软件再筛选目标设备；这一权限范围大于单个 UPS，见[安全说明](SECURITY.md)。

## 本地演示与开发

无需 UPS 即可回放测试样本；演示模式有明确标识，不会查询 NUT。本地构建前端需要 Node.js 22.12+；使用 Docker 部署时镜像已包含构建环境。

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock -r requirements-dev.txt
npm --prefix frontend ci
npm --prefix frontend run build

# 终端一
.venv/bin/python -m ups_panel.collector --replay fixtures/online.hex --output runtime/latest.json

# 终端二：一个数据库只运行一个后端 worker
UPS_SNAPSHOT=runtime/latest.json UPS_DATABASE=runtime/history.sqlite \
  .venv/bin/uvicorn ups_panel.app:app --host 127.0.0.1 --port 8765
```

打开 `http://127.0.0.1:8765`。热更新可另运行 `npm --prefix frontend run dev`，开发代理会将 `/api` 转发到端口 `8765`。

```sh
.venv/bin/python -m pytest -q
npm --prefix frontend run build
docker compose config --quiet
```

## 历史与备份

- 实时报告在已验证设备上约每 2 秒到达；样本或采集心跳超过 10 秒未更新即显示过期。
- 10 秒聚合保留 7 天，60 秒聚合保留 90 天，事件保留 180 天；当前保留周期没有设置页面。
- 历史查询按时间范围降采样。CSV 对应查询后的聚合值，**不是逐帧 USB 记录**。
- 聚合约每 10 秒提交一次；突然掉电可能丢失最后一个提交周期。不同测点、模型和质量状态分开聚合，不回填旧值。
- 持续写盘失败时，待写队列设有上限并优先保留最近数据；`storage_dropped_buckets` 表示因此丢弃的桶数。磁盘故障不会变成无限增长的内存缓存。
- 原始 USB 总线数据不长期落盘。请勿在运行时删除 `/run/ugreen-ups-panel`，容器绑定依赖该目录。

在线备份应使用 SQLite 备份接口，避免只复制正在写入的数据库主文件：

```sh
docker compose exec -T panel python -c "import sqlite3; s=sqlite3.connect('/data/history.sqlite'); d=sqlite3.connect('/data/backup.sqlite'); s.backup(d); d.close(); s.close()"
docker compose cp panel:/data/backup.sqlite ./backup.sqlite
```

恢复时停止 `panel`，将备份放回数据卷中的 `/data/history.sqlite`，删除同目录旧的 `history.sqlite-wal` 和 `history.sqlite-shm`，确认文件属于 `10001:10001` 后再启动。不要在数据库仍有写入时替换文件。

## 升级、回退与卸载

先备份数据库、Compose 配置和采集器配置。采集器发布目录位于 `/opt/ugreen-ups-panel/releases/`，`current` 指向当前版本，`previous` 保留上次版本。

**0.3.0 将数据库升级到 schema 2。回退到只支持旧结构的版本时，需要恢复升级前的数据库备份；不要直接让旧版写入升级后的数据库。** 详见[发布说明](docs/DELIVERY.md)。

```sh
sudo sh scripts/install-collector.sh
docker compose up -d --build

# 需要回退采集器时
sudo sh scripts/rollback-collector.sh
```

面板回退需要升级前保存的旧镜像或旧版本源码。历史卷不会随容器重建删除，跨版本恢复应先核对数据库兼容性。

```sh
docker compose down
sudo sh scripts/uninstall-collector.sh
```

卸载保留历史卷、快照和采集器发布文件。若 usbmon 原先未加载，脚本会尝试正常卸载；正在使用时保留模块，不强制卸载。不要为卸载面板停用原 UPS 驱动。

## 常见问题

**NUT 正常，面板没有数据？** 原驱动可能没有读取完整 `0x71` 报告，或内核没有 usbmon。先看 `journalctl -u ugreen-ups-collector.service -n 50 --no-pager`；不要通过停止 NUT 或解绑 USB 强行获取面板数据。

**为什么功率留空，或与插座不一样？** 默认不启用单机校准模型。插座测量交流输入，电池侧功率和 NAS 直流输出是不同测点，包含的转换损耗也不同。[校准说明](docs/calibration.md)解释了可选模型、假设和适用范围。

**USB 完成状态 `-2` 是 UPS 故障吗？** 这是传输元数据。已验证驱动会在这类完成事件中带回完整报告；采集器仅在状态、端点、完整长度与字段校验同时通过时接受数据。

**NAS 更新后不再显示？** 核对当前内核是否仍提供匹配的 usbmon，以及原驱动是否继续产生完整报告。其他 NAS 服务正常不代表采集依赖仍然满足。

**没有温度、剩余续航或负载率？** 硬件具备某种传感能力，不等于该数据已经通过私有 USB 报告可靠导出；项目不会为这些指标填入推测值。

## 文档与来源

- [架构与数据流](docs/architecture.md) · [协议字段](docs/fields.md) · [功率校准](docs/calibration.md)
- [硬件资料](docs/hardware.md) · [验证范围](docs/validation.md) · [更新记录](CHANGELOG.md)
- [贡献指南](CONTRIBUTING.md) · [安全说明](SECURITY.md)

协议调查起点来自 [cktk/ugreen-ups](https://github.com/cktk/ugreen-ups)；本项目结合设备记录独立实现 Linux 采集、字段判断和面板，未将上游功率映射作为已验证测量。底层接口依据 [Linux usbmon 文档](https://docs.kernel.org/usb/usbmon.html)。US3000 的标准 HID 支持可参考 [NUT 驱动源码](https://github.com/networkupstools/nut/blob/master/drivers/arduino-hid.c)；标准 HID 与私有报告不是同一套字段。

本项目原创源码使用 [MIT 许可证](LICENSE)。前端运行依赖的许可证与版权声明见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)，它们保留各自许可，不因项目采用 MIT 而改变。产品名称、商标、第三方资料与素材仍归各自权利人所有。
