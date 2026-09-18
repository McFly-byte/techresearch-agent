# Agent 接手指南

本页给未来接手项目的 Agent 使用，目标是减少重复探查、误用旧文档和覆盖他人改动。

## 开工前必做

1. 读取根 `README.md` 和本目录 `README.md`。
2. 执行 `git status --short`、`git branch --show-current`、`git remote -v`。
3. 记录已有未提交文件，禁止覆盖或混入提交。
4. 以 `src/`、`web/src/`、`evals/` 和 `tests/` 为事实来源。
5. 对 `docs/技术设计文档.md`、`docs/implementation-progress.md` 只作历史参考，不直接当当前实现。
6. 修改前先查对应测试和配置。

## 推荐探查顺序

### 后端主链路

```text
src/core/config.py
→ src/api/main.py
→ src/api/runner.py
→ src/graph/state.py
→ src/graph/builder.py
→ src/agents/planner.py
→ src/agents/supervisor.py
→ src/agents/worker.py
→ src/service/verified_report.py
→ src/service/verifier.py
```

### 前端

```text
web/package.json
→ web/src/App.tsx
→ web/src/*.test.tsx
→ web/e2e/app.spec.ts
```

### 评测

```text
evals/README.md
→ evals/adapter.py
→ evals/configs.py
→ evals/baselines.py
→ evals/metrics.py
→ evals/runner.py
→ evals/cli.py
```

## 决策规则

- 看到注释写“Phase N”时，不要据此判断功能是否完成，继续查看实际调用路径。
- 看到类已实现时，确认它是否接入 Runner/API；“存在”不等于“生效”。
- 看到测试通过记录时，不要沿用旧数字；重新运行相关命令。
- 看到 live 适配器时，先判断当前入口是否真的能选择 live。
- 看到结果文件时，检查其 commit、数据哈希、模型和 judge，不默认有效。
- 需要提交时只 stage 本次改动的文件。

## 修改工作流

```text
确认需求与边界
→ 定位源码和测试
→ 写最小失败测试
→ 修改实现
→ 跑相关测试
→ 跑全量质量门禁
→ 回读用户会看到的结果
→ 更新知识库/README
→ 检查 diff
→ 精确提交
```

## 常见陷阱

1. API 默认 fake，不能因为 `.env` 有 Key 就声称 Web 在真实联网。
2. `Settings` 有缓存，测试改环境变量要清缓存。
3. `Send` 节点收到的是 payload，不自动获得完整共享 state。
4. Reducer 决定并发正确性，不能随意改成“后到覆盖”。
5. Worker 的预算票据在异常和取消路径必须释放。
6. CitationVerifier 的 NLI 失败不能转成 neutral。
7. TaskStore JSON 模式发生变化时要考虑旧文件兼容。
8. 前端不能用 `dangerouslySetInnerHTML` 显示模型报告。
9. fake fixture、mock transport 和真实 smoke 是三种不同证据。
10. 仓库可能已有其他人的未提交改动。

## 提交规范建议

- 一次提交聚焦一个主题。
- 文档提交示例：`docs: 建立中文项目知识库`。
- 代码和文档可在同一提交中，但必须围绕同一行为变化。
- 提交前运行 `git diff --check`。
- 推送前确认远端和分支，避免强推。

## 完成标准

- 实现与测试一致。
- 用户入口可用。
- 错误路径有明确结果。
- 相关知识库已更新。
- 没有提交 `.env`、运行大文件、密钥或其他人的工作区改动。
- 最终说明列出验证命令、结果和仍未解决的限制。
