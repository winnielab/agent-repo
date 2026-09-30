"""Modularized knowledge component of the PR review agent."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .domain import Transcript
from .settings import MAX_COMPANY_CONTEXT_CHARS
from .utils import truncate


@dataclass(frozen=True)
class CompanyKnowledgeChunk:
    policy_id: str
    doc_path: str
    heading: str
    text: str
    category: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CompanyKnowledgeBase:
    def __init__(
        self,
        knowledge_dir: Path,
        index_path: Path,
        transcript: Transcript,
        api_key: str | None = None,
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_model: str = "text-embedding-v4",
        embedding_dimensions: int = 1024,
        enabled: bool = True,
        timeout: int = 20,
    ):
        self.knowledge_dir = knowledge_dir.resolve()
        self.index_path = index_path
        self.transcript = transcript
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self.embedding_model = embedding_model
        self.embedding_dimensions = int(embedding_dimensions or 1024)
        self.enabled = enabled
        self.timeout = timeout
        self._chunks: list[CompanyKnowledgeChunk] | None = None
        self._embeddings: list[list[float] | None] | None = None
        self._embeddings_fingerprint = ""
        self._embedding_failed = False

    def available(self) -> bool:
        return self.enabled and self.knowledge_dir.exists()

    def search(self, query: str, category: str | None = None, max_results: int = 3) -> list[dict[str, Any]]:
        query = query.strip()
        if not self.available() or not query:
            return []
        max_results = max(1, min(int(max_results or 3), 10))
        chunks = self._load_chunks()
        if category:
            category = category.strip().lower()
            filtered = [chunk for chunk in chunks if category in {chunk.category.lower(), "all"}]
            if filtered:
                chunks = filtered
        if not chunks:
            return []

        query_tokens = self._tokens(query)
        keyword_scores = [self._keyword_score(query_tokens, query, chunk) for chunk in chunks]
        query_embedding = self._embed_query(query)
        embeddings = self._load_embeddings(chunks) if query_embedding is not None else []
        results: list[dict[str, Any]] = []
        for idx, chunk in enumerate(chunks):
            method = "keyword_fallback"
            score = keyword_scores[idx]
            if query_embedding is not None and idx < len(embeddings) and embeddings[idx]:
                cosine = self._cosine(query_embedding, embeddings[idx] or [])
                score = (cosine * 0.85) + (min(keyword_scores[idx], 1.0) * 0.15)
                method = "embedding_hybrid"
            if score <= 0:
                continue
            results.append(self._result(chunk, score, method))

        results.sort(key=lambda item: item["score"], reverse=True)
        selected = results[:max_results]
        self.transcript.emit(
            "company_rag.search",
            query=truncate(query, 300),
            category=category or "",
            count=len(selected),
            method=selected[0]["retrieval_method"] if selected else "none",
        )
        return selected

    def context_block(self, query: str, max_results: int = 5) -> str:
        results = self.search(query, max_results=max_results)
        if not results:
            return ""
        lines = ["<company_knowledge>", "Use these company policies as review standards when relevant."]
        for item in results:
            lines.extend(
                [
                    f"- policy_id: {item['policy_id']}",
                    f"  source: {item['doc_path']}#{item['heading']}",
                    f"  retrieval: {item['retrieval_method']} score={item['score']}",
                    f"  excerpt: {item['excerpt']}",
                ]
            )
        lines.append("</company_knowledge>")
        return truncate("\n".join(lines), MAX_COMPANY_CONTEXT_CHARS)

    def _load_chunks(self) -> list[CompanyKnowledgeChunk]:
        if self._chunks is not None:
            return self._chunks
        self._chunks = self._read_markdown_chunks()
        self.transcript.emit("company_rag.load", dir=str(self.knowledge_dir), chunks=len(self._chunks))
        return self._chunks

    def _read_markdown_chunks(self) -> list[CompanyKnowledgeChunk]:
        if not self.knowledge_dir.exists():
            self.transcript.emit("company_rag.missing", dir=str(self.knowledge_dir))
            return []
        chunks: list[CompanyKnowledgeChunk] = []
        for path in sorted(self.knowledge_dir.rglob("*.md")):
            rel = path.relative_to(self.knowledge_dir).as_posix()
            category = path.stem.replace("_", "-")
            text = path.read_text(encoding="utf-8", errors="replace")
            current_heading = path.stem.replace("_", " ").title()
            current_lines: list[str] = []

            def flush() -> None:
                body = "\n".join(line.strip() for line in current_lines).strip()
                if not body:
                    return
                chunks.append(
                    CompanyKnowledgeChunk(
                        policy_id=self._policy_id(rel, current_heading),
                        doc_path=rel,
                        heading=current_heading,
                        text=truncate(body, 1800),
                        category=category,
                    )
                )

            for raw in text.splitlines():
                if raw.startswith("#"):
                    flush()
                    current_heading = raw.lstrip("#").strip() or current_heading
                    current_lines = []
                else:
                    current_lines.append(raw)
            flush()
        return chunks

    def _load_embeddings(self, chunks: list[CompanyKnowledgeChunk]) -> list[list[float] | None]:
        fingerprint = self._fingerprint(chunks)
        if self._embeddings is not None and self._embeddings_fingerprint == fingerprint:
            return self._embeddings
        cached = self._read_index_cache(chunks)
        if cached is not None:
            self._embeddings = cached
            self._embeddings_fingerprint = fingerprint
            return cached
        if not self.api_key or self._embedding_failed:
            self.transcript.emit("company_rag.embedding_disabled", reason="DASHSCOPE_API_KEY is not set")
            self._embeddings = [None for _ in chunks]
            self._embeddings_fingerprint = fingerprint
            return self._embeddings
        vectors = self._embed_texts([self._embedding_text(chunk) for chunk in chunks])
        if vectors is None:
            self._embedding_failed = True
            self._embeddings = [None for _ in chunks]
            self._embeddings_fingerprint = fingerprint
            return self._embeddings
        self._embeddings = vectors
        self._embeddings_fingerprint = fingerprint
        self._write_index_cache(chunks, vectors)
        return vectors

    def _embed_query(self, query: str) -> list[float] | None:
        if not self.api_key or self._embedding_failed:
            if not self.api_key:
                self.transcript.emit("company_rag.embedding_disabled", reason="DASHSCOPE_API_KEY is not set")
            return None
        vectors = self._embed_texts([query])
        if not vectors or vectors[0] is None:
            self._embedding_failed = True
            return None
        return vectors[0]

    def _embed_texts(self, texts: list[str]) -> list[list[float]] | None:
        payload: dict[str, Any] = {
            "model": self.embedding_model,
            "input": texts,
            "dimensions": self.embedding_dimensions,
        }
        endpoint = f"{self.base_url}/embeddings"
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        self.transcript.emit(
            "company_rag.embedding.request",
            model=self.embedding_model,
            dimensions=self.embedding_dimensions,
            count=len(texts),
            endpoint=endpoint,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            vectors = [item["embedding"] for item in sorted(data["data"], key=lambda item: item.get("index", 0))]
            if len(vectors) != len(texts):
                raise ValueError("embedding count mismatch")
            self.transcript.emit("company_rag.embedding.response", count=len(vectors))
            return [[float(value) for value in vector] for vector in vectors]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.transcript.emit("company_rag.embedding.error", error=str(exc))
            return None

    def _read_index_cache(self, chunks: list[CompanyKnowledgeChunk]) -> list[list[float] | None] | None:
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if payload.get("fingerprint") != self._fingerprint(chunks):
            return None
        if payload.get("model") != self.embedding_model:
            return None
        if int(payload.get("dimensions", 0) or 0) != self.embedding_dimensions:
            return None
        vectors = payload.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(chunks):
            return None
        self.transcript.emit("company_rag.index_cache.hit", chunks=len(chunks))
        return vectors

    def _write_index_cache(self, chunks: list[CompanyKnowledgeChunk], vectors: list[list[float]]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "fingerprint": self._fingerprint(chunks),
            "model": self.embedding_model,
            "dimensions": self.embedding_dimensions,
            "chunks": [chunk.to_dict() for chunk in chunks],
            "embeddings": vectors,
        }
        self.index_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.transcript.emit("company_rag.index_cache.write", chunks=len(chunks))

    def _fingerprint(self, chunks: list[CompanyKnowledgeChunk]) -> str:
        digest = hashlib.sha256()
        for chunk in chunks:
            digest.update(json.dumps(chunk.to_dict(), sort_keys=True, ensure_ascii=False).encode("utf-8"))
        return digest.hexdigest()

    def _policy_id(self, rel: str, heading: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-") or "policy"
        return f"{Path(rel).stem}:{slug}"

    def _embedding_text(self, chunk: CompanyKnowledgeChunk) -> str:
        return f"{chunk.doc_path}\n{chunk.heading}\n{chunk.text}"

    def _result(self, chunk: CompanyKnowledgeChunk, score: float, method: str) -> dict[str, Any]:
        excerpt = " ".join(chunk.text.split())
        return {
            "policy_id": chunk.policy_id,
            "doc_path": chunk.doc_path,
            "heading": chunk.heading,
            "excerpt": truncate(excerpt, 700),
            "score": round(float(score), 4),
            "retrieval_method": method,
        }

    def _tokens(self, text: str) -> set[str]:
        tokens = {token.lower() for token in re.findall(r"[A-Za-z0-9_./-]{2,}", text)}
        aliases = {
            "webhook": {"signature", "callback", "replay", "idempotency"},
            "signature": {"webhook", "hmac", "reject"},
            "token": {"secret", "credential", "log"},
            "card": {"pan", "sensitive", "log"},
            "sql": {"parameterized", "injection", "query"},
            "shell": {"subprocess", "command", "execution"},
            "test": {"coverage", "regression"},
        }
        expanded = set(tokens)
        for token in tokens:
            expanded.update(aliases.get(token, set()))
        return expanded

    def _keyword_score(self, query_tokens: set[str], query: str, chunk: CompanyKnowledgeChunk) -> float:
        haystack = f"{chunk.doc_path} {chunk.heading} {chunk.text}".lower()
        if not query_tokens:
            return 0.0
        overlap = sum(1 for token in query_tokens if token in haystack)
        score = overlap / max(len(query_tokens), 1)
        query_lower = query.lower()
        for phrase in ("webhook signature", "sensitive log", "sql injection", "shell true", "command execution"):
            if phrase in query_lower and all(part in haystack for part in phrase.split()):
                score += 0.35
        return score

    def _cosine(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 0.0
        dot = sum(a * b for a, b in zip(left, right))
        left_norm = math.sqrt(sum(a * a for a in left))
        right_norm = math.sqrt(sum(b * b for b in right))
        if left_norm == 0 or right_norm == 0:
            return 0.0
        return dot / (left_norm * right_norm)
