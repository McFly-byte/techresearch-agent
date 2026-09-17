# 任务 3：飞书（Lark）文档 API 接入指南

> 目标：让 DeepResearchAgent 把调研报告自动写成一篇**飞书在线文档（新版文档 docx）**。
> 官方文档站：https://open.feishu.cn/ （国际版 Lark：https://open.larkoffice.com/ ，接口路径一致）
> 调研日期：2026-09-16

---

## 1. 认证模型（先搞懂这张图）

```
你的 Agent（后端）
   │  app_id + app_secret（硬编码到后端环境变量）
   ▼
POST /open-apis/auth/v3/tenant_access_token/internal   ← 自建应用专用
   ▼
tenant_access_token（最长 2 小时，剩余 <30 分钟时再申请会签发新的）
   ▼
Authorization: Bearer <tenant_access_token>
   ▼
POST /open-apis/docx/v1/documents          创建文档
POST /open-apis/docx/v1/documents/{id}/blocks/{block_id}/children   写入内容块
```

- **自建应用（Custom App）**：用 `tenant_access_token`，代表"应用这个机器人"身份操作。**MVP 用这个最简单**，不需要 OAuth 跳转用户授权。
- **用户身份（user_access_token）**：代表某个真人操作，需要走 OAuth 授权码流程。MVP 不用。
- 关键限制：`tenant_access_token` 能操作的范围 = **应用被授予的权限 + 应用被加为协作者/有权限的文件夹**。常见报错 `1770040 no folder permission` 就是没把应用加进目标文件夹。

---

## 2. 关键 API

| 用途 | 方法 & 路径 | 文档 |
|---|---|---|
| 取 tenant_access_token | `POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal` | https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal |
| 创建新版文档 | `POST https://open.feishu.cn/open-apis/docx/v1/documents`，body `{"title": ..., "folder_token": ...}` | https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document/create |
| 批量写块（追加子块） | `POST https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children`，body `{"index": 0, "children": [ ...blocks ]}` | https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/create |
| 列出/读取块 | `GET .../docx/v1/documents/{document_id}/blocks` | 同上父级 |

**关于 block_id**：新建文档后，文档根块（page）的 `block_id` 就等于 `document_id`。所以往文档末尾追加内容时，`{block_id}` 填 `document_id`。

**常见 block_type 枚举**（写内容用）：
| block_type | 含义 | 块字段名 |
|---|---|---|
| 1 | 文档页（page，根） | — |
| 2 | 普通段落 | `paragraph` |
| 3 / 4 / 5 | 一级/二级/三级标题 | `heading1` / `heading2` / `heading3` |
| 12 | 无序列表项 | `bullet` |
| 13 | 有序列表项 | `ordered` |
| 14 | 引用 | `quote` |
| 15 | 代码块 | `code` |

每个块结构示例（二级标题）：
```json
{
  "block_type": 4,
  "heading2": {
    "elements": [{"text_run": {"content": "选型结论"}}]
  }
}
```

---

## 3. 需要开通的权限（Scopes）

在开放平台后台「权限管理」里勾选并**发布版本**后才生效：

| 权限名 | Scope Key | 用途 |
|---|---|---|
| 创建及编辑新版文档（高级） | `docx:document` | 创建 docx + 增删改内容块（推荐直接开这个，包含只读） |
| 查看新版文档 | `docx:document:readonly` | 读文档（可选） |
| 云空间文件管理 | `drive:drive` | 在指定文件夹下创建文档、移动文件（写 folder 时通常需要） |
| （可选）编辑新版文档 | `docx:document:write_only` | 仅写 |

> 文档原文：「开启任一权限即可创建文档」——MVP 最低只要 `docx:document`；要往指定文件夹放文档，再加 `drive:drive` 并把应用加为该文件夹协作者。

---

## 4. Python SDK：lark-oapi

官方 SDK：`pip install lark-oapi`（GitHub：https://github.com/larksuite/oapi-sdk-python ）。
用它就不用自己管 token 缓存和签名，SDK 自动维护 `tenant_access_token`。

