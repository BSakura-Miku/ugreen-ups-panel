# US3000 电力监控

**简体中文** · [English](README.en.md)

<p align="center"><img src="frontend/src/assets/us3000-logo.png" width="80" height="80" alt="US3000 Monitor Logo" /></p>

![US3000 电力监控面板预览：演示数据](docs/assets/dashboard-demo.png)

*预览使用 DEMO 演示数据，不包含实际 NAS 数据或序列号。*

为 UGREEN US3000 提供供电状态、电量、电芯电压与历史趋势。宿主机采集器通过 Linux usbmon 只读观察已有 UPS 驱动的数据，Docker Compose 启动网页面板。

**NAS 原有 UPS 服务继续负责断电保护与关机。** 本项目是社区监控工具，与 UGREEN 无隶属或认证关系。

## 功能

- 查看外部供电、充电与电池供电状态。
- 查看电量、输入/输出电压、电池组与四节电芯电压、电芯压差。
- 查看最长一年的历史趋势、供电与连接事件，导出含均值和极值的 CSV。
- 对照电芯压差的均值与峰值，查看实际记录的日期跨度。
- 查看每次电池供电的起止时间、观测时长、起止电量，以及次数和电量净下降。
- 在网页切换功率校准配置、查看公式和系数，保存自定义系数。
- 查看硬件资料、NUT 状态与采集诊断。

趋势可选 1 小时、24 小时、7 天、30 天、90 天、半年（180 天）和一年（365 天）。超过 90 天使用按日统计；图表保留完整所选时间范围，尚未采集或中断的部分留空，已有几天数据就只显示几天。

电池供电明细从升级后的首次有效采样开始记录，旧事件和聚合历史不会被补成明细。默认查看 90 天，可选 7 天至一年；缺少起止或采集中断的记录会标为不完整。电量净下降以百分点计，允许出现电量回升，不是耗电瓦时或电池循环次数。

功率相关字段仍有协议解释和测点限制；可选经验模型默认关闭（校准配置 `none`），不套用开发样机系数，详见[功率校准](docs/calibration.md)。

## 安装前提

- UPS 连接到 Linux NAS，宿主机具备 systemd、Python 3.10+、curl、tar、Docker 和 Compose v2 或更新版本。
- 内核支持 usbmon，并可提供 `/dev/usbmonN`。
- 原有 NUT/系统 UPS 驱动已连接 US3000，并持续读取完整的私有 `0x71` 报告。

镜像支持 `linux/amd64` 和 `linux/arm64`；目前实机验证仅覆盖一台 x86_64 NAS 与 US3000，ARM 仅完成容器运行验证。

页面和 API 没有内置认证，请仅在可信局域网使用。

## UGOS Pro 三步安装

以下以 **绿联 DXP4800 Plus + 绿联 US3000 UPS + UGOS Pro** 的实测环境为例。先通过 USB 将 UPS 连接到 NAS，确认系统能够识别 UPS，并在应用中心安装 **Docker**。

### 1. 启用 SSH

打开【控制面板 → 终端机】，勾选 **SSH 启用**，端口默认 `22`，点击【应用】。

![UGOS Pro：在控制面板的终端机页面启用 SSH](docs/assets/ugos-pro-enable-ssh.png)

### 2. 连接终端，安装采集器

在电脑上打开终端，用 NAS 管理员账号连接；将 `用户名`、`NAS_IP` 和端口替换为自己的设置：

```sh
ssh 用户名@NAS_IP -p 22
```

登录后执行以下**首次安装**命令。这里假设 `docker` 共享文件夹位于 `/volume1/docker`，请按实际路径修改，并确保账号有该文件夹的写入权限：

```sh
mkdir /volume1/docker/ugreen-ups-panel &&
curl -fL https://codeload.github.com/BSakura-Miku/ugreen-ups-panel/tar.gz/refs/heads/main -o /tmp/ugreen-ups-panel.tar.gz &&
tar -xzf /tmp/ugreen-ups-panel.tar.gz --strip-components=1 -C /volume1/docker/ugreen-ups-panel &&
cd /volume1/docker/ugreen-ups-panel &&
sudo sh scripts/install-collector.sh
```

出现 `Fresh UPS telemetry verified.` 表示安装成功。脚本会自动安装并启用采集器、准备 `data` 目录，无需手动修改权限；原有 UPS 服务保持运行。

### 3. 创建 Docker 项目

打开【Docker → 项目 → 创建项目】：

- **项目名称**：`ugreen-ups-panel`
- **存放路径**：选择第二步的同一目录，即 `共享文件夹/docker/ugreen-ups-panel`。
- **Compose 配置**：粘贴下面内容，也可导入该目录中的 [`docker-compose.yaml`](docker-compose.yaml)。

```yaml
services:
  panel:
    image: bsakuramiku/ugreen-ups-panel:latest
    restart: unless-stopped # NAS 重启后自动启动，手动停止后保持停止
    ports:
      - "9086:8080" # 访问 NAS_IP:9086；更换访问端口只改左侧
    volumes:
      # 宿主机采集器输出，只读挂载给面板
      - /run/ugreen-ups-panel:/run/ugreen-ups-panel:ro
      # 历史数据库保存在当前目录，更新容器时保留
      - ./data:/data
```

