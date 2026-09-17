"""Feishu/Lark export adapter.

Reads app_id / app_secret / folder_token from config. If not configured,
returns `(False, "not_configured")` and does NOT raise.

Real Open API flow (Docx v1):
  1. POST /open-apis/auth/v3/tenant_access_token/internal  -> tenant_access_token
  2. POST /open-apis/docx/v1/documents                       -> create doc (returns document_id)
  3. POST /open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children
     -> append real text blocks (block_type=2 text). We parse the markdown
        into simple text blocks; we do NOT send a fake {"markdown": ...} body.

Every step checks the Feishu ``code`` field (0 = success). Non-zero codes are
returned as stable, redacted reasons (never the secret). The returned URL is
on the user-accessible ``open.feishu.cn/docx/<id>`` domain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx


@dataclass
class FeishuExportResult:
    exported: bool
    reason: str
    doc_url: str = ""


def _markdown_to_text_blocks(markdown: str) -> list[dict]:
    """Turn simple markdown lines into Docx text blocks (block_type=2).

    We only handle plain text paragraphs (one block per non-empty line).
    Headings/lists are flattened to text — this is a minimal smoke export,
    not a full markdown converter.
    """
    blocks: list[dict] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Strip markdown heading markers / bullets for the plain text block.
        cleaned = re.sub(r"^#+\s*", "", stripped)
        cleaned = re.sub(r"^[-*]\s+", "", cleaned)
        cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
        blocks.append(
            {
                "block_type": 2,
                "text": {
                    "elements": [{"text_run": {"content": cleaned}}],
                    "style": {},
                },
            }
        )
    return blocks


class FeishuExporter:
    name = "feishu_httpx"

    def __init__(
        self,
        *,
        app_id: str = "",
        app_secret: str = "",
        folder_token: str = "",
        base_url: str = "https://open.feishu.cn",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._folder_token = folder_token
        self._base_url = base_url.rstrip("/")
        self._client = client

    @property
    def configured(self) -> bool:
        return bool(self._app_id and self._app_secret)

    async def export_markdown(self, *, title: str, markdown: str) -> FeishuExportResult:
        if not self.configured:
            return FeishuExportResult(exported=False, reason="not_configured")

        close_client = False
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=15.0)
            close_client = True

        try:
            # 1. tenant access token.
            token_resp = await client.post(
                f"{self._base_url}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            if token_resp.status_code >= 400:
                return FeishuExportResult(
                    exported=False, reason=f"auth_error_http_{token_resp.status_code}"
                )
            token_data = token_resp.json()
            if token_data.get("code", 0) != 0:
                return FeishuExportResult(
                    exported=False, reason=f"auth_code_{token_data.get('code')}"
                )
            token = token_data.get("tenant_access_token", "")
            if not token:
                return FeishuExportResult(exported=False, reason="no_token")

            headers = {"Authorization": f"Bearer {token}"}

            # 2. create document.
            create_resp = await client.post(
                f"{self._base_url}/open-apis/docx/v1/documents",
                headers=headers,
                json={"title": title, "folder_token": self._folder_token},
            )
            if create_resp.status_code >= 400:
                # Try to surface the Feishu JSON error code for a precise reason.
                feishu_code: int | None = None
                try:
                    err_body = create_resp.json()
                    feishu_code = err_body.get("code")
                except Exception:  # noqa: BLE001 - non-JSON body
                    pass
                if create_resp.status_code == 403:
                    if feishu_code is not None:
                        # Known Feishu error code (e.g. 99991672 = scope/folder
                        # permission denied).
                        return FeishuExportResult(
                            exported=False,
                            reason=f"http_403_code_{feishu_code}_scope_denied",
                        )
                    return FeishuExportResult(
                        exported=False,
                        reason="http_403_unknown",
                    )
                return FeishuExportResult(
                    exported=False, reason=f"create_http_{create_resp.status_code}"
                )
            create_data = create_resp.json()
            if create_data.get("code", 0) != 0:
                return FeishuExportResult(
                    exported=False, reason=f"create_code_{create_data.get('code')}"
                )
            doc_data = create_data.get("data", {}).get("document", {})
            doc_id = doc_data.get("document_id", "")
            if not doc_id:
                return FeishuExportResult(exported=False, reason="no_doc_id")

            # 3. append real text blocks (Docx children API).
            blocks = _markdown_to_text_blocks(markdown)
            if blocks:
                append_resp = await client.post(
                    f"{self._base_url}/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                    headers=headers,
                    json={"children": blocks},
                )
                if append_resp.status_code >= 400:
                    return FeishuExportResult(
                        exported=False,
                        reason=f"blocks_http_{append_resp.status_code}",
                        doc_url=f"https://{self._base_url.removeprefix('https://')}/docx/{doc_id}",
                    )
                append_data = append_resp.json()
                if append_data.get("code", 0) != 0:
                    return FeishuExportResult(
                        exported=False,
                        reason=f"blocks_code_{append_data.get('code')}",
                        doc_url=f"https://{self._base_url.removeprefix('https://')}/docx/{doc_id}",
                    )

            # User-accessible URL on the proper domain.
            host = self._base_url.removeprefix("https://").removeprefix("http://")
            url = f"https://{host}/docx/{doc_id}"
            return FeishuExportResult(exported=True, reason="ok", doc_url=url)
        except httpx.HTTPError as e:
            # Export failure must NOT break the local report.
            return FeishuExportResult(exported=False, reason=f"http_error:{type(e).__name__}")
        finally:
            if close_client:
                await client.aclose()


__all__ = ["FeishuExportResult", "FeishuExporter"]
