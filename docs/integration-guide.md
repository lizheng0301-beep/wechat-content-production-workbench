# 集成配置

所有配置在启动进程前通过环境变量传入。程序不会自动读取仓库中的 `.env`，也不会把凭据写入数据库或备份。

## 推荐组合

当前演示组合是 DeepSeek 文本模型、RightCode 图片模型和微信公众号。

### DeepSeek

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat
```

### RightCode 图片

```text
RIGHTCODE_IMAGE_API_KEY=
RIGHTCODE_IMAGE_BASE_URL=https://www.rightapi.ai/draw
RIGHTCODE_TASK_BASE_URL=https://www.rightapi.ai
RIGHTCODE_IMAGE_MODEL=gpt-image-2
RIGHTCODE_IMAGE_SIZE=16:9
RIGHTCODE_IMAGE_RESOLUTION=1K
RIGHTCODE_IMAGE_TIMEOUT=150
RIGHTCODE_SUBMIT_RETRIES=3
RIGHTCODE_RETRY_DELAY=2
```

正文图固定优先使用 `4:3`，`RIGHTCODE_IMAGE_SIZE` 主要控制封面。图片请求是异步任务：先提交，再轮询 `task_id`，因此通常比文本生成慢。

请使用 `RIGHTCODE_IMAGE_API_KEY`。如果设置共用变量 `RIGHTCODE_API_KEY`，RightCode 也会进入文本候选并排在 DeepSeek 前面。

### 微信公众号

```text
WECHAT_APP_ID=
WECHAT_APP_SECRET=
WECHAT_AUTHOR=
```

还要在公众号后台把运行电脑的公网出口 IPv4 加入 IP 白名单。本机地址 `127.0.0.1` 不是微信看到的出口 IP。网络出口变化后需要更新白名单，但 API Key 和 AppSecret 不需要因此重填。

## 文本提供商优先级

当多个文本 Key 同时存在时，候选顺序是：

1. RightCode：`RIGHTCODE_API_KEY` 或 `RIGHT_CODE_API_KEY`
2. YuzAPI：`YUZAPI_API_KEY` 或 `YUZ_API_KEY`
3. OpenAI 兼容：`OPENAI_API_KEY`
4. DeepSeek：`DEEPSEEK_API_KEY`

只在连接错误、超时或 5xx 等临时故障时尝试一个备用候选。界面和草稿记录显示实际使用的提供商，不把备用结果伪装成首选模型。

若目标是 DeepSeek 写作加 RightCode 生图，只配置 `DEEPSEEK_API_KEY` 和 `RIGHTCODE_IMAGE_API_KEY`。

## 其他文本配置

```text
OPENAI_API_KEY
OPENAI_BASE_URL
OPENAI_MODEL

YUZAPI_API_KEY
YUZAPI_BASE_URL
YUZAPI_MODEL
YUZAPI_JSON_MODE
YUZAPI_OMIT_TEMPERATURE
YUZAPI_FALLBACK_ENABLED

RIGHTCODE_TEXT_BASE_URL
RIGHTCODE_TEXT_MODEL
RIGHTCODE_MODEL
RIGHTCODE_TEXT_TIMEOUT
RIGHTCODE_JSON_MODE

TEXT_MODEL_TIMEOUT
TEXT_MODEL_FALLBACK_TIMEOUT
```

## OpenAI 兼容图片配置

```text
OPENAI_IMAGE_API_KEY
OPENAI_IMAGE_BASE_URL
OPENAI_IMAGE_MODEL
```

接口必须兼容 `POST /images/generations`，返回 `data[0].b64_json` 或 `data[0].url`。仅有文本兼容不代表图片接口也兼容。

## 启动与检查

PowerShell 示例：

```powershell
$env:DEEPSEEK_API_KEY = "..."
$env:RIGHTCODE_IMAGE_API_KEY = "..."
$env:WECHAT_APP_ID = "..."
$env:WECHAT_APP_SECRET = "..."
python app.py
```

打开状态面板或请求：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/status
```

检查 `text_configured`、`image_configured` 和微信公众号 `configured`。不要在排障截图、终端输出或 Git diff 中暴露真实凭据。

## 常见错误

| 现象 | 原因与处理 |
| --- | --- |
| 文本提供商显示 RightCode | 配置了共用的 `RIGHTCODE_API_KEY`；改用图片专用变量 |
| 图片生成很慢 | 异步任务要排队和轮询；先观察任务提示，避免重复点击 |
| 微信返回 `40164` | 当前公网出口 IPv4 不在白名单 |
| 微信返回 `48001` | 当前账号没有图文数据分析权限；草稿不受影响，可登记后台真实数据 |
| 微信拒绝图片格式 | 安装 Pillow 后重启，服务会将不稳定格式转换为 JPEG |
| 修改配置后仍显示旧状态 | 必须重启 Python 服务，浏览器刷新不能重载环境变量 |
