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
- 查看历史曲线、供电与连接事件，导出 CSV。
- 查看硬件资料、NUT 状态与采集诊断。

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

采集器只需首次安装；日常更新面板不用重新安装。

## 更新

SSH 登录后，在项目目录执行（路径与安装时一致）：

```sh
cd /volume1/docker/ugreen-ups-panel
sudo docker compose pull && sudo docker compose up -d
```

默认使用 `bsakuramiku/ugreen-ups-panel:latest`。版本变更见 [Release](https://github.com/BSakura-Miku/ugreen-ups-panel/releases)；只有说明要求更新采集器时，才更新对应脚本并重新安装。

## 配置与数据

- 默认访问端口为 `9086`。需要更改端口或镜像版本时，直接编辑 `docker-compose.yaml`，再运行 `docker compose up -d`。
- Compose 自动读取 `docker-compose.yaml`，无需顶层 `name`；项目名默认来自部署目录名称，按上述步骤安装时为 `ugreen-ups-panel`。
- 历史数据库保存在项目目录的 `./data/history.sqlite`，重建容器会保留历史。请保留并备份 `data` 目录。
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
