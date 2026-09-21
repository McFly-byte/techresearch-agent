# 用户手动操作清单（Manual Setup）

> 阶段 0 **不需要**做任何事就能跑测试、lint、type check、`tra doctor`、
> `/api/health` 和前端构建。
> 下面每一项都是**可选**的，对应后续阶段才会用到的外部服务。
> 所有 Key 都填进 `.env`（不要提交到 Git）。

---

## 1. 阿里云百炼 / DashScope（阶段 1 起需要）

**何时需要**：第一次想让 Agent 调用真实 LLM（qwen3.8-max / qwen3.8-flash）时。

步骤：
1. 打开 <https://bailian.console.aliyun.com/> ，登录/注册。
2. 在"模型服务"里确认 `qwen3.8-max` 已开通（新用户可能有免费 token 包，
   具体数量以"费用中心 → 额度管理"实际显示为准——见
   `research/04_fact_audit/qwen_api_audit.md` §6）。
3. 进入"API-KEY 管理"，创建一个 Key（`sk-...` 格式）。
4. 编辑 `.env`：
   ```
   DASHSCOPE_API_KEY=sk-你的真实Key
   ```
5. 可选：把 `QWEN_BASE_URL` 改成业务空间专属域名
   `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`
   （旧域名 `https://dashscope.aliyuncs.com/compatible-mode/v1` 仍可用）。

**审计结论提醒**（来自 `research/04_fact_audit/qwen_api_audit.md`）：
- qwen3.8 系列默认 `preserve_thinking=true`，多轮对话必须把历史中的
  `reasoning_content` 原样回传——这是阶段 1 接 LangChain 时的坑。
- `qwen3.8-max` 输入 ¥12 / 输出 ¥36 每百万 token（原价）。

## 2. LangSmith（阶段 1+ 可观测性）

**何时需要**：想在 LangSmith UI 里看到 trace 时。

步骤：
1. 打开 <https://smith.langchain.com/> ，用 Google/GitHub/邮箱注册（**无需信用卡**）。
2. 创建项目，名称建议 `tech-research-agent`。
3. Settings → API Keys → Create API Key（`lsv2_...` 格式）。
4. 编辑 `.env`：
   ```
   LANGCHAIN_TRACING_V2=true
   LANGCHAIN_API_KEY=lsv2_你的Key
   LANGCHAIN_PROJECT=tech-research-agent
   ```

**审计结论提醒**：免费版 5,000 traces/月、base trace 保留 14 天；不绑卡天然封顶 5k。
环境变量新旧两套命名（`LANGSMITH_*` / `LANGCHAIN_*`）当前都生效，本项目沿用设计文档
的 `LANGCHAIN_*` 命名。

## 3. Tavily Web 搜索（阶段 1+）

**何时需要**：Worker 开始联网搜索时。

步骤：
1. 打开 <https://tavily.com/> 注册（免费 1,000 次/月）。
2. Dashboard 复制 Key（`tvly-...` 格式）。
3. 编辑 `.env`：
   ```
   TAVILY_API_KEY=tvly-你的Key
   ```
   有多个 Key 时可额外配置逗号分隔的池：
   ```
   TAVILY_API_KEYS=tvly-Key1,tvly-Key2,tvly-Key3
   ```
   系统会去重后轮询使用；额度耗尽或认证失败的 Key 会在当前进程中自动熔断，
   429、5xx 和超时会有限重试并切换到下一个 Key。

## 4. 飞书自建应用（阶段 5+ 报告导出）

**何时需要**：要把 Markdown 报告导出成飞书在线文档时。

步骤：
1. 打开 <https://open.feishu.cn/> → 创建企业自建应用。
2. 在"凭证与基础信息"页拿到 `App ID`（`cli_...`）和 `App Secret`。
3. 在"权限管理"开通：`docx:document`、`drive:drive`。
4. 创建版本并发布（可能需要管理员审批）。
5. 在目标文件夹 URL 里提取 `folder_token`（形如
   `https://xxx.feishu.cn/drive/folder/{folder_token}`）。
6. 使用 `tenant_access_token` 让应用访问“我的空间”文件夹时，**不要直接把应用添加为文件夹协作者**，飞书官方说明这种方式对文件夹不会真正授权。正确方式：
   - 确认应用已启用机器人能力并发布版本；
   - 新建群聊，将应用添加为群机器人（不是自定义机器人）；
   - 将目标文件夹分享给该群聊，并授予可管理权限。
   否则创建文档会返回 `1770040 no folder permission`。
7. 编辑 `.env`：
   ```
   FEISHU_APP_ID=cli_xxx
   FEISHU_APP_SECRET=xxx
   FEISHU_FOLDER_TOKEN=xxx
   ```

## 5. 不要做的事

- 不要把 `.env` 提交到 Git（已在 `.gitignore` 里）。
- 不要把真实 Key 粘贴进 `research/`、`docs/`、测试或截图。
- 不要在没填 `DASHSCOPE_API_KEY` 时强行把 `LLM_PROVIDER=qwen`——
  `tra doctor` 和工厂会直接报错，而不是悄悄降级。
