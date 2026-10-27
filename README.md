# Elder Story Studio · 老人口述故事工作台

一个可部署到云服务器、也可在 Windows 本机运行的独立服务：上传老人的访谈录音，提取其中的个人经历，生成可以直接交给视频制作者的文案（配音旁白、分镜、老人原声位置）。原稿、整理稿和音频另行保留。

## 云服务器与其他程序调用

项目包含 `Dockerfile`、`compose.yaml` 和配置模板，部署步骤见 [CLOUD_DEPLOY.md](CLOUD_DEPLOY.md)。部署后可通过网页上传音频、查看任务、对照原稿与整理稿，也可通过 [API.md](API.md) 中的接口随时调用。云端访问要求服务令牌；模型密钥仅保存在服务端。

## Windows 使用

首次使用先安装 Python 3.11 或 3.12，并在项目目录的 PowerShell 中执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

如果安装的是 Python 3.11，将第一行的 `-3.12` 改为 `-3.11`。仅首次创建 `.env`；已有配置请直接编辑。填入自己的 DeepSeek API 密钥后保存；改用本机 Codex 见下文“选择写文案的大模型”。首次识别会下载语音模型，依赖和模型下载时间取决于网络；ffmpeg 由 Python 依赖提供，无须另外安装。

双击 **启动服务.bat**。浏览器将打开本机的操作界面，默认地址是 `http://127.0.0.1:8765`。服务只接受本机访问。关闭网页不会停止后台服务；需要停止时双击 **停止服务.bat**。软件不会自动设置开机启动。

- **处理新录音**：选择音频文件或粘贴本机音频的完整路径，可顺手填写这位老人的姓名、地名等热词，点击开始。软件会依次预处理、识别、导出声音素材、生成原稿，然后整理文稿，最后生成视频文案。
- **整理已有文稿**：选择已有结果，或粘贴一个包含 `story.json` 和 `transcript.json` 的结果目录。不会重新识别音频。
- **给别人用的提交入口**：打开 `http://127.0.0.1:8765/submit`，对方只需上传录音，等待处理后在同一页面预览并下载视频文案和分镜表；看不到原稿、转写和其他人的结果。提交后会得到一个专属链接，可以关掉页面稍后再回来看。云端部署时需设置提交码才会开放，见 [CLOUD_DEPLOY.md](CLOUD_DEPLOY.md)。
- 处理完成后，结果区最上方是“视频文案”，可下载文案和 Excel 分镜表直接交给视频制作者；下方可对照查看“原稿”“整理稿”和“修改记录”。整理稿未通过核对时，视频文案会按原稿生成并在开头注明，页面也会明确显示整理失败。

任务在本机排队处理。网页默认只在同一台电脑上使用；要给别人用，见下文“让别人通过外网访问”。DeepSeek 文稿生成和整理会使用已经配置的 API 密钥，并上传本次文稿和转写文本供模型处理；语音识别在本机运行。不会把原音频作为整理请求上传。

## 选择写文案的大模型

语音识别之后，提炼经历、写文案和分镜、整理文稿都由一个大模型完成。在 `.env` 里用 `LLM_BACKEND` 选择：

- `deepseek`（默认）：调用 DeepSeek 接口，需要 `LLM_API_KEY`。`LLM_BASE_URL`、`LLM_MODEL` 也可改成其他兼容 OpenAI 接口的服务（通义千问、Kimi 等），换模型后请先用一段录音核对效果。
- `codex`：调用这台电脑上的 Codex 命令行，用你已登录的 Codex 账号，不需要 API 密钥。先安装 Codex（`npm install -g @openai/codex`），在命令行运行一次 `codex login` 登录；找不到 `codex` 时在 `.env` 里用 `CODEX_PATH` 指定完整路径。每次调用以只读沙箱、空白临时目录运行 `codex exec`，提示词经标准输入传入。Codex 比接口调用慢，单次默认最多等 900 秒，可用 `CODEX_TIMEOUT` 调整；`CODEX_MODEL` 可指定模型。