```python
import lark_oapi as lark
client = (
    lark.Client.builder()
    .app_id("cli_xxx")
    .app_secret("xxx")
    .log_level(lark.LogLevel.INFO)
    .build()
)
# client.docx.v1.document.create(...)
# client.docx.v1.document_block_children.create(...)
```

---

## 5. 用户必须手动完成的操作清单（一次性）

> 这些 Agent 做不了，必须你本人在浏览器里点。建议截图存档。

1. **创建自建应用**：打开 https://open.feishu.cn/app → 「创建企业自建应用」→ 填名称（如 `DeepResearchAgent`）。
2. **拿凭证**：应用「凭证与基础信息」页复制 **App ID（`cli_` 开头）** 和 **App Secret**，存到后端 `.env`：
   ```
   FEISHU_APP_ID=cli_xxxxxxxx
   FEISHU_APP_SECRET=xxxxxxxx
   ```
3. **开权限**：「权限管理」搜索并勾选 `docx:document`（创建及编辑新版文档）、`drive:drive`（云空间）。
4. **发布应用**：「版本管理与发布」→ 创建版本 → 提交发布（企业自建应用通常需**企业管理员审批**；个人版租户可自审）。
5. **确定目标文件夹 folder_token**：在飞书云文档里新建一个文件夹（如「Agent 调研报告」），打开它，浏览器地址栏：
   `https://xxx.feishu.cn/drive/folder/`**`fldcnXXXXXXXX`**`?xxxx`
   这一段 `fldcn...` 就是 `folder_token`。存到 `.env`：
   ```
   FEISHU_FOLDER_TOKEN=fldcnXXXXXXXX
   ```
6. **把应用加进该文件夹协作者**（关键，否则报 `1770040 no folder permission`）：打开该文件夹 → 「...」→「权限设置/协作者」→ 添加你刚建的应用为**可编辑**协作者。
7. （可选）如果要把文档自动分享给某群/某人，再加 `drive:permission:member` 并用成员权限接口。

> 备注：`tenant_access_token` 代表应用身份。文档创建出来后，**默认只有应用自己有权限**；你本人若打不开，用上面第 6 步的思路，把文档或其所在文件夹共享给你自己（或在代码里调"添加协作者"接口）。

---

## 6. 最小可运行代码骨架（创建文档 + 写入 Markdown 风格内容）

```python
# feishu_writer.py
# 依赖：pip install lark-oapi
# 环境变量：FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_FOLDER_TOKEN
import os
import re
import lark_oapi as lark
from lark_oapi.api.docx.v1 import (
    CreateDocumentRequest, CreateDocumentRequestBody,
    CreateDocumentBlockChildrenRequest, CreateDocumentBlockChildrenRequestBody,
)

# ---------- 0. 客户端 ----------
def _client() -> lark.Client:
    return (
        lark.Client.builder()
        .app_id(os.environ["FEISHU_APP_ID"])
        .app_secret(os.environ["FEISHU_APP_SECRET"])
        .log_level(lark.LogLevel.INFO)
        .build()
    )

# ---------- 1. 创建空文档 ----------
def create_doc(title: str, folder_token: str) -> str:
    """返回 document_id。"""
    req = (
        CreateDocumentRequest.builder()
        .request_body(
            CreateDocumentRequestBody.builder()
            .title(title)
            .folder_token(folder_token)
            .build()
        )
        .build()
    )
    resp = _client().docx.v1.document.create(req)
    if not resp.success():
        raise RuntimeError(f"创建文档失败: {resp.code} {resp.msg}")
    return resp.data.document.document_id

# ---------- 2. 极简 Markdown -> blocks ----------
# 只支持：# 标题 / 普通段落 / - 无序列表 / ``` 代码块
# 复杂 Markdown（表格、加粗、链接）MVP 先不处理，保持报告模板简单。
def _md_to_blocks(md: str) -> list[dict]:
    blocks = []
    in_code = False
    code_buf: list[str] = []

    def flush_code():
        if code_buf:
            blocks.append({
                "block_type": 15,
                "code": {"elements": [{"text_run": {"content": "\n".join(code_buf)}}]},
            })
            code_buf.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.strip().startswith("```"):
            if in_code:
                flush_code(); in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_buf.append(line); continue

        if not line.strip():
            continue
        if line.startswith("### "):
            blocks.append({"block_type": 5, "heading3": {"elements": [{"text_run": {"content": line[4:]}}]}})
        elif line.startswith("## "):
            blocks.append({"block_type": 4, "heading2": {"elements": [{"text_run": {"content": line[3:]}}]}})
        elif line.startswith("# "):
            blocks.append({"block_type": 3, "heading1": {"elements": [{"text_run": {"content": line[2:]}}]}})
        elif line.lstrip().startswith("- "):
            blocks.append({"block_type": 12, "bullet": {"elements": [{"text_run": {"content": line.lstrip()[2:]}}]}})
        else:
            blocks.append({"block_type": 2, "paragraph": {"elements": [{"text_run": {"content": line}}]}})
    flush_code()
    return blocks

