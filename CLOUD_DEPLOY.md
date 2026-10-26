# 云服务器部署

本服务提供音频上传、排队处理、文稿整理、任务查询和结果下载。默认一个任务工作进程，语音识别使用 CPU；文稿生成与整理调用配置的 DeepSeek 接口。已有 Windows 本地运行环境不包含在云部署包中。

## 用 Docker Compose 部署

服务器需要 Linux、Docker Engine 和 Docker Compose。克隆仓库或上传源码包，在项目目录执行：

```sh
cp .env.cloud.example .env.cloud
# 编辑 .env.cloud，填写 LLM_API_KEY，并为 SERVICE_API_TOKEN 设置长随机字符串。
# 可在服务器用以下命令生成访问令牌：
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
docker compose --env-file .env.cloud up -d --build
docker compose --env-file .env.cloud ps
```

`SERVICE_API_TOKEN` 是访问本服务的令牌；`LLM_API_KEY` 是调用模型的密钥，两者应不同。程序在非回环地址监听时要求配置服务令牌。不要将真实密钥提交到仓库或放进构建包。

首次处理录音会下载识别模型，耗时取决于网络。模型和输出保存在独立 Docker 卷中，重建容器不会清空。上传大小上限默认 256 MiB，可通过 `MAX_UPLOAD_MB` 调整；它不是音频时长上限。长录音的内存、磁盘和执行时间需求应按实际任务评估。

默认只将容器端口映射到服务器的 `127.0.0.1:8765`。可先通过 SSH 转发访问：

```sh
ssh -L 8765:127.0.0.1:8765 user@server
```

在自己电脑打开 `http://127.0.0.1:8765`，输入服务令牌，上传音频即可。

## 开放给团队以外的人提交录音

`/submit` 是给提交者用的页面：上传录音，等待处理，然后预览和下载视频文案（`video_script.md`）与分镜表（`video_script.csv`）。提交者不需要服务令牌，也看不到原稿、转写、声音素材或其他人的任务。

云端默认关闭此入口。在 `.env.cloud` 设置 `SUBMIT_ACCESS_CODE=你发给提交者的提交码`（不能与 `SERVICE_API_TOKEN` 相同）后重启服务，再把 `https://域名/submit` 和提交码发给对方。每份提交会得到一个随机链接，凭链接即可查看结果，请提醒对方保存。服务同时最多处理 10 份来自此入口的录音，超出时提示稍后再试，团队自己的任务不受影响。提交的录音同样出现在团队界面的任务列表中。

## 使用域名

在服务器已有的 HTTPS 反向代理中将域名转发到 `http://127.0.0.1:8765`；必须保留原始 Host。把 `.env.cloud` 的 `SERVICE_PUBLIC_ORIGIN` 设置为实际的 `https://域名`，重新启动服务。不要填写 URL 路径。

以已经配置好证书的 Nginx HTTPS server 块为例：

```nginx
client_max_body_size 256m;
location / {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_request_buffering off;
    proxy_read_timeout 300s;
}
```

上传接口返回后，识别和整理在后台排队运行，通过任务状态接口查询；无需保持数十分钟的 HTTP 请求。反向代理的上传上限应与应用上限一致。

## 数据、任务与重启

- `/data/uploads`：上传的原始音频。
- `/data/outputs`：各次任务的原稿、转写、候选音频及整理稿。
- `/data/logs`：任务运行日志。
- `/data/jobs.json`、`/data/uploads.json`：任务和上传记录。
- 重启后保留历史任务与结果；被中断的任务标为失败，不宣称已完成，可重新提交。
- 整理模块有结果目录锁，且在提交新整理稿前再次核对原稿哈希。
- 备份时同时保留数据卷与配置；不要执行会删除卷的命令，除非确实要清空录音资料。

查看状态和日志：

```sh
docker compose --env-file .env.cloud logs --tail=100
curl http://127.0.0.1:8765/api/health
```

具体上传和调用示例见 [API.md](API.md)。这个版本面向单个可信团队共用令牌，不提供独立用户、租户隔离或按用户计费。

## 验证边界

本项目已进行 Windows 本机离线测试及部分真实录音验证；初版发布时尚未验证 Linux Docker 镜像构建或实际云服务器部署。部署时请先执行构建和健康检查，再用一个短音频验证模型下载及权限。

Compose 的配置文件插值与必填变量语法依据 [Docker 官方说明](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)。
