# 部署到云服务器（小范围试用）

一台国内云服务器，Docker 运行应用，nginx 负责 HTTPS。下面按顺序做。

## 1. 服务器

- 系统：Ubuntu 22.04 或 24.04。
- 配置：4 核 8 GB 起步。识别在 CPU 上跑，每路识别占 2 个线程；克隆音色这一版不开。
- 安装 Docker 和 Compose 插件（按云厂商文档，或者 `curl -fsSL https://get.docker.com | sh`）。
- 安全组只开 80、443 和 SSH 端口。
- 域名解析到这台服务器，ICP 备案通过后才能用 80、443 对外提供服务。

## 2. 放代码和模型

```bash
git clone <仓库地址> voice && cd voice
mkdir -p data models backups deploy/certs
sudo chown 10001 data backups        # 容器里用 uid 10001 运行，要能写这两个目录
```

识别模型：国内服务器通常访问不了 Hugging Face，从开发机把整个 `models/sensevoice-small` 目录拷过去：

```bash
scp -r models/sensevoice-small 用户@服务器:voice/models/
```

## 3. 配置

```bash
cp .env.example .env
nano .env
```

- 自己在服务器上填 `DEEPSEEK_API_KEY`，不要贴到聊天或代码里。
- 数据库密码：`POSTGRES_PASSWORD`（数据库管理员）和 `VOICE_DB_PASSWORD`（应用自己的账号）各填一个不同的长随机串，可以用 `openssl rand -hex 24` 生成。它们只在数据库第一次创建时读取，之后再改要进数据库里改。
- `VOICE_ALLOWED_ORIGINS` 改成正式地址，如 `https://voice.example.com`。
- `VOICE_ASR_CONCURRENCY` 约为 CPU 核数的一半。

证书：把云厂商签发的证书放成 `deploy/certs/fullchain.pem` 和 `deploy/certs/privkey.pem`。再把 `deploy/nginx.conf` 里的 `example.com` 改成你的域名。

## 4. 启动

```bash
docker compose up -d --build
docker compose ps                      # db 和 app 都显示 healthy 才算起来了
docker compose logs -f app             # 看日志，Ctrl+C 退出查看
```

创建管理员账号（密码由你自己在终端里输入）：

```bash
docker compose exec app python manage.py create-admin 你的邮箱
```

账号、花费、记忆、提醒、任务、英语练习和聊天记录都存在 PostgreSQL（`db` 服务，数据在 Docker 卷 `pgdata` 里）。每个用户的数据靠行级安全隔开：应用用的 `voice_app` 账号不是超级用户，启动时如果发现账号能绕过行级安全会直接拒绝启动。知识库索引、录音、音色和设置仍在 `data/users/<编号>/` 下。

### 从 SQLite 版迁过来

以前用 SQLite 跑过、`data/` 里已有 `accounts.db` 的，在第一次启动后、创建管理员之前导入一次（数据库必须是空的，导入两次会被拒绝）：

```bash
docker compose exec app python manage.py migrate-to-postgres
```

账号编号保持不变（用户文件夹按编号命名），记忆、聊天等条目重新编号，互相之间的关联会一起改好。SQLite 文件原样留着，确认无误后再自己移走。

浏览器打开 `https://你的域名`，用管理员登录，在“设置”页可以看到服务状态和运行情况。

## 5. 备份

每晚 3 点备份一次，保留 14 份（`crontab -e` 加一行）：

```
0 3 * * * cd /home/你/voice && docker compose exec -T db pg_dump -U postgres -Fc voice > backups/db-$(date +\%Y\%m\%d).dump 2>> backups/backup.log && find backups -name 'db-*.dump' -mtime +14 -delete
10 3 * * * cd /home/你/voice && docker compose exec -T app python backup.py --keep 14 >> backups/backup.log 2>&1
```

两份备份：`backups/db-日期.dump` 是数据库（账号、记忆、提醒、聊天、英语、花费），`backups/data-日期.tar.gz` 是文件（知识库、录音、音色、设置）。建议再定期拷到对象存储或另一台机器。

恢复数据库：

```bash
docker compose stop app
docker compose exec -T db pg_restore -U postgres -d voice --clean --if-exists --no-owner --role=voice_app < backups/db-日期.dump
docker compose start app
```

恢复文件：先停服务（`docker compose down`），把 `data` 目录换成解压出来的内容，再启动。

## 6. 更新和回退

```bash
git pull && docker compose up -d --build     # 更新
git checkout <上一个版本> && docker compose up -d --build   # 出问题时回退
```

数据在 `data` 目录和 `pgdata` 卷里，更新和回退都不会动它。表结构由程序启动时自动创建和升级；回退到更旧的程序时，如果数据库结构版本比程序新，程序会拒绝启动并提示，这时先恢复对应日期的数据库备份。千万不要用 `docker compose down -v`，`-v` 会删掉数据库卷。

## 7. 上线前还要做的（见 SaaS改造方案.md 的 S0、S2）

- ICP 备案、生成式 AI 服务登记，按网信办的要求公示模型名称和备案号。
- 默认语音换成国内商用语音合成服务（edge-tts 只用于试用）。
- 隐私政策、用户协议、投诉渠道；按要求接入实名认证。
- 压测：`python work/load_test.py --url https://你的域名 --pcm 一句话.pcm --users 1 5`。它会创建测试账号，只在试用期或测试环境跑。