# ---------- 3. 批量追加内容 ----------
def append_markdown(doc_id: str, markdown: str, batch: int = 50):
    """飞书单批 children 有数量/长度上限，分批写。block_id 用文档根块(=doc_id)。"""
    client = _client()
    blocks = _md_to_blocks(markdown)
    for i in range(0, len(blocks), batch):
        chunk = blocks[i:i + batch]
        req = (
            CreateDocumentBlockChildrenRequest.builder()
            .document_id(doc_id)
            .block_id(doc_id)           # 根块 == document_id
            .request_body(
                CreateDocumentBlockChildrenRequestBody.builder()
                .index(i)               # 追加到末尾
                .children(chunk)
                .build()
            )
            .build()
        )
        resp = client.docx.v1.document_block_children.create(req)
        if not resp.success():
            raise RuntimeError(f"写块失败: {resp.code} {resp.msg}")

# ---------- 4. 一键：报告 -> 飞书文档 ----------
def publish_report(title: str, markdown: str) -> str:
    doc_id = create_doc(title, os.environ["FEISHU_FOLDER_TOKEN"])
    append_markdown(doc_id, markdown)
    # 文档链接：https://<你的域名>.feishu.cn/docx/<doc_id>
    return f"https://example.feishu.cn/docx/{doc_id}"

if __name__ == "__main__":
    demo = """# 技术选型调研报告
## 背景
对比 LightRAG 与微软 GraphRAG。
## 结论
- LightRAG 轻量、增量建图，MVP 之后首选
- GraphRAG 成本高，留作观察
## 代码
```python
print("hello")
```
"""
    url = publish_report("DeepResearch 选型报告 Demo", demo)
    print("文档已发布:", url)
```

> 注意：上面 `children(chunk)` 直接传 dict 列表；若你的 `lark-oapi` 版本要求强类型对象，把每个 dict 换成 `Block.builder()...build()`（SDK 版本不同构造方式略异，以 `lark_oapi.api.docx.v1` 里的类为准）。首次跑通后用 `client.docx.v1.document.list` 回读验证。

---

## 7. 常见坑（新人必看）

1. **`1770040 no folder permission`**：应用没被加为目标文件夹协作者 → 见第 5 节第 6 步。
2. **`99991663` / token 无效**：App ID/Secret 错，或改完权限**没重新发布应用版本**。
3. **文档创建出来自己打不开**：tenant 身份创建的文档，需要再把自己加为协作者（调 drive 权限接口），或直接在代码里用 `folder_token` 指向你有权限的文件夹。
4. **一批 children 太大报错**：单批建议 ≤50 个块 / 文本总长受控，按第 6 节 `batch` 分批。
5. **`tenant_access_token` 2 小时过期**：**不要自己存 token**，用 `lark-oapi` 的 Client，它自动缓存并刷新。
6. **国际版 Lark**：域名换成 `open.larkoffice.com`，其余接口一致。

---

## 8. 一句话决策

> 用官方 **lark-oapi SDK + tenant_access_token**（自建应用），`docx:document` + `drive:drive` 两个权限，
> `create_doc` 建文档 → `block_children.create` 分批写内容。
> 手动一次性把应用建好、开权限、发布、拿到 `folder_token` 并把应用加进文件夹即可，之后全自动。
