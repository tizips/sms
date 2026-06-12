# 短信收发平台

这个仓库维护一套 PVE 托管的短信收发平台。PVE 主机直接管理 Quectel
EM120R-GL 模块和 Gammu，负责真实短信收发；Admin 作为业务控制台运行在
Docker 中，负责登录、队列、发送计划、通知和状态展示。

PVE 持有 AT 设备，Admin 通过数据库队列和 PVE Radio Agent 发起发送任务。

## 目录

- `admin/`: Admin 控制台、队列、页面和业务逻辑。
- `bin/`: PVE agent、接收 hook、迁移和安全检查脚本。
- `docker/`: Admin 容器入口脚本。
- `migrations/`: Postgres 队列表结构。
- `systemd/`: PVE 自定义服务模板。
- `admin/admin.env.example`: Admin 运行时环境示例。

不要把密码、`.env`、数据库连接串、线上数据行或运行时密钥提交到仓库。

## PVE / Gammu

PVE 是短信硬件层。它拥有 `/dev/wwan0at0`，运行 `gammu-smsd`，并把短信收发
落到 `/var/lib/pve-sms` 的 file backend 队列中。

核心路径：

- `/etc/gammu-smsdrc`
- `/var/lib/pve-sms/spool/inbox/`
- `/var/lib/pve-sms/spool/outbox/`
- `/var/lib/pve-sms/spool/sent/`
- `/var/lib/pve-sms/spool/error/`
- `/usr/local/sbin/pve-radio-agent`
- `/usr/local/sbin/pve-sim-watchdog`
- `/usr/local/sbin/sms-received-hook`
- `/usr/local/sbin/quectel-radio-unlock`

### 安装 Gammu

在 PVE 上安装系统包：

```sh
apt update
apt install -y gammu gammu-smsd
```

`gammu-smsd.service` 使用系统包提供的 unit，路径通常是：

```text
/usr/lib/systemd/system/gammu-smsd.service
```

本仓库不维护这个系统包 unit 的副本。PVE 上需要的定制项放在 systemd drop-in
里，例如：

```text
/etc/systemd/system/gammu-smsd.service.d/pve-sms.conf
```

drop-in 只放运行环境变量，数据库、Redis、密码等必须保留在 PVE 运行环境中，
不要复制进仓库。

### 配置 `/etc/gammu-smsdrc`

PVE 的 Gammu 配置使用 AT 口和 file backend。关键配置如下，线上文件可以在此
基础上调整：

```ini
[gammu]
Device = /dev/wwan0at0
Connection = at115200
SynchronizeTime = no

[smsd]
Service = files
LogFile = syslog
DebugLevel = 0
PhoneID = pve-quectel
RunOnReceive = /usr/local/sbin/sms-received-hook
InboxPath = /var/lib/pve-sms/spool/inbox/
OutboxPath = /var/lib/pve-sms/spool/outbox/
SentSMSPath = /var/lib/pve-sms/spool/sent/
ErrorSMSPath = /var/lib/pve-sms/spool/error/
InboxFormat = detail
OutboxFormat = detail
TransmitFormat = auto
CommTimeout = 1
ReceiveFrequency = 1
LoopSleep = 1
MultipartTimeout = 20
```

创建队列目录：

```sh
install -d -m 0755 /var/lib/pve-sms/spool/inbox
install -d -m 0755 /var/lib/pve-sms/spool/outbox
install -d -m 0755 /var/lib/pve-sms/spool/sent
install -d -m 0755 /var/lib/pve-sms/spool/error
```

### 安装 PVE 脚本和服务

把脚本安装到 PVE：

```sh
install -m 0755 bin/pve-radio-agent /usr/local/sbin/pve-radio-agent
install -m 0755 bin/pve-sim-watchdog /usr/local/sbin/pve-sim-watchdog
install -m 0755 bin/sms-received-hook /usr/local/sbin/sms-received-hook
```

把 PVE 自定义 systemd 文件安装到 `/etc/systemd/system/`：

```sh
install -m 0644 systemd/pve-radio-agent.service /etc/systemd/system/pve-radio-agent.service
install -m 0644 systemd/pve-sim-watchdog.service /etc/systemd/system/pve-sim-watchdog.service
install -m 0644 systemd/pve-sim-watchdog.timer /etc/systemd/system/pve-sim-watchdog.timer
install -m 0644 systemd/quectel-radio-unlock.service /etc/systemd/system/quectel-radio-unlock.service
```

