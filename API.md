# 服务接口

服务默认监听 `127.0.0.1:8765`。云端使用 `--host 0.0.0.0`，必须设置 `SERVICE_API_TOKEN`。以下示例使用 Python 标准库，可以从其他程序调用；服务器部署见 [CLOUD_DEPLOY.md](CLOUD_DEPLOY.md)。

## 上传、提交、查询

```python
import json
import os
import time
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

base = os.environ.get("ELDER_SERVICE_URL", "http://127.0.0.1:8765")
token = os.environ["SERVICE_API_TOKEN"]

def call(path, body=None, content_type="application/json", headers=None):
    h = {"Authorization": "Bearer " + token, **(headers or {})}
    if body is not None:
        h["Content-Type"] = content_type
    with urlopen(Request(base + path, data=body, headers=h), timeout=300) as response:
        return json.load(response)

audio = Path("interview.mp3")
upload = call("/api/uploads", audio.read_bytes(), "application/octet-stream",
              {"X-Filename": quote(audio.name)})
job = call("/api/jobs", json.dumps({
    "mode": "audio", "upload_id": upload["upload_id"]
}).encode("utf-8"))["job"]

while job["status"] not in {"completed", "failed"}:
    time.sleep(5)
    job = call("/api/jobs/" + job["id"])["job"]
    print(job["status"], job["message"])

if job["status"] == "completed":
    query = urlencode({"job_id": job["id"], "name": "story_polished.md"})
    request = Request(base + "/api/files?" + query,
                      headers={"Authorization": "Bearer " + token})
    with urlopen(request) as response:
        Path("story_polished.md").write_bytes(response.read())
else:
    print("处理未全部完成，请查看任务消息和 manifest；原稿可能已经保存。")
```

示例把音频读入内存，适用于当前上传上限内的普通文件。上传接口接收原始文件流，并非 multipart 表单；要求 Content-Length，不接受 chunked。浏览器上传由界面自动完成。

## 接口表

| 方法与路径 | 用途 |
|---|---|
| `GET /api/health` | 无需令牌的服务身份、鉴权要求和上传大小限制；不返回模型密钥 |
| `POST /api/uploads` | 上传音频；请求头 `X-Filename` 为 URL 编码文件名；返回 `upload_id` |
| `POST /api/jobs` | JSON：`{"mode":"audio","upload_id":"…"}`，提交完整解析任务 |
| `POST /api/jobs` | JSON：`{"mode":"polish","result_id":"…"}`，仅整理已有结果 |
| `POST /api/jobs` | JSON：`{"mode":"resume","result_id":"…"}`，转写完成但原稿尚未生成时继续生成文稿 |
| `GET /api/jobs` | 返回 `{"jobs":[…]}` |
| `GET /api/jobs/<id>` | 返回 `{"job":{…}}` |
| `GET /api/results` | 返回已有结果、状态及可下载文件清单 |
| `GET /api/files?result_id=…&name=story_polished.md` | 获取指定结果文件；也可用 `job_id` 代替 `result_id` |

文件请求可附加 `download=1`。仅允许读取原稿、转写、整理稿、修改记录和 manifest 等固定文件，不能借该接口读取配置、密钥、任意服务器路径。声音素材完整保存在服务端结果目录中，此版本没有公开音频素材下载接口。

任务状态通常为 `queued`、`asr`、`original_ready`、`polishing`，恢复文稿时还可出现 `generating_story`，最后为 `completed` 或 `failed`。具体流程可能跳过短暂中间状态。轮询间隔建议 5 秒。失败详情以 `message` 和 `manifest.polish` 为准；整理失败不会删除已经生成的原稿。

仅本机回环部署支持 `{"mode":"audio","path":"音频完整路径"}` 或 `{"mode":"polish","path":"结果目录完整路径"}`。云端拒绝服务器任意路径，须使用上传 ID 或已有结果 ID。

## 访问规则

- 启用服务令牌时，除首页和健康检查外均要求 `Authorization: Bearer …`。
- 无 Origin 请求头的程序调用，在 Bearer 校验通过后不需要 CSRF 令牌。浏览器仍需同源校验和页面 CSRF 令牌，网页已实现。
- 默认允许 localhost/127.0.0.1 的 Host。公网域名须配置 `SERVICE_PUBLIC_ORIGIN=https://实际域名`；代理保留 Host。接口不启用任意跨站 CORS。
- 当前为单服务、单任务队列、单个可信团队共用令牌。不要运行多个进程共享同一数据目录；任务文件持久化不等于分布式队列。
- 返回 400 表示输入错误，401 表示令牌无效，403 表示访问不允许，413 表示上传超限；请求失败时不要无限自动重试。

整理模型使用 JSON 输出，依次验证结构、可读性、来源和语义，另允许一次有限修复。一次整理最多调用两次生成、两次可读性检查和两次语义核对；接口自动重试关闭。参考：[DeepSeek JSON 输出文档](https://api-docs.deepseek.com/guides/json_mode/)。
