"""BM25-backed discovery for dynamically registered MCP tools."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable

from mini_openharness.tools.base import ToolContext, ToolDescriptor, ToolRegistry, ToolResult


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    """Tokenize text while retaining snake_case terms and their parts."""
    result: list[str] = []
    for token in _TOKEN_RE.findall(text.lower()):
        result.append(token)
        if "_" in token:
            result.extend(part for part in token.split("_") if part)
    return result


def schema_search_text(schema: dict[str, Any]) -> str:
    """Extract useful searchable content from a JSON Schema."""
    parts: list[str] = []

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for key in ("description", "title"):
            value = node.get(key)
            if isinstance(value, str):
                parts.append(value)
        enum = node.get("enum")
        if isinstance(enum, list):
            parts.extend(str(item) for item in enum)
        properties = node.get("properties")
        if isinstance(properties, dict):
            for name, child in properties.items():
                parts.append(str(name))
                visit(child)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items)

    visit(schema)
    return " ".join(parts)


def tool_name_search_text(tool: Any) -> str:
    return str(tool.name)


def tool_content_search_text(tool: Any) -> str:
    return " ".join(
        (
            str(getattr(tool, "description", "") or ""),
            schema_search_text(getattr(tool, "parameters", {}) or {}),
        )
    )


@dataclass(frozen=True)
class SearchHit:
    name: str
    score: float
    tool: Any


class BM25ToolSearch:
    """Small BM25Okapi index suitable for a few thousand tools."""

    def __init__(
        self,
        tools: Iterable[Any] = (),
        *,
        k1: float = 1.5,
        b: float = 0.75,
        name_weight: float = 3.0,
        content_weight: float = 1.0,
    ) -> None:
        if name_weight < 0 or content_weight < 0:
            raise ValueError("BM25 field weights must be non-negative")
        if name_weight == 0 and content_weight == 0:
            raise ValueError("at least one BM25 field weight must be positive")
        self.k1 = k1
        self.b = b
        self.name_weight = name_weight
        self.content_weight = content_weight
        self._tools: list[Any] = []
        self._name_documents: list[list[str]] = []
        self._content_documents: list[list[str]] = []
        self._name_term_freqs: list[Counter[str]] = []
        self._content_term_freqs: list[Counter[str]] = []
        self._name_doc_freq: Counter[str] = Counter()
        self._content_doc_freq: Counter[str] = Counter()
        self._name_avgdl = 0.0
        self._content_avgdl = 0.0
        self.rebuild(tools)

    def rebuild(self, tools: Iterable[Any]) -> None:
        self._tools = list(tools)
        self._name_documents = [
            tokenize(tool_name_search_text(tool)) for tool in self._tools
        ]
        self._content_documents = [
            tokenize(tool_content_search_text(tool)) for tool in self._tools
        ]
        self._name_term_freqs = [Counter(document) for document in self._name_documents]
        self._content_term_freqs = [
            Counter(document) for document in self._content_documents
        ]
        self._name_doc_freq = self._document_frequency(self._name_documents)
        self._content_doc_freq = self._document_frequency(self._content_documents)
        self._name_avgdl = self._average_document_length(self._name_documents)
        self._content_avgdl = self._average_document_length(self._content_documents)

    @staticmethod
    def _document_frequency(documents: list[list[str]]) -> Counter[str]:
        frequencies: Counter[str] = Counter()
        for document in documents:
            for term in set(document):
                frequencies[term] += 1
        return frequencies

    @staticmethod
    def _average_document_length(documents: list[list[str]]) -> float:
        if not documents:
            return 0.0
        return sum(len(document) for document in documents) / len(documents)

    def search(self, query: str, *, top_k: int = 5) -> list[SearchHit]:
        query_terms = tokenize(query)
        if not query_terms or not self._tools:
            return []

        hits: list[SearchHit] = []
        for tool, name_document, content_document, name_term_freq, content_term_freq in zip(
            self._tools,
            self._name_documents,
            self._content_documents,
            self._name_term_freqs,
            self._content_term_freqs,
        ):
            name_score = self._score_field(
                query_terms,
                name_document,
                name_term_freq,
                self._name_doc_freq,
                self._name_avgdl,
            )
            content_score = self._score_field(
                query_terms,
                content_document,
                content_term_freq,
                self._content_doc_freq,
                self._content_avgdl,
            )
            score = self.name_weight * name_score + self.content_weight * content_score
            if score > 0:
                hits.append(SearchHit(tool.name, score, tool))
        hits.sort(key=lambda hit: (-hit.score, hit.name))
        return hits[:top_k]

    def _score_field(
        self,
        query_terms: list[str],
        document: list[str],
        term_freq: Counter[str],
        document_frequency: Counter[str],
        average_document_length: float,
    ) -> float:
        n_docs = len(self._tools)
        doc_len = len(document)
        score = 0.0
        for term in query_terms:
            frequency = term_freq.get(term, 0)
            if frequency == 0:
                continue
            term_document_frequency = document_frequency[term]
            idf = math.log(
                1
                + (n_docs - term_document_frequency + 0.5)
                / (term_document_frequency + 0.5)
            )
            normalization = frequency + self.k1 * (
                1 - self.b + self.b * doc_len / max(average_document_length, 1.0)
            )
            score += idf * frequency * (self.k1 + 1) / normalization
        return score


class ToolSearchTool:
    name = "tool_search"
    description = (
        "Search for relevant MCP tools using BM25 over tool names, descriptions, "
        "parameter names, parameter descriptions, titles, and enum values."
    )
    descriptor = ToolDescriptor(source="local", effect="read")
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "description": "Natural-language capability query for the required MCP tool.",
            },
            "top_k": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 5,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        top_k = arguments.get("top_k", 5)
        mcp_tools = [
            tool
            for name, tool in self.registry.items()
            if self.registry.descriptor(name).source == "mcp"
        ]
        hits = BM25ToolSearch(mcp_tools).search(arguments["query"], top_k=top_k)
        matches = [
            {
                "name": hit.name,
                "description": hit.tool.description,
                "score": hit.score,
            }
            for hit in hits
        ]
        return ToolResult(
            output=json.dumps(matches, ensure_ascii=False),
            metadata={"matched_tools": [hit.name for hit in hits]},
        )
