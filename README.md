# 编辑部工作台：公众号 v1

一个运行在 Mac 本机的单用户公众号内容工作台。它把热点、选题、写作、配图、排版、公众号草稿和数据复盘放进同一条内容流里。

本项目默认只绑定 `127.0.0.1`，不会自动群发公众号文章。API Key、公众号 AppSecret 和 access token 只从启动环境读取，不写入数据库、文章文件、日志或导出包。

## 功能概览

- 总览：查看今日热点、进行中的文章、待确认草稿和最近表现。
- 热点雷达：同步 AI HOT，支持 24 小时、7 天和分类筛选，并保留来源链接。
- 选题库：保存选题角度、目标读者、时效窗口、个人观察和 H/K/R 评分。
- 写作台：读取本地风格配置和历史样本，生成标题候选、文章结构、长文初稿和证据区分。
- 选择题式协作：写作前通过选项选择角度、目标和口吻，补充内容为可选项。
- 四层质检：检查禁用词、禁用标点、空泛表达、事实支撑和具体案例。
- 素材库：导入网页图片、登记来源和版权备注，也可以生成原创封面和正文插图。
- 自动配图：按正文每约 500 字至少 1 张图规划位置，先抓取原文相关图片，缺口再调用 AI；AI 图统一采用编辑视觉编译器，避免泛化科技插画。
- 3s 原则：内容首屏和配图缩略图都必须让读者在约 3 秒内看懂重点、冲突或收益。
- 选中段落生图：在编辑模式选中一段正文，自动转换为生图 Prompt，确认后调用 Right Code。
- 公众号排版：编辑模式和排版预览分离，支持标题、段落、引用、列表、分隔线、封面和正文图片；可整理重点加粗、高亮、下划线和强调色。
- 草稿箱：通过微信公众号接口上传图片并创建图文草稿，只创建草稿，不执行群发。
- 数据复盘：同步可获得的阅读、分享、点赞和评论数据；缺失字段显示“暂无数据”，不估算。
- 本地备份：导出和恢复文章、选题、热点、素材和数据台账。

## 环境要求

- macOS
- Python 3.11 或更高版本
- 一个浏览器

核心服务只使用 Python 标准库。`certifi` 和 `Pillow` 是可选依赖：前者用于更稳定的 HTTPS 证书校验，后者用于生成更丰富的本地视觉卡。

可选安装：

```bash
python3 -m pip install certifi pillow
```

## 启动

```bash
cd "/Users/你的用户名/Desktop/公众号"
python3 app.py
```

然后打开：<http://127.0.0.1:8765/>

如果出现 `Address already in use`，说明 8765 端口已有工作台进程。关闭原进程后再启动，或者使用其他端口：

```bash
WORKBENCH_PORT=8766 python3 app.py
```

改过 `app.py` 或环境变量后，需要停止旧进程并重新启动；仅刷新浏览器不会重新加载 Python 配置。

## 配置模型

所有密钥都应该在启动前通过环境变量提供。不要把真实值写进 README、脚本、文章或 `.env` 文件后再提交。

### Right Code / GPT-5.6 Sol 文本中转

