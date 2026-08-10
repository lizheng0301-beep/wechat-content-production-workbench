# 本地 API

所有路由只服务于本机工作台。默认基地址为 `http://127.0.0.1:8765`。

## GET

| 路径 | 用途 |
| --- | --- |
| `/` | 返回工作台单页 |
| `/api/dashboard` | 总览计数和集成状态 |
| `/api/hot` | 本地热点缓存 |
| `/api/topics` | 选题列表 |
| `/api/drafts` | 草稿及最近微信草稿任务 |
| `/api/drafts/{id}/auto-images/{job_id}` | 自动配图任务状态 |
| `/api/drafts/{id}/markdown` | 下载 Markdown |
| `/api/assets` | 素材列表 |
| `/api/metrics` | 微信 API 来源的每日指标 |
| `/api/metrics/summary?days=N` | 1 至 30 天指标汇总 |
| `/api/published-articles` | 已登记发布文章 |
| `/api/style` | 风格档案状态 |
| `/api/status` | 本地和外部集成状态 |
| `/api/wechat/drafts` | 微信草稿列表 |
| `/api/export` | JSON 备份 |
| `/api/export/package` | ZIP 工作台包 |
| `/media?path=...` | 读取已登记本地素材 |

## POST

| 路径 | 用途 |
| --- | --- |
| `/api/import` | 恢复 JSON 或 ZIP 备份 |
| `/api/hot/sync` | 同步 AI HOT |
| `/api/style` | 保存个人风格偏好 |
| `/api/topics` | 创建选题和关联草稿 |
| `/api/drafts/blank` | 创建空白草稿 |
| `/api/topics/{id}/draft` | 为选题创建新草稿 |
| `/api/drafts/{id}/generate` | 生成结构化长文 |
| `/api/drafts/{id}/quality` | 执行本地规则质检 |
| `/api/drafts/{id}/save` | 保存草稿 |
| `/api/drafts/{id}/readability` | 整理排版标记 |
| `/api/drafts/{id}/auto-images` | 启动自动配图任务 |
| `/api/assets/import-url` | 从网页导入候选图片 |
| `/api/assets/prompt-from-selection` | 将选中段落编译为图片 Prompt |
| `/api/assets/generate-from-prompt` | 生成图片或本地信息卡 |
| `/api/assets/generate-image` | 生成原创图片 |
| `/api/assets` | 登记已有本地图片 |
| `/api/wechat/preview` | 生成最终微信渲染预览 |
| `/api/wechat/test` | 验证 access token |
| `/api/published-articles` | 登记正式发布文章 |
| `/api/metrics/manual` | 登记后台真实指标 |
| `/api/drafts/{id}/publish` | 创建微信草稿，不群发 |
| `/api/metrics/sync` | 按天同步近 7 个完整自然日内的微信指标 |

错误响应会返回 `error` 字段。服务端会尝试从异常消息中移除当前环境里的 Key、AppID 和 AppSecret。
