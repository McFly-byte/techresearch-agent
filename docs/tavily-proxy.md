# Tavily 本地 Key 池代理

项目通过 Git 子模块固定第三方 `tavily_search_sub_api`，由它统一承担 Key
选择、单 Key 并发、额度同步、冷却和隔离；主项目只面向兼容的 `/search` 接口。

## 初始化与启动

```powershell
git submodule update --init --recursive
.\scripts\tavily-proxy.ps1 start
```

启动封装会把 `.env` 中 `TAVILY_API_KEY` / `TAVILY_API_KEYS` 原子同步到子模块内
被忽略的 `key.txt`，不会打印 Key；然后固定以如下安全配置启动：

- `HOST=127.0.0.1`
- `WORKERS=1`
- `TAVILY_MAX_CONCURRENT_SEARCHES_PER_KEY=1`
- 复用主项目 `.venv`

在 `.env` 中启用主项目适配器：

```dotenv
TAVILY_PROXY_URL=http://127.0.0.1:15280
TAVILY_PROXY_ERROR_FILE=vendor/tavily_search_sub_api/error_key.txt
```

查看状态和停止服务：

```powershell
.\scripts\tavily-proxy.ps1 status
.\scripts\tavily-proxy.ps1 stop
```

## 错误契约

主项目不会判断 Key 来源或替用户管理 Key 有效性，只将代理状态转换成稳定错误：

- 所有上游 Key 鉴权失败：`ToolAuthenticationError`
- 所有上游 Key 额度耗尽：`ToolQuotaExceededError`
- 所有 Key 冷却、繁忙或上游网络不可用：`TransientToolError`
- 混合原因或诊断接口不可用：`ToolError`，消息包含脱敏错误码

当连续失败的 Key 已从活动池移入 `error_key.txt`、导致 `/accounts` 为空时，适配器
只读取隔离记录的 `reason` 字段继续分类；不会读取、记录或返回其中的 `api_key`。

第三方服务不校验调用方 Authorization，并提供可写入 Key 的管理接口，因此只能监听
本机回环地址。不要把端口映射到公网，也不要提交 `key.txt`、`error_key.txt` 或
`runtime/`。

## 升级第三方版本

```powershell
git -C vendor/tavily_search_sub_api fetch origin
git -C vendor/tavily_search_sub_api checkout <reviewed-commit>
git add vendor/tavily_search_sub_api
```

升级前应重新审查路由鉴权、密钥落盘位置和错误响应格式。第三方仓库当前未提供显式
LICENSE，因此保留为独立子模块，不复制进本项目 MIT 源码。