`quectel-radio-unlock.service` 依赖 PVE 本机已有的
`/usr/local/sbin/quectel-radio-unlock`。如果当前主机不需要或尚未准备这个脚本，
先不要启用该服务。

`pve-radio-agent.service` 和 `pve-sim-watchdog.service` 会读取：

```text
/etc/pve-radio-agent.env
```

这个文件通常包含数据库、Redis、设备路径和发送超时等运行时配置。不要把它提交
到仓库。

启用服务：

```sh
systemctl daemon-reload
systemctl enable --now gammu-smsd.service
systemctl enable --now pve-radio-agent.service
systemctl enable --now pve-sim-watchdog.timer
systemctl enable --now quectel-radio-unlock.service
```

### PVE 管理和检查

常用状态检查：

```sh
systemctl is-active gammu-smsd.service
systemctl is-active pve-radio-agent.service
systemctl list-timers pve-sim-watchdog.timer
gammu-smsd-monitor -c /etc/gammu-smsdrc -n 1
journalctl -u gammu-smsd.service -n 120 --no-pager
journalctl -u pve-radio-agent.service -n 120 --no-pager
```

PVE 必须保持短信专用：

- `ModemManager.service` 应保持禁用。
- `wwan0` 应保持 `DOWN`。
- 不配置 APN、DHCP、MBIM connect 或数据网络。
- 不在 `gammu-smsd` 或 `pve-radio-agent` 使用 AT 口时重置或 rebind PCI 设备。

恢复短信模块时，先检查硬件和无线状态：

```sh
lspci -nnk | grep -A4 -i -E 'quectel|1eac|mhi'
ls -l /dev/wwan*
ip link show wwan0
mbimcli -p -d /dev/wwan0mbim0 --query-radio-state
mbimcli -p -d /dev/wwan0mbim0 --query-registration-state
mbimcli -p -d /dev/wwan0mbim0 --query-signal-state
```

如果 MBIM 显示软件无线关闭，先执行 FCC unlock，再通过 AT 口确认：

```sh
/usr/share/ModemManager/fcc-unlock.available.d/1eac:1001 /dummy/path wwan0mbim0
```

```text
AT+CFUN?
AT+CPIN?
AT+CEREG?
AT+CSQ
```

如果 `AT+CFUN?` 返回 `+CFUN: 4`，打开无线：

```text
AT+CFUN=1
```

健康状态应满足：

```text
Software radio state: on
Register state: home
Signal is not 99
wwan0 remains DOWN
```

## Admin

Admin 是短信平台的业务层。它提供 Web 控制台，维护入站短信、出站短信、发送
计划、策略、邮件通知和状态页。部署模式下，Admin 不直接占用本地 modem，而是
写入 dispatch job，由 PVE Radio Agent 领取并调用 Gammu 发送。

### 运行模式

部署到 PVE 架构时使用：

```text
SMS_DISPATCH_MODE=pve
```

发送流程：

1. 操作员在 Admin 创建普通短信或计划短信。
2. Admin worker 到期后把出站记录转成 `sms_dispatch_jobs`。
3. PVE Radio Agent 从 Postgres 领取 job。
4. PVE Radio Agent 最终调用 `gammu-smsd-inject -c /etc/gammu-smsdrc`。
5. `gammu-smsd` 把 outbox 文件移动到 sent 或 error。
6. PVE Radio Agent 回写发送结果，Admin worker 同步状态并发送通知。

接收流程：

1. PVE 上的 `gammu-smsd` 收到短信。
2. `RunOnReceive` 调用 `/usr/local/sbin/sms-received-hook`。
3. hook 写入数据库，并可通过 Redis 通知 Admin。
4. Admin 页面展示入站记录，并按配置执行邮件转发。

### Admin 配置

Admin 镜像默认使用：

```text
SMS_BASE=/htdocs/sms
PORT=8088
```

入口脚本会创建：

```text
/htdocs/sms/conf/admin.env
/htdocs/sms/data/
/htdocs/sms/logs/
```

PVE 部署不依赖也不应挂载 `/htdocs/sms/spool/`。真实 Gammu file backend 队列
在 PVE 的 `/var/lib/pve-sms/spool/`；`SMS_BASE/spool/` 只属于旧本地
`gammu-smsd` fallback 路径。

如果 `conf/admin.env` 不存在，容器启动时需要通过环境变量提供
`ADMIN_PASSWORD` 或 `ADMIN_PASSWORD_HASH`。示例文件在：