Right Code 的 Codex 文档使用 `https://www.rightapi.ai/codex/v1`，工作台通过兼容的 `chat/completions` 接口调用。配置 Right Code 后，它会优先于 YuzAPI 成为写作、排版校准和视觉提示词整理模型；文本结果仍会记录实际模型。Right Code 文档说明其兼容层会替换 `system` 指令，因此工作台会把编辑规则合并进用户请求。[Codex 配置文档](https://docs.right.codes/docs/rc_cli_config/codex.html)、[Curl 调用示例](https://docs.right.codes/docs/rc_extension/curl.html)

```bash
export RIGHTCODE_API_KEY="你的 Right Code Key"
export RIGHTCODE_TEXT_BASE_URL="https://www.rightapi.ai/codex/v1"
export RIGHTCODE_TEXT_MODEL="gpt-5.6-sol"
export RIGHTCODE_TEXT_TIMEOUT="90"
```

如果后台明确支持 JSON 模式，再开启：

```bash
export RIGHTCODE_JSON_MODE="1"
```

### YuzAPI / GPT-5.6 Sol 文本模型

如果你有 YuzAPI 的中转 Key，推荐单独使用 `YUZAPI_*` 变量。工作台会请求 OpenAI 兼容的 `v1/chat/completions` 接口，默认模型为 `gpt-5.6-sol`。相关配置入口见 [YuzAPI 配置文档](https://yuzapi.fun/docs/configuration)。

```bash
export YUZAPI_API_KEY="你的 YuzAPI Key"
export YUZAPI_BASE_URL="https://yuzapi.fun/v1"
export YUZAPI_MODEL="gpt-5.6-sol"
```

如果该中转不接受 `temperature` 参数，再增加：

```bash
export YUZAPI_OMIT_TEMPERATURE="1"
```

如果该中转明确支持 JSON 模式，可增加：

```bash
export YUZAPI_JSON_MODE="1"
```

`YUZAPI_*` 优先级高于 `OPENAI_*` 和 `DEEPSEEK_*`，方便把更强的模型用于文章生成、重点整理和质检；没有 YuzAPI Key 时，工作台仍按 OpenAI、DeepSeek、本地模板的顺序降级。

当前版本会显示实际使用的提供商和模型。YuzAPI 遇到临时不可用、TLS、连接重置、超时或 5xx 时，默认只尝试一个备用 OpenAI 兼容配置；成功后会在文章状态中明确标记“备用模型”，不会把结果伪装成 GPT-5.6 Sol。设置 `YUZAPI_FALLBACK_ENABLED=0` 可关闭备用链路。

YuzAPI 遇到 TLS、连接重置或 5xx 响应时，工作台会自动重试一次；长文章请求超时不会重复发送同一份长 Prompt，而是进入一次备用尝试。Right Code 文本等待由 `RIGHTCODE_TEXT_TIMEOUT` 控制，默认 90 秒；针对 Right Code 的写作请求会自动压缩风格摘要和历史样本，减少中转接口因提示词过长而超时。备用模型等待由 `TEXT_MODEL_FALLBACK_TIMEOUT` 控制，默认 45 秒。微信草稿创建不会使用这套自动重试，避免网络响应丢失时重复创建草稿。

### DeepSeek 文本模型

工作台使用 OpenAI 兼容的 Chat Completions 接口，因此 DeepSeek 只需要替换 Base URL 和模型名：

```bash
export OPENAI_API_KEY="你的 DeepSeek Key"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_MODEL="deepseek-chat"
```

如果中转服务提供 OpenAI 兼容接口，也可以把 `OPENAI_BASE_URL` 改成中转服务地址。

也可以直接使用 DeepSeek 变量名，效果相同：

```bash
export DEEPSEEK_API_KEY="你的 DeepSeek Key"
export DEEPSEEK_BASE_URL="https://api.deepseek.com"
export DEEPSEEK_MODEL="deepseek-chat"
```

### OpenAI 兼容图片模型

```bash
export OPENAI_IMAGE_API_KEY="你的图片模型 Key"
export OPENAI_IMAGE_BASE_URL="https://api.openai.com/v1"
export OPENAI_IMAGE_MODEL="gpt-image-1"
```

没有配置图片模型时，工作台会生成一张明确标注为本地模式的编辑视觉卡，不会假装调用了外部模型。

### Right Code 图片中转

Right Code 图片接口是异步任务：先提交任务，再轮询 `task_id`，工作台会自动等待结果并把 PNG 保存到本地素材库。

```bash
export RIGHTCODE_API_KEY="你的 Right Code Key"
export RIGHTCODE_IMAGE_MODEL="gpt-image-2"
export RIGHTCODE_IMAGE_SIZE="16:9"       # 封面；正文默认使用 4:3
export RIGHTCODE_IMAGE_RESOLUTION="1K"
export RIGHTCODE_IMAGE_TIMEOUT="150"
```

图片接口默认使用 `https://www.rightapi.ai/draw/v1/images/generations`，提交后轮询 `https://www.rightapi.ai/v1/tasks/{task_id}`。Right Code 文档支持的比例包括 `1:1`、`16:9`、`9:16`、`4:3` 和像素尺寸；工作台会把旧的 `3:2` 配置兼容转换为 `4:3`。[图片生成文档](https://docs.right.codes/docs/rc_draw/images-generations.html)、[任务查询文档](https://docs.right.codes/docs/rc_draw/tasks.html)

公众号上传前会把 WebP、AVIF、GIF 和 SVG 等不稳定格式转换成 JPEG/PNG；Mac 优先使用系统 `sips`，Pillow 可用时使用 Pillow 转换，避免微信返回 `unsupported file type`。

正式模型名以 Right Code 后台模型列表为准，例如：

- `gpt-image-2`
- `gpt-image-2-vip`
- `nano-banana`
- `nano-banana-2`
- `nano-banana-pro`
- `nano-banana-2-lite`

旧配置 `RIGHTCODE_IMAGE_MODEL=image2` 会自动兼容映射为 `gpt-image-2`。如果要使用 VIP，明确设置：

```bash
export RIGHTCODE_IMAGE_MODEL="gpt-image-2-vip"
```

Right Code 官方接口文档：<https://docs.right.codes/docs/rc_draw/>

### 编辑视觉生图策略

自动配图的优先级固定为：

1. 原文来源图：只从页面正文主内容区域导入能通过尺寸、格式和内容过滤的候选图；评论区、回复区、推荐区、侧栏、头像、图标和装饰图全部排除，并记录原图 URL、来源页面和版权待确认状态。
2. 编辑视觉图：来源图不足时，工作台从缺口段落提炼一个主体、动作或关系，再使用 `gc-minimal-zine-poster-v0-1` 的方法编译提示词。
3. 离线信息卡：没有任何图片模型配置时，才生成明确标注为本地模式的信息卡，不会假装调用 AI。

编辑视觉编译器会固定加入旧纸张、平视扫描稿、大面积留白、中小型视觉集群、撕纸或复印质感、一个高饱和色锚点和明确的反向约束。正文图主体会比极简海报中的微小锚点稍稍放大，但仍保留留白，不会变成满幅插画。它不会把段落直接复制进 Prompt，也不会默认加入机器人、发光网络、漂浮图标、仪表盘、蓝紫渐变或随机人物。正文图默认使用横版 `4:3`，封面仍由 `RIGHTCODE_IMAGE_SIZE` 单独控制。历史上未标记为正文区域的来源图不会再次被自动配图复用，文章中的旧引用会被移除但不会删除素材文件。

### 3s 原则

这是内容和图片共同遵守的首屏原则，不是要求文章变成短视频文案：

- 内容 3s：标题和开头首屏的前 3 到 5 句，要交代具体事件或对象、冲突或看点，以及读者继续阅读能得到的判断或帮助。质检会检查首屏是否从具体事实切入，是否有明确看点和阅读承诺。
- 图片 3s：缩略图或首屏停留约 3 秒时，要能先看懂一个主体、一个动作或冲突、一个视觉重点。看不懂的抽象背景、装饰图标和无意义的信息卡不算合格配图。
- 执行顺序：先删装饰，再强化主体关系，最后才补色彩、纸张和排版质感。

### 编辑部阅读版排版

“整理重点”会先尝试让当前首选文本模型只添加结构标记，模型超时、返回无效 JSON、改动原文或把标题全部堆在开头时，会自动切换到本地排版规则。两种模式都必须通过正文纯文本一致性校验，不允许借排版之名改写文章。

- 一级正文标题使用 `##`，通常为 2–4 个，按文章前段、中段和后段分布；渲染时自动增加 `01 / 02 / 03` 编辑编号、朱砂侧边、浅纸色背景和错位边框。
- 二级标题使用 `###`，最多 2 个，用绿色细边形成较弱层级。
- 首段固定使用一个引言框；1500 字以内再选 1 个语义框，1500–2800 字再选 2 个，超过 2800 字再选 3 个。框型优先分配给方法、技巧、风险和金句，并均匀穿插在长段正文之间。
- 普通正文按句意拆成每段 1–3 句、约 45–100 字，最长不超过 120 字；只增加段落边界，不改写原句。旧文章和手动粘贴内容即使没有重新整理，预览与微信发布渲染层也会执行同样的兜底拆段。
- 正文采用 17px 字号和 1.95 倍行高，每连续 3 个普通段落增加一次较大的呼吸间距，标题、图片和提示框会自动重置段落节奏。
- `**加粗**` 用于核心判断，`==高亮==` 用于金句，`__下划线__` 用于方法和技巧，`^^朱砂色^^` 用于风险与边界。
- 本地兜底规则会把 4–7 处重点分散到全文，不再只处理文章开头；重复点击会先清理旧标记再重新布局，避免样式不断叠加。
- 排版预览和微信公众号草稿共用同一套颜色、间距、标题编号、图片边框和提示框语义。摘要在保存和发布前会转为纯文本，不会把 Markdown 控制符带入公众号后台。

### 微信公众号

在微信公众号后台获取开发者 AppID 和 AppSecret，并把运行本机的公网出口 IP 加入 IP 白名单。然后启动工作台：

```bash
export WECHAT_APP_ID="你的公众号 AppID"
export WECHAT_APP_SECRET="你的公众号 AppSecret"
export WECHAT_AUTHOR="作者名"
python3 app.py
```

工作台支持：

1. 获取 access token。
2. 上传封面和正文图片。
3. 创建图文草稿。
4. 查询草稿列表。
5. 同步可获得的图文数据。

工作台不会调用群发接口。公众号后台的 IP 白名单必须允许当前网络出口 IP；本地服务绑定 `127.0.0.1` 不等于公众号接口看到的公网 IP。

## 推荐使用流程

1. 启动服务并打开总览。
2. 在热点雷达同步 AI HOT，点击热点进入选题。
3. 用选择题确定文章角度、目标和口吻，补充真实观察可选但建议填写。
4. 进入写作台，点击“重新整理长文”。
5. 在“编辑模式”修改正文；点击“排版预览”检查公众号阅读效果。
6. 选中某一段文字，点击“选中段落生图”，确认自动转换的 Prompt。
7. 选择封面，运行“校对全文”，处理质检提示。
8. 点击“创建草稿”，明确授权后写入微信公众号草稿箱。
9. 发布后到数据复盘同步真实返回数据。

## 数据与隐私

以下内容属于本地工作数据，默认不会进入公开 Git 仓库：

- `.workbench/`：SQLite 数据库、素材文件、缓存和运行状态。
- 根目录 Markdown：历史文章、来源卡、初稿、质检报告和素材清单。
- `*_assets/`：文章配图、封面、截图和生成脚本。
- 根目录 `.docx`：公众号导入版文档和其他本地交付文件。
- PNG、JPG、GIF、WebP 等本地媒体文件。

如果需要备份数据，请使用工作台里的“导出工作台包”，并把备份文件放在仓库目录之外。公开仓库只保存可复用的程序代码和使用说明。

## API 路径

- `GET /api/dashboard`：总览数据。
- `POST /api/hot/sync`：同步 AI HOT v1 热点。
- `GET/POST /api/topics`：读取和创建选题。
- `POST /api/drafts/{id}/generate`：生成或整理初稿。
- `POST /api/drafts/{id}/quality`：执行自动质检。
- `POST /api/drafts/{id}/save`：保存草稿。
- `GET /api/drafts/{id}/markdown`：下载 Markdown 成稿。
- `POST /api/assets/import-url`：从公开网页提取图片。
- `POST /api/assets/prompt-from-selection`：把选中文字转换成图片 Prompt。
- `POST /api/assets/generate-from-prompt`：生成并保存正文配图。
- `POST /api/assets/generate-image`：生成封面或普通原创配图。
- `POST /api/drafts/{id}/auto-images`：按 500 字节奏规划配图，来源图优先，AI 补缺口。
- `POST /api/drafts/{id}/readability`：调用已配置的 5.6 Sol 做排版校准，只给原句增加 `##/###`、引言、方法、风险、金句和少量重点标记；服务端校验正文纯文本一致，不通过时使用本地兜底排版。
- `POST /api/drafts/{id}/publish`：授权后创建微信公众号草稿，不群发。
- `GET /api/wechat/drafts`：读取微信公众号草稿列表。
- `POST /api/metrics/sync`：同步公众号可获得的数据。
- `GET /api/status`：查看本地、模型、AI HOT、公众号和风格配置状态。
- `GET /api/export/package`：导出本地工作台备份包。
- `POST /api/import`：恢复 JSON 或 ZIP 备份。

## Git 开发说明

公开仓库只跟踪程序代码、README 和 `.gitignore`。本地文章、图片、数据库和公众号数据不会被自动提交。

```bash
git status
git add app.py index.html README.md .gitignore
git commit -m "feat: update workbench"
git push origin main
```

如果误把本地数据加入暂存区，先取消跟踪但保留本地文件：

```bash
git rm -r --cached -- '*.md' '*_assets' '*.docx' '*.png' '*.jpg' '*.jpeg' '*.gif' '*.webp'
git add .gitignore README.md
```

## 安全提醒

- 不要把 API Key、AppSecret、access token 写入代码或文章。
- 不要把 `.workbench/` 上传到公开仓库。
- 公众号流程只创建草稿，发布前必须人工确认。
- 来源图片需要人工核验版权；AI 生成图片也应保留生成模型和 Prompt 记录。
- 公开仓库发布前，检查 `git diff --cached` 和 `git ls-files`，确认没有私人内容。
