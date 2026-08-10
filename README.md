# 公众号内容生产工作台

一个面向微信公众号创作者的本地 AI 内容生产工作台，将热点发现、选题判断、长文生成、规则质检、自动配图、公众号排版、草稿创建和发布后复盘串成一条可操作的内容工作流。

项目默认只监听 `127.0.0.1`，本地内容、SQLite 数据库和 API 凭据不会提交到 Git。系统只创建微信公众号草稿，不调用群发接口。

## 当前能力

- 热点雷达：从 AI HOT 公共只读接口同步热点并保留来源链接。
- 选题库：记录核心角度、目标读者、作者观察、真实经历、情绪节点和 H/K/R 评分。
- AI 写作：调用 DeepSeek 或其他 OpenAI 兼容文本模型，生成结构化标题、摘要、正文、证据和声明。
- 排版与质检：对正文执行 12 项确定性规则检查；模型排版结果必须通过“正文未被改写”校验，否则回退到本地规则。
- 素材与配图：来源图优先；缺口可调用 RightCode/OpenAI 兼容图片接口，未配置图片模型时生成明确标注的本地信息卡。
- 公众号草稿：上传封面和正文图片，使用统一渲染链生成预览并写入微信公众号草稿箱。
- 数据复盘：按天同步微信实际返回的阅读、分享、收藏等指标；无数据接口权限时可手工登记公众号后台真实数据。
- 本地备份：导出和恢复 JSON，或导出包含素材文件的 ZIP 工作台包。

## 能力边界

这不是多 Agent、RAG、向量数据库或微调项目。当前核心是固定业务工作流、直接模型调用、确定性校验和本地数据管理。

- 单用户、本地运行，没有登录、多租户、云部署和持久任务队列。
- 自动配图任务使用进程内线程，服务重启后运行中的任务状态会丢失。
- 来源图片版权仍需人工确认；AI 结果和规则质检不能代替事实核查。
- “创建草稿”只写入公众号草稿箱，不会正式发布或群发。
- 本地预览与公众号草稿共用同一套服务端渲染函数和内联样式，尽量保持一致；微信后台或客户端仍可能清洗 HTML、替换字体或微调样式。
- 数据接口最多同步昨天以前的近 7 个完整自然日。账号没有图文分析权限时，微信会返回 `48001`，草稿功能不受影响。
- 正式发布文章需要人工登记标题和发布日期，系统再尝试匹配微信统计；不会自动发现已发布文章。
- 数据复盘不生成演示数字，也不声称支持代码中没有的点赞、评论指标。

## 技术架构

```text
浏览器（原生 HTML / CSS / JavaScript）
                |
        本地 HTTP JSON API
                |
Python ThreadingHTTPServer
  |             |              |
SQLite      模型适配层       微信公众号 API
  |        文本 / 图片         草稿 / 数据
本地素材目录
```

- 后端：Python 3.11+ 标准库，`ThreadingHTTPServer`
- 前端：单页原生 HTML/CSS/JavaScript
- 数据：SQLite，默认位于 `.workbench/workbench.sqlite3`
- 素材：默认位于 `.workbench/assets/`
- 可选依赖：`certifi` 用于 HTTPS CA；`Pillow` 用于图片转换和更丰富的本地视觉卡

详细设计见 [架构说明](docs/architecture.md)。

## 快速启动

### Windows PowerShell

```powershell
cd "你的项目目录"
python app.py
```

### macOS / Linux

```bash
cd "/path/to/wechat-content-production-workbench"
python3 app.py
```

浏览器打开 <http://127.0.0.1:8765/>。修改环境变量或 `app.py` 后必须重启服务，单纯刷新浏览器不会重新加载后端配置。

端口冲突时：

```powershell
$env:WORKBENCH_PORT = "8766"
python app.py
```

核心功能无需安装第三方包。可选安装：

```bash
python -m pip install certifi pillow
```

## 最小配置

凭据只通过启动环境传入。不要把真实 Key、AppSecret 或 access token 写进仓库。

### DeepSeek 文本 + RightCode 图片

推荐分别使用文本和图片专用变量。不要把图片令牌写成 `RIGHTCODE_API_KEY`，否则它也会成为优先文本提供商。

```powershell
$env:DEEPSEEK_API_KEY = "你的 DeepSeek Key"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:DEEPSEEK_MODEL = "deepseek-chat"

$env:RIGHTCODE_IMAGE_API_KEY = "你的 RightCode Key"
$env:RIGHTCODE_IMAGE_BASE_URL = "https://www.rightapi.ai/draw"
$env:RIGHTCODE_TASK_BASE_URL = "https://www.rightapi.ai"
$env:RIGHTCODE_IMAGE_MODEL = "gpt-image-2"
$env:RIGHTCODE_IMAGE_SIZE = "16:9"
$env:RIGHTCODE_IMAGE_RESOLUTION = "1K"
```

### 微信公众号

```powershell
$env:WECHAT_APP_ID = "你的 AppID"
$env:WECHAT_APP_SECRET = "你的 AppSecret"
$env:WECHAT_AUTHOR = "作者名"
```

公众号后台还需要把运行电脑的公网出口 IPv4 加入“设置与开发 -> 基本配置 -> IP 白名单”。不要填 `127.0.0.1`。

配置后先在状态面板执行“测试公众号连接”。测试只验证 access token；数据分析权限要以实际同步结果为准。

全部变量、提供商优先级和错误排查见 [集成配置](docs/integration-guide.md)。

## 推荐工作流

1. 同步 AI HOT，选择一个有可靠来源的热点。
2. 写下自己的核心判断、目标读者和真实观察，再创建选题。
3. 在写作台选择篇幅并生成或整理长文。
4. 人工核对事实、来源和作者经历，运行规则质检。
5. 整理重点并自动配图，逐张确认来源、版权和视觉含义。
6. 在“排版预览”检查最终渲染，选择封面。
7. 明确授权后创建公众号草稿，再到公众号后台完成最终检查和发布。
8. 发布后登记文章，等待微信统计生成，再同步或手工登记真实指标。

## 数据与安全

以下内容默认被 `.gitignore` 排除：

- `.workbench/`：SQLite、素材、缓存和运行状态
- `.env`、`*.env`：本地凭据配置
- 根目录内容稿、DOCX、图片和素材目录
- 运行日志和 Python 缓存

公开提交前至少执行：

```bash
git status --short
git diff --cached
git ls-files
```

使用界面的“导出工作台包”备份数据，并把备份文件放在仓库目录之外。

## 验证

```bash
python -c "from pathlib import Path; compile(Path('app.py').read_text(encoding='utf-8'), 'app.py', 'exec')"
node -e "const fs=require('fs');const h=fs.readFileSync('index.html','utf8');new Function(h.match(/<script>([\s\S]*?)<\/script>/)[1]);"
```

启动后检查：

- `GET /api/status` 返回本地、模型和公众号配置状态。
- 首页、写作台、素材库、草稿箱和数据复盘均能加载。
- 未配置外部服务时不显示伪造的“已连接”或“已生成”结果。

## 项目文档

- [架构说明](docs/architecture.md)
- [本地 API](docs/api-reference.md)
- [集成配置](docs/integration-guide.md)
- [运行手册](docs/operator-runbook.md)
- [维护交接](docs/handoff.md)
- [第三方依赖声明](THIRD_PARTY_NOTICES.md)

## 许可说明

仓库目前未声明开源许可证。第三方可选依赖的许可信息见 `THIRD_PARTY_NOTICES.md`。
