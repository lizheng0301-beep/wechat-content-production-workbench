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
- 自动配图：按正文每约 500 字至少 1 张图规划位置，先抓取原文相关图片，缺口再调用 AI。
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
- `POST /api/drafts/{id}/readability`：只增加重点标记，不改写正文。
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
