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
- `VOICE_ALLOWED_ORIGINS` 改成正式地址，如 `https://voice.example.com`。
- `VOICE_ASR_CONCURRENCY` 约为 CPU 核数的一半。

证书：把云厂商签发的证书放成 `deploy/certs/fullchain.pem` 和 `deploy/certs/privkey.pem`。再把 `deploy/nginx.conf` 里的 `example.com` 改成你的域名。

## 4. 启动

```bash
docker compose up -d --build
docker compose ps                      # app 显示 healthy 才算起来了
docker compose logs -f app             # 看日志，Ctrl+C 退出查看
```

创建管理员账号（密码由你自己在终端里输入）：

```bash
docker compose exec app python manage.py create-admin 你的邮箱
```

浏览器打开 `https://你的域名`，用管理员登录，在“设置”页可以看到服务状态和运行情况。

## 5. 备份

每晚 3 点备份一次，保留 14 份（`crontab -e` 加一行）：

```
0 3 * * * cd /home/你/voice && docker compose exec -T app python backup.py --keep 14 >> backups/backup.log 2>&1
```

备份是 `backups/data-日期.tar.gz`，包含所有账号、记忆、提醒、知识库、录音和设置。建议再定期拷到对象存储或另一台机器。

恢复：先停服务（`docker compose down`），把 `data` 目录换成解压出来的内容，再启动。

## 6. 更新和回退

```bash
git pull && docker compose up -d --build     # 更新
git checkout <上一个版本> && docker compose up -d --build   # 出问题时回退
```

数据在 `data` 目录里，更新和回退都不会动它。

## 7. 上线前还要做的（见 SaaS改造方案.md 的 S0、S2）

- ICP 备案、生成式 AI 服务登记，按网信办的要求公示模型名称和备案号。
- 默认语音换成国内商用语音合成服务（edge-tts 只用于试用）。
- 隐私政策、用户协议、投诉渠道；按要求接入实名认证。
- 压测：`python work/load_test.py --url https://你的域名 --pcm 一句话.pcm --users 1 5`。它会创建测试账号，只在试用期或测试环境跑。
