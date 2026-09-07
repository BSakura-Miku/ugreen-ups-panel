# 通过面板更新采集器

v0.9.0 在「诊断与说明」加入采集器更新。首次启用需在 NAS 安装独立的宿主更新服务，并给面板挂载它的本地通信目录。之后可在页面检查正式发行、查看说明、更新采集器和回退兼容版本。

它只处理本项目采集器代码。面板 Docker 镜像、宿主更新服务自身、NUT、UPS 固件和 NAS 系统仍分别维护。没有定时检查或自动安装；打开页面、刷新状态均不访问发行服务器。

## 首次启用

先按 [README](../README.md)安装并确认已有采集器正常工作。v0.8.0 采集器可直接作为网页更新的起点。备份原数据库与校准配置，见[备份说明](architecture.md#备份与维护)。

在 **NAS 宿主机**执行以下命令。将路径改成原部署目录；自定义过 Compose 项目名时，为所有 Compose 命令加上同一个 `-p 原项目名`。保留已有 `docker-compose.yaml` 和 `data/`。

```sh
(
  set -eu
  ups_project_dir=/volume1/docker/ugreen-ups-panel
  ups_update_tmp="$(mktemp -d)"
  trap 'rm -rf "$ups_update_tmp"' EXIT
  curl -fL https://codeload.github.com/BSakura-Miku/ugreen-ups-panel/tar.gz/refs/tags/v0.9.0 -o "$ups_update_tmp/source.tar.gz"
  tar -xzf "$ups_update_tmp/source.tar.gz" --strip-components=1 -C "$ups_update_tmp"
  sudo sh "$ups_update_tmp/scripts/install-updater.sh"
  # 首次创建可选覆盖文件；如已存在，先检查其内容，不覆盖个人设置。
  if [ ! -e "$ups_project_dir/compose.updater.yaml" ]; then
    sudo cp "$ups_update_tmp/compose.updater.yaml" "$ups_project_dir/compose.updater.yaml"
  fi
  sudo docker compose -f "$ups_project_dir/docker-compose.yaml" -f "$ups_project_dir/compose.updater.yaml" pull
  sudo docker compose -f "$ups_project_dir/docker-compose.yaml" -f "$ups_project_dir/compose.updater.yaml" up -d
)
```

覆盖文件只添加 `/run/ugreen-ups-updater` 的只读目录挂载；目录必须由已安装服务准备，Compose 不自动创建它。默认容器用户 root 和 `10001:10001` 均可连接。使用其他容器 UID 时，需要给容器补充 GID `10001`。后续重建面板时也保留这个覆盖文件；通过 NAS Docker 界面管理时，将其中挂载合入原项目配置即可。

首次启用还会移除现有项目源码的组写与其他用户写权限，保留代码内容与所有者；已有服务、环境文件和数据目录不因此改动。安装失败会恢复此前的源码权限。

首次安装自动生成管理密钥，重装保留原密钥。在 NAS 终端读取：

```sh
sudo cat /etc/ugreen-ups-updater/key
```

将密钥填入页面的「管理密钥」。它只留在当前页面内存中；离开该页或刷新后需重新输入，也可主动清除。查看版本和进度不需要密钥，检查新版、安装及回退需要密钥。面板不会读取宿主密钥文件，也不会将密钥写入浏览器存储、操作记录或诊断导出。

密钥赋予采集器更新权限，应作为密码保管。面板普通遥测和校准仍没有登录机制，继续只在可信网络使用；需要跨网络管理时，通过自己的 HTTPS 入口访问，避免明文传送密钥。不要将密钥放进 URL、截图、公开问题或访问日志。

## 日常操作

1. 打开「诊断与说明」，确认更新服务可用并输入管理密钥。
2. 点击「检查更新」。只查询固定项目仓库的最新正式发行，显示版本与说明。
3. 核对目标后点击安装。页面依次显示下载、校验、安装及采集验证阶段；不显示估算百分比。
4. 等待完成。更新会短暂重启本项目采集器，历史中可能出现短连接间隔；原历史与校准配置保留，NUT 保护服务保持运行。

页面关闭或刷新不会取消已接受的操作。重新打开可恢复进度；更新服务正常停止时会等待正在执行的操作结束。一次只允许一个操作，手动命令与网页更新共用安装锁；提交时检查版本和源码指纹，避免下载期间发生手动升级后覆盖新版本。

检查结果在当前更新服务进程中最多用于安装一小时。服务重启或信息变化后需重新检查。下载时再次核对发行附件身份及 SHA-256；网络或校验失败不会切换采集器代码。

## 回退与异常

更新包安装到独立版本目录。安装器保留先前代码与服务状态，切换后等待新的 USB 样本及心跳；验证失败会尝试恢复先前版本。页面区分「已恢复原版本」和「需要检查服务」，不会把失败恢复也显示成成功。

网页回退只用于通过保留配置模式产生的兼容备份，最早支持 v0.8.0。旧安装器产生的备份可能同时恢复宿主配置，因此不提供网页回退按钮；需要时按原版本的命令行回滚说明处理。回退不删除其他版本目录，也不回退历史数据库或面板镜像。

NAS 断电或强制终止进程可能中断事务。服务重新启动会显示上次操作被中断，并重新读取当前版本；不会根据旧记录自动再装一次。若采集未恢复，在 NAS 检查：

```sh
sudo systemctl status ugreen-ups-updater.service ugreen-ups-collector.service --no-pager
sudo journalctl -u ugreen-ups-updater.service -u ugreen-ups-collector.service -n 60 --no-pager
```

未安装、通信目录未挂载、目录权限不匹配和服务停止都会使更新卡片不可用，不影响已有遥测与历史接口。发行服务器访问失败时检查 NAS 网络；更新服务使用宿主进程的无凭据 HTTP(S) 代理环境，不沿用浏览器代理。

## 权限与发行包

宿主服务只监听 Unix socket，没有 TCP 监听端口。通信目录权限为 `0750 root:10001`，socket 为 `0660 root:10001`；管理密钥单独保存在宿主 `0600` 文件中。所有写操作还需密钥校验，HTTP 接口检查同源、自定义请求头、请求大小和固定字段。更新服务不接受任意命令、路径、仓库或下载 URL。

下载来源固定为 `BSakura-Miku/ugreen-ups-panel` 的正式 GitHub Release。版本包名为 `collector-v版本.tar.gz`，使用 GitHub API 提供的 SHA-256，缺失时读取同发行的 `SHA256SUMS`。HTTPS 和固定仓库定义来源信任；SHA-256用于完整性检查，不等同于独立维护者签名。

包最多 4 MiB，仅包含 `ups_panel/*.py`、固定版本元数据和 `LICENSE`。拒绝路径穿越、绝对路径、重复成员、链接、特殊文件、扩展归档头、大小超限及不兼容版本。安装前重算源码指纹并核对版本，解析时不导入下载代码。执行的是首次安装在宿主的固定安装器；下载包没有可执行安装脚本或服务配置。正式发行中的采集器 Python 代码仍是受信任代码，安装后由原采集器服务运行。

常规更新保留现有 systemd 配置、采集器环境文件、数据目录权限、校准和历史，仅切换采集器代码与重启本项目服务。将来若升级需要修改这些配置或更新宿主更新服务，会单独说明命令行升级步骤。

## 停用

在对应版本的源码目录执行：

```sh
sudo sh scripts/uninstall-updater.sh
```

这会停止并禁用更新服务，移除其 unit 与 socket；采集器继续工作。更新服务代码、管理密钥和操作记录保留，便于重装。删除 Compose 中的可选更新挂载并重建面板即可停用面板中的采集器更新操作；卡片会显示未安装或不可用。不要删除原采集快照挂载或 `data/`。