改完 `.env` 后先双击 **停止服务.bat** 再启动。网页健康检查 `/api/health` 的 `llm_backend` 会显示当前使用的是哪一个。

## 让别人通过外网访问（内网穿透）

在一台 Windows 电脑上跑服务，再用内网穿透把它开放到外网，别人就能在自己的电脑或手机上打开 `/submit` 提交录音、拿文案。服务本身仍只监听 `127.0.0.1`，由穿透程序把外网请求转进来。

**1. 在 `.env` 打开对外模式**（三项都要填）：

```ini
SERVICE_PUBLIC_ORIGIN=https://audio.你的域名.com
SERVICE_API_TOKEN=一串长随机字符（团队自己进工作台用）
SUBMIT_ACCESS_CODE=发给提交者的提交码（与上面不同）
```

填了 `SERVICE_PUBLIC_ORIGIN` 后，服务按云端规则运行：工作台和接口都要访问令牌，不再接受本机文件路径，提交页凭提交码使用。你自己在这台电脑上打开 `http://127.0.0.1:8765/` 也需要输入令牌。保存后重启服务。

**2. 选一种穿透方式：**

- **Cloudflare Tunnel（推荐，域名已托管在 Cloudflare 时）**：在 Cloudflare 控制台 Zero Trust → Networks → Tunnels 新建隧道，按页面提示在这台 Windows 上安装 cloudflared 并运行它给出的 `cloudflared.exe service install …` 命令（会装成系统服务，开机自动连）。再给隧道添加 Public Hostname：子域名填 `audio`，Service 选 `HTTP`、地址 `localhost:8765`。不需要开放路由器端口，自带 HTTPS。
- **frp（已有一台有公网 IP 的服务器时）**：服务器上运行 frps，这台 Windows 上运行 frpc，把本机 `127.0.0.1:8765` 映射出去，再在服务器的 nginx 上用 HTTPS 域名反向代理过去（保留 Host 头，`client_max_body_size 256m`）。
- 不建议用每次地址都会变的临时隧道（如 trycloudflare 的随机域名）：地址一变就要改 `SERVICE_PUBLIC_ORIGIN` 并重启。

**3. 验证**：在别的网络（比如手机关掉 Wi-Fi）打开 `https://audio.你的域名.com/submit`，输入提交码上传一段短录音。把这个网址和提交码发给别人即可。

注意：这台电脑需要保持开机、不睡眠、服务和穿透程序都在运行；录音和结果都保存在这台电脑上。来自提交页的任务同时最多处理 10 份，超出时对方会看到稍后再试。

## 本机运行环境

启动脚本默认使用项目内的 `.venv\Scripts\python.exe`。如需复用已有环境，可在本机创建 `runtime.json`，设置 `python` 字段为解释器的完整路径；该配置优先于 `.venv`。

`.env` 存放当前模型配置。请不要把 `.env`、`runtime.json` 或个人录音结果发给其他人。源码发布包应排除这些文件。换一台电脑时，需要安装依赖并重新填写配置，不能直接移动旧虚拟环境。

语音识别、切分和声音素材导出在运行服务的机器上完成；大模型（默认 DeepSeek，可换成本机 Codex）用于原稿生成、整理及审校。正常无重试的完整流程通常调用文字模型4次。`--skip-llm` 可只运行本机处理，不使用 DeepSeek 额度。当前版本尚未记录逐次 token 用量或实施费用上限，使用前请自行设置供应商侧额度。

## 输出

