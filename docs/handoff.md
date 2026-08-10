# 维护交接

更新时间：2026-08-11

## 运行入口

- 服务入口：`app.py`
- 前端入口：`index.html`
- 默认地址：`http://127.0.0.1:8765/`
- 本地数据：`.workbench/workbench.sqlite3`
- 本地素材：`.workbench/assets/`

## 当前验证主路径

- 文本：DeepSeek `deepseek-chat`
- 图片：RightCode `gpt-image-2` 异步任务
- 发布链路：微信公众号图片上传和草稿创建
- 存储：SQLite 和本地素材目录
- 数据复盘：微信 API 按天同步，或公众号后台真实数据手工登记

## 关键不变量

- 不把凭据、SQLite、用户文章、素材和日志提交到 Git。
- 不把“创建公众号草稿”描述成正式发布或群发。
- 不把固定工作流描述成多 Agent。
- 不使用演示指标冒充公众号真实数据。
- 排版模型不能改写正文；校验不通过必须使用本地排版规则。
- 预览和微信草稿共用同一渲染链，但不承诺微信客户端绝对像素一致。
- DeepSeek 文本配 RightCode 图片时使用 `RIGHTCODE_IMAGE_API_KEY`，避免改变文本优先级。

## 已知限制

- 单用户、本地服务，没有认证和多租户。
- 自动配图任务只存在于当前 Python 进程。
- 微信数据分析权限取决于账号类型和平台实际授权。
- 正式发布文章需要人工登记，标题和日期匹配不是稳定的微信文章 ID 关联。
- 规则质检不进行外部事实校验。
- 来源图片版权状态需要人工确认。
- 根目录 `build_wechat_import_docx.py` 和 `fontconfig-codex.conf` 已移除：它们绑定旧文章、旧 Mac 路径，不属于当前产品运行链路。

## 修改检查

```bash
python -c "from pathlib import Path; compile(Path('app.py').read_text(encoding='utf-8'), 'app.py', 'exec')"
node -e "const fs=require('fs');const h=fs.readFileSync('index.html','utf8');new Function(h.match(/<script>([\s\S]*?)<\/script>/)[1]);"
git diff --check
git status --short
```

提交前检查 `git diff --cached`，重点确认没有 `API_KEY`、`APP_SECRET`、access token、`.workbench` 或本地文章内容。
