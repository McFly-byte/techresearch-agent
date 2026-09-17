# Fixture: fake-mode E2E output for "LangGraph vs LlamaIndex"

This file is a recorded snapshot of `tra research "LangGraph vs LlamaIndex"`
running entirely against fake providers (FakeSearchProvider + FakeFetcher +
HeuristicFactExtractor + MarkdownReportWriter). It is test data, not a real
literature review.

```
# 调研报告：LangGraph vs LlamaIndex

- 事实条数：6
- 引用来源数：2

## 要点

- LangGraph models agents as a graph of nodes. [c1]
- It provides typed state, checkpoints and human-in-the-loop. [c1]
- It is maintained by LangChain. [c1]
- LlamaIndex focuses on indexing and retrieval over external data. [c2]
- It offers high-level agents but less control over state machines. [c2]
- It is maintained by LlamaIndex Inc. [c2]

## 引用

- [c1] https://example.com/langgraph — https://example.com/langgraph
- [c2] https://example.com/llamaindex — https://example.com/llamaindex
```

Notes:
- Both URLs are `example.com`, reserved by IANA for documentation.
- Every `[cN]` tag maps to a line in the "## 引用" section with a locator.
- The fact extractor copies sentences verbatim from the fake page content.
