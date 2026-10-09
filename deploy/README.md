# 三五反 v1.2.1 服务器部署

这个包已实现好友房存档、部署服务入口和 HTTPS 配置。当前阶段只准备部署包，尚未创建或使用公网服务器。

## 之后需要准备什么

- 一台能运行 Linux Docker Engine 与 Compose 插件的服务器，架构为 amd64 或 arm64。
- 一个域名，将 DNS A 记录指向服务器公网 IPv4。只有 IPv4 时不要配置指向其他机器的 AAAA 记录。
- 服务器入站开放 TCP 80、443；UDP 443 可选，用于 HTTP/3。应用的 8080 端口无需开放。
- 一个用于证书通知的邮箱。

应用由 Waitress 提供 HTTP 服务，Caddy 负责公网 HTTPS。域名、证书和数据均与应用容器分开。游戏状态由单个应用进程持有，请保持一个 app 实例；不要添加多 worker 或多个副本。SQLite 文件有进程锁，重复启动会拒绝打开同一存档。

## Docker 部署

在解压后的项目根目录操作：

```bash
cp .env.example .env
```

编辑 `.env`，填入实际值，DOMAIN 只写域名，不写 `https://`、端口或路径：

```dotenv
DOMAIN=play.your-domain.com
ACME_EMAIL=your-email@example.com
```

确认 DNS 已生效后运行：

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 app caddy
```

打开 `https://你的域名/friends`。一人建房，其余朋友通过同一网址和房间号加入。Caddy 会申请并续期证书；应用使用 Secure、HttpOnly 会话 cookie。公网 POST 必须来自配置的 HTTPS 源地址，不从客户端传入的代理头推断身份或公网地址。

`data-init` 只为数据卷根目录设置应用 UID 10001 的所有权，运行后正常退出。app 使用普通用户和只读容器文件系统，存档在可写数据卷中。Caddy 等 app 健康检查通过再启动；只有 Caddy 对外发布端口。请求体上限为 8KB。

数据卷 `game_data` 保存房间与牌局，`caddy_data` 和 `caddy_config` 保存证书及代理配置。重建 app 镜像不会清空这些卷。更新可运行：

```bash
docker compose up -d --build
```

普通停机使用 `docker compose stop`，普通移除容器使用 `docker compose down`。**不要使用 `down -v`，它会删除存档和证书卷。** 更新部署包时保持同一 Compose 项目名 `sanwufan`，并保留数据卷。更换代码版本前先做备份；当前只支持存档 schema 1，不能保证旧代码可读取以后版本的存档。

## 恢复行为

每次成功的玩家操作和自动发牌推进都先提交 SQLite 事务，再返回成功。写入失败时回滚内存中的操作并返回可重试错误。恢复内容包括昵称、座位、房主、房间号、牌桌标识与版本、完整发牌顺序、各人的手牌、底牌、贡牌交换、当前轮、得分、结算和下局义务。其他玩家仍只能看到自己的手牌。

服务启动后，玩家用**原浏览器、原域名**回来即可恢复身份；清除 cookie、换浏览器或换域名不能自动认领旧座位。发牌中断后等四位玩家重新连接再继续，并重新给足最后的亮2窗口。其他阶段恢复到原来等待操作的位置。整桌最后活动超过两小时会过期，停机时间也计算在内。单人练习桌继续仅保存在内存，本阶段持久化覆盖四人好友房。

## 在线备份与恢复

备份使用 SQLite 在线备份接口，包含已经提交到 WAL 的内容，不直接复制运行中的 `.sqlite3` 文件。以下命令生成带时间戳的备份文件，已有目标文件不会被覆盖：

```bash
docker compose exec app python -m sanwufan.backup /data/sanwufan.sqlite3 /data/backup-20261006-120000.sqlite3
mkdir -p backups
docker compose cp app:/data/backup-20261006-120000.sqlite3 ./backups/
```

请用每次实际时间替换文件名，并把拷出的备份保存到服务器之外。备份包含会话令牌和所有手牌，按私有文件保管，不放到网页目录或分享给玩家。成功后可以定期清理数据卷中的旧备份，先确认外部副本可用。

恢复之前先停 app，保留现有存档及其 WAL 文件作为回退副本。可以保留停止的容器后拷入已校验备份：

```bash
docker compose exec app python -m sanwufan.backup /data/sanwufan.sqlite3 /data/before-restore-20261006-120000.sqlite3
docker compose cp app:/data/before-restore-20261006-120000.sqlite3 ./backups/
docker compose stop app
docker compose cp ./backups/backup-20261006-120000.sqlite3 app:/data/restore.sqlite3
docker compose run --rm --no-deps --user 0 --cap-add CHOWN --entrypoint python app -c "import os; os.chown('/data/restore.sqlite3',10001,10001)"
docker compose run --rm --no-deps --entrypoint python app -c "import os; from pathlib import Path; p=Path('/data'); os.replace(p/'restore.sqlite3',p/'sanwufan.sqlite3'); [(p/n).unlink(missing_ok=True) for n in ('sanwufan.sqlite3-wal','sanwufan.sqlite3-shm')]"
docker compose start app
```

恢复操作会把牌局回退到备份时刻。执行前应确认所有玩家已停止操作。上面的 chown 命令为拷入文件设置正确所有者；before-restore 文件名也需换成当前时间。重启后查看日志，确认服务读到原房间，不要在存档损坏时直接删文件开新桌。

## 不用 Docker 的入口

仅验证部署服务或已有自己的 HTTPS 代理时：

```bash
python -m pip install -r requirements-server.txt
python -m sanwufan.serve --local-test --port 8080 --database data/sanwufan.sqlite3
```

上面的验证地址为 `http://127.0.0.1:8080/friends`。正式使用时去掉 `--local-test`，设置 `--public-origin https://你的域名`，并由 HTTPS 代理转发到仅本机监听的 8080。使用已有反向代理时，保留原始 Host、Origin 和 Cookie，代理后面的 app 不直接公开。默认本地启动仍可用 `python -m sanwufan.web` 或 `启动牌桌.cmd`，无需安装 Waitress。

本地在线备份：

```powershell
python -m sanwufan.backup data/sanwufan.sqlite3 backups/game-backup.sqlite3
```

日志记录服务启动、操作路由与返回状态及内部错误，不记录 cookie、昵称、查询参数或手牌。查看 Docker 日志即可，不需要额外日志文件。镜像依赖固定为 Python 3.12.15、Waitress 3.0.2 与 Caddy 2.11.4；后续升级依赖时应重新验证恢复与联机流程。

## 本阶段验证范围

已执行规则回归、SQLite 恢复和故障回滚测试、WSGI 的 HTTPS 源地址与 cookie 校验，以及本机 Waitress 多客户端和进程重启验证。部署包不包含任何现有会话、存档、日志或测试数据。

当前电脑未安装 Docker，尚未执行容器构建、实际域名证书申请和异地访问测试。选择服务器后先运行上面的 `config`、构建和健康检查步骤，再验证四人真实设备联机。

参考：[Waitress 参数](https://docs.pylonsproject.org/projects/waitress/en/latest/arguments.html)、[Caddy HTTPS](https://caddyserver.com/docs/quick-starts/https)、[Compose 启动顺序](https://docs.docker.com/compose/how-tos/startup-order)、[Compose 环境变量](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation)。
