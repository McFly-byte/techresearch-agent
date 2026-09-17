"""Prompt Hub data models: PromptSpec + RenderedPrompt.

A *PromptSpec* is the static, auditable definition of one runtime LLM prompt:
its logical name, its local ChatPromptTemplate (as messages), its required
variables, and a version tag. A *RenderedPrompt* is the result of rendering a
spec for one call: the concrete messages, plus provenance (which source the
template came from, the commit/tag pin, and the variable NAMES actually
passed — never their values).

Security contract (see docs/prompt-hub.md):
- The template stored in a spec contains ONLY template text. No document
  bodies, no API keys, no user-supplied secrets.
- RenderedPrompt.variables records the NAMES of the variables used so a trace
  can show "this call rendered fact_extraction_system with variables
  [user_context, source_docs]" WITHOUT echoing the values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from langchain_core.prompts import ChatPromptTemplate

PromptSource = Literal["local", "langsmith", "local_fallback", "hybrid"]


@dataclass(frozen=True)
class PromptSpec:
    """Static definition of one runtime prompt.

    Attributes:
        name: logical name (e.g. ``fact_extraction_system``).
        description: human-readable summary.
        version: semver-ish string, bumped on every template edit.
        messages: ordered list of ``(role, template)`` pairs. ``role`` is one
            of ``system`` / ``user`` / ``assistant``. ``template`` may contain
            ``{var}`` placeholders.
        variables: required variable names (derived from the templates).
    """

    name: str
    description: str
    version: str
    messages: tuple[tuple[str, str], ...]
    variables: tuple[str, ...] = field(default_factory=tuple)

    def to_chat_prompt_template(self) -> ChatPromptTemplate:
        """Build a LangChain ChatPromptTemplate from this spec.

        This is the object pushed to LangSmith (official recommended shape)
        and rendered locally. It contains ONLY template text — never document
        bodies or variable values.
        """
        role_map: dict[str, str] = {
            "system": "system",
            "user": "human",
            "assistant": "ai",
        }
        lc_messages: list[tuple[str, str]] = [
            (role_map[role], template) for role, template in self.messages
        ]
        return ChatPromptTemplate.from_messages(lc_messages)

    @classmethod
    def from_chat_prompt_template(
        cls,
        name: str,
        template: ChatPromptTemplate | object | str,
        *,
        description: str = "",
        version: str = "1.0.0",
    ) -> PromptSpec:
        """Build a PromptSpec from a ChatPromptTemplate (or plain string).

        Used when pulling a prompt back from LangSmith: we normalize the remote
        object into our local spec shape so manifest validation stays uniform.
        """
        if isinstance(template, str):
            messages = (("system", template),)
            variables = tuple(sorted(_extract_vars(template)))
            return cls(
                name=name,
                description=description,
                version=version,
                messages=messages,
                variables=variables,
            )
        # ChatPromptTemplate: extract each message's template string.
        out: list[tuple[str, str]] = []
        var_set: set[str] = set()
        if not isinstance(template, ChatPromptTemplate):
            # Defensive: pull off a .prompt attribute if present (LangSmith
            # Prompt wrapper) or fall back to treating it as a single system msg.
            inner = getattr(template, "prompt", template)
            if not isinstance(inner, ChatPromptTemplate):
                text = str(inner)
                return cls(
                    name=name,
                    description=description,
                    version=version,
                    messages=(("system", text),),
                    variables=tuple(sorted(_extract_vars(text))),
                )
            template = inner
        for msg in template.messages:
            # The role lives on the message wrapper type, not on .prompt.
            msg_type = type(msg).__name__
            if "System" in msg_type:
                role = "system"
            elif "Human" in msg_type:
                role = "user"
            elif "AIMessage" in msg_type:
                role = "assistant"
            else:
                role = "user"
            inner_prompt = getattr(msg, "prompt", None)
            tmpl = getattr(inner_prompt, "template", "") or ""
            out.append((role, tmpl))
            var_set.update(_extract_vars(tmpl))
        return cls(
            name=name,
            description=description,
            version=version,
            messages=tuple(out),
            variables=tuple(sorted(var_set)),
        )


@dataclass(frozen=True)
class RenderedPrompt:
    """One rendered prompt ready to send to an LLM, with provenance.

    Attributes:
        name: logical prompt name.
        source: where the template came from (local / langsmith /
            local_fallback).
        identifier: LangSmith identifier or local name.
        commit: commit hash pinned (if any).
        tag: tag pinned (if any).
        variables: NAMES of the variables actually supplied to render.
        messages: rendered ``(role, content)`` list — content is the CONCRETE
            text that will be sent. This DOES contain variable values; callers
            must NOT log these.
    """

    name: str
    source: PromptSource
    identifier: str
    commit: str | None
    tag: str | None
    variables: tuple[str, ...]
    messages: tuple[tuple[str, str], ...]

    def system_text(self) -> str:
        """Convenience: the first system message content, or ''."""
        for role, content in self.messages:
            if role == "system":
                return content
        return ""


def _extract_vars(template: str) -> set[str]:
    """Extract ``{var}`` names from a template string (str.format style)."""
    import string

    names: set[str] = set()
    for _literal, field_name, _fmt, _conv in string.Formatter().parse(template):
        if field_name:
            names.add(field_name)
    return names


__all__ = ["PromptSpec", "PromptSource", "RenderedPrompt"]