| 文件 | 说明 |
|---|---|
| `video_script.md` | 给视频制作者的文案：配音旁白、分镜（画面、旁白、情绪、估算时长）、背景资料、老人原话在原录音中的时间位置、制作前需确认事项 |
| `video_script.csv` | 分镜表，Excel 可直接打开 |
| `video_script.json` | 视频文案的结构化数据，供其他程序使用 |
| `story.json`、`story.md` | 软件第一阶段原始经历稿 |
| `transcript.json`、`transcript.txt` | 自动转写与原始说话人标签 |
| `voice/` | 原流程导出的候选声音素材，不由整理模块改写 |
| `story_polished.json`、`story_polished.md` | 标注“非逐字转录”的整理稿 |
| `polish_changes.json` | 实质修改、来源句子编号和未解决问题 |
| `manifest.json` | 原始处理和整理状态、结果位置 |

整理会参考原稿和原始转写，补标点、分段、去除机械重复，并修复有依据的语病。生成后先独立检查可读性，再核对来源和人物归属；任一检查失败只允许一次修复，不无限重试。无法理解的片段会保守处理并说明，不从网上或文件名补全情节。原话字段不静默修改，引用匹配状态与语义判断单独呈现。模型审校不能替代原音复听，也不能证明历史事实已经外部核实。

分镜的画面和情绪沿用原稿，可能包含未经来源核实的构想；本步骤只整理旁白。旁白与正文的对应状态是文字匹配检查，“未逐字匹配”不等于内容一定错误，也不代表已完成语义分镜对齐。

同一结果的整理任务不允许同时运行。重新整理时保留前一版整理结果，并更新当前状态。原稿、转写、音频和训练标注保持不变。

视频文案不调用模型，只重新排版已有结果：整理稿通过核对时使用整理稿（失败的重新整理不会顶掉此前通过的整理稿），否则使用原稿。时长按每分钟约 240 字估算，画面描述是创作建议，不是老人原话。旧结果在网页中打开时会自动补生成；原稿或整理稿更新后会自动重新生成。

## 命令行与接口

以下命令中的 `python` 应使用已经安装本项目依赖的解释器。

```powershell
python recorder.py 'C:\audio\访谈.mp3' -o 'outputs\一次新任务'
python recorder.py --polish-only 'outputs\一次新任务'
python recorder.py --resume-result 'outputs\原稿生成失败的任务'
python recorder.py 'C:\audio\访谈.mp3' -o 'outputs\只生成原稿' --skip-polish
python recorder.py 'C:\audio\访谈.mp3' -o 'outputs\只转写' --skip-llm
python polish.py 'outputs\一次新任务'
python export_preview.py 'outputs\整理失败但想查看候选的任务'
python video_script.py 'outputs\一次新任务'
python service.py --port 8765
```

`--skip-llm` 跳过经历稿和整理；`--skip-polish` 保留经历稿但不整理。整理失败时命令返回非零退出码，不影响已经保存的原始处理结果。新录音的输出目录不得覆盖已有结果；网页会自动新建独立目录。

`--resume-result` 适用于转写和音频素材已经完成、原稿生成失败的目录，从已有转写继续，不重新识别音频。如果原稿已存在，请使用 `--polish-only`；恢复模式拒绝覆盖原稿。

`export_preview.py` 不调用模型，只将失败诊断中已保存且结构有效的候选导出为 `review_preview.md/json`，明确标为未通过核对的预览，不修改正式输出和完成状态。模型接口报余额不足时，处理好服务端账户余额后再执行 `--polish-only` 即可。

接口提供健康检查、音频上传、任务队列、任务状态、结果列表和白名单文件下载。浏览器写入请求使用页面防跨站令牌；云端另要求 Bearer 访问令牌，具体接口与示例见 [API.md](API.md)。

## 验证

使用 Python 标准库 unittest 运行离线验证：

```powershell
python -m unittest discover -s tests -v
```

测试使用模拟模型响应和临时数据，不需要 API 密钥，不调用真实 DeepSeek。离线测试验证程序行为，不能代表真实录音识别准确率、文稿事实正确率或云服务器部署已经通过验收。仓库不包含个人录音、转写、生成文稿和本机测试报告。