```text
admin/admin.env.example
```

常用环境变量：

- `SMS_DISPATCH_MODE=pve`: 使用 PVE dispatch job。
- `PVE_RADIO_AGENT_URL`: Admin 状态页读取 PVE SIM 状态的 HTTP 地址。
- `SMS_DATABASE_URL` 或 `DATABASE_URL`: Postgres 连接串。
- `SMS_REDIS_URL`: 入站通知使用的 Redis 地址。
- `ADMIN_PASSWORD` 或 `ADMIN_PASSWORD_HASH`: 首次生成 Admin 登录配置。
- `SESSION_SECRET`: Admin 登录会话密钥。

这些值属于运行时配置，尤其是数据库和密码，不要写入仓库。

### 数据库迁移

`migrations/` 只保存 Postgres schema migration，文件名按数字前缀排序执行。
迁移状态记录在数据库的 `schema_migrations` 表中，已执行过的文件会被跳过。

当前脚本：

- `0001_initial_schema.sql`: 创建短信平台初始表结构，包括入站短信、出站短信、
  发送策略、发送计划、应用设置、应用状态和 PVE dispatch job。
- `0002_send_plan_phone_parts.sql`: 为发送计划补充 `country_code` 和
  `phone_number` 字段，用于把区号和本地号码分开保存。

迁移可以由 Admin 启动时自动执行，也可以在容器或同等运行环境中手动执行：

```sh
SMS_DATABASE_URL='<postgres-url>' /htdocs/sms/bin/db-migrate
```

手动执行前先确认目标数据库和备份；不要把真实连接串写入 README、compose 或
提交历史。

### 构建和部署 Admin

Admin 以预构建 Docker 镜像部署，不在运行服务器上构建镜像。

本地构建：

```sh
rtk docker build --platform linux/amd64 -t sms/admin:latest .
```

部署前至少做语法检查和相关行为验证：

```sh
python3 -m py_compile admin/*.py bin/db-migrate bin/sms-received-hook
python3 tests/test_sms_admin_features.py
```

导出镜像并上传到运行服务器，再在服务器上 `docker load`：

```sh
docker save sms/admin:latest | gzip > sms-admin-latest.tar.gz
scp sms-admin-latest.tar.gz <admin-server>:/htdocs/docker/sms/
ssh <admin-server> 'cd /htdocs/docker/sms && sudo docker load < sms-admin-latest.tar.gz'
```

服务器 compose 文件因主机不同可以不同，但运行镜像 tag 应保持：

```text
sms/admin:latest
```

是否挂载目录按需求决定：

- 挂载 `conf`：需要跨容器替换保留 `admin.env`、SMTP 配置或 UI 修改后的密码。
- 挂载 `logs`：需要保留文件日志或直接在宿主机检查日志。

更新 compose 前先备份，重启后检查：

```sh
sudo docker compose up -d
sudo docker compose ps
curl -f http://127.0.0.1:8088/
```

### Admin 功能

- 密码登录和修改密码。
- Dashboard 展示入站、出站、SIM、信号和服务状态。
- 普通短信发送。
- 计划短信和发送策略。
- 发送队列、重试、取消和状态追踪。
- 入站短信列表和转发状态。
- SMTP 通知配置，支持入站转发、计划发送成功、最终失败和状态不明通知。
- PVE 模式下，Dashboard 使用 PVE Radio Agent 展示 SIM 状态。

发送策略偏保守：只有 `gammu-smsd-inject` 明确返回非零失败时才重试；超时或状态
不明会标记为 `ambiguous`，避免恢复后重复发送。

## 日常排障顺序

1. 在 PVE 上确认 modem、AT 口、`wwan0`、`gammu-smsd` 和
   `pve-radio-agent` 状态。
2. 用 `gammu-smsd-monitor -c /etc/gammu-smsdrc -n 1` 确认 Gammu 能读到 SIM。
3. 查 `journalctl -u gammu-smsd.service` 是否有 `NOSIM`、`DEVICEOPENERROR` 或
   `UNKNOWN[27]`。
4. 查 `journalctl -u pve-radio-agent.service` 是否能连接数据库和领取 dispatch
   job。
5. 在 Admin 中查看出站记录、dispatch job 结果和最后错误。
6. 只有确认 PVE 硬件层健康后，再处理 Admin 队列或通知配置。