勾选【创建完成后立即运行】，点击【立即部署】，等待镜像下载并启动。

![UGOS Pro：创建 Docker 项目，选择目录并填写 Compose 配置](docs/assets/ugos-pro-docker-project.png)

最后在浏览器打开 **`http://NAS_IP:9086`**，将 `NAS_IP` 替换为 NAS 的局域网地址。

采集器首次安装后会自动运行。已有 v0.4.0 采集器升级到 v0.5.0 时，只需更新面板容器，无需重装采集器。

## 更新

**v0.5.0 兼容现有 v0.4.0 采集器。** SSH 登录后，在项目目录执行（路径与安装时一致）：

```sh
cd /volume1/docker/ugreen-ups-panel
sudo docker compose pull && sudo docker compose up -d
```

默认使用 `bsakuramiku/ugreen-ups-panel:latest`。版本变更见 [Release](https://github.com/BSakura-Miku/ugreen-ups-panel/releases)。

**若仍使用 v0.3.x 或更早的采集器**，需先更新一次宿主机采集器，网页保存的校准配置才能生效。将下方 `/volume1/docker/ugreen-ups-panel` 改为现有项目路径后执行；源码会下载到临时目录，安装结束后清理，保留原 `data` 与 `docker-compose.yaml`：

```sh
(
  set -eu
  tmp_dir="$(mktemp -d)"
  trap 'rm -rf "$tmp_dir"' EXIT
  curl -fL https://codeload.github.com/BSakura-Miku/ugreen-ups-panel/tar.gz/refs/tags/v0.4.0 -o "$tmp_dir/source.tar.gz"
  tar -xzf "$tmp_dir/source.tar.gz" --strip-components=1 -C "$tmp_dir"
  sudo sh "$tmp_dir/scripts/install-collector.sh" --data-dir /volume1/docker/ugreen-ups-panel/data
  sudo docker compose -f /volume1/docker/ugreen-ups-panel/docker-compose.yaml pull
  sudo docker compose -f /volume1/docker/ugreen-ups-panel/docker-compose.yaml up -d
)
```

## 网页功率校准

打开网页中的**功率校准**，可查看当前生效配置、三个系数及计算公式，并选择 `none`（关闭估算）、`local-19v-v1`（开发样机配置）或 `custom`（自定义）。默认仍为 `none`。系数最多显示 4 位小数，计算与未修改字段的保存仍保留完整精度。

自定义时，`base_gain` 与 `battery_gain` 大于 `0`、不超过 `10`；`charge_gain` 为 `0`–`10`。这些范围只是输入限制，**自定义配置始终标为未独立验证**，不会因保存成功而获得精度保证。

页面分别显示“已保存”和“已生效”；需等采集器用新配置生成新鲜数据后才算生效。配置保存在已有的 `./data/calibration.json`，无需增加 Compose 参数。原始读数和已有历史保留，不同校准版本的历史分开统计。模型仍使用 18–20 V 交流输入限制与 8 秒平滑，详见[功率校准说明](docs/calibration.md)。

## 配置与数据

- 默认访问端口为 `9086`。需要更改端口或镜像版本时，直接编辑 `docker-compose.yaml`，再运行 `docker compose up -d`。
- Compose 自动读取 `docker-compose.yaml`，无需顶层 `name`；项目名默认来自部署目录名称，按上述步骤安装时为 `ugreen-ups-panel`。
- 历史数据库保存在 `./data/history.sqlite`，网页校准配置保存在 `./data/calibration.json`。重建容器会保留这些文件，请保留并备份整个 `data` 目录。
- 历史按 10 秒统计保留 7 天、按分钟统计保留 90 天、按日统计保留 365 天。升级会从数据库中仍保留的旧记录补建日统计，已经过期删除的记录无法恢复。
- 容器只读访问宿主机采集快照；采集器与原有 UPS 服务同时运行。

数据流、历史存储和备份细节见[架构文档](docs/architecture.md)。

## 没有数据时

先查看采集器和面板日志：

```sh
journalctl -u ugreen-ups-collector.service -n 50 --no-pager
docker compose logs --tail 50
```

NUT 能显示电量，不一定意味着驱动会读取面板所需的完整报告。安装条件与已知限制见[验证范围](docs/validation.md)。

## 更多文档

- [架构与数据流](docs/architecture.md) · [协议字段](docs/fields.md) · [功率校准](docs/calibration.md)
- [硬件资料](docs/hardware.md) · [验证范围](docs/validation.md) · [更新记录](CHANGELOG.md)
- [开发与贡献](CONTRIBUTING.md) · [安全说明](SECURITY.md)

## 参考与致谢

感谢 [cktk/ugreen-ups](https://github.com/cktk/ugreen-ups) 分享的 US3000 USB HID 协议研究与遥测字段说明，本项目的协议调查由此开始。后续字段解析结合了 DXP4800 Plus + US3000 的实际采集与验证。

宿主机采集接口依据 [Linux usbmon 文档](https://docs.kernel.org/usb/usbmon.html)。

## 许可证

原创源码使用 [MIT 许可证](LICENSE)。第三方依赖保留各自许可，见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)；产品名称、商标和第三方素材归各自权利人所有。
