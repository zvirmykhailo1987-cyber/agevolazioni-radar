# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
"""Question answering over the knowledge base with mandatory, verified citations.

Retrieval is lexical (BM25 via LangChain) with light Italian normalization —
the corpus is small, legal and full of exact terms ("ZES", "35 anni",
"tempo indeterminato") where lexical recall beats generic embeddings. The model
answers through a forced tool call; code then checks every quote against the
cited paragraph. No verified citation -> the answer is replaced by a refusal.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document

from radar.paths import HOME
from radar.rules import NUMBER_WORDS
from radar.text import canon, quote_found

NUMBER = re.compile(r"\b\d+\b")

DEFAULT_MODEL = os.environ.get("RADAR_QA_MODEL", "claude-sonnet-5-5")
EMBED_MODEL = os.environ.get("RADAR_EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
MODELS_DIR = Path(os.environ.get("RADAR_MODELS_DIR", HOME / "data" / "models"))
REFUSAL = "Non trovo questa informazione nelle circolari e nei messaggi INPS presenti nella base di conoscenza."

_STOP = set(
    [
        "a",
        "ad",
        "al",
        "alla",
        "alle",
        "agli",
        "ai",
        "all",
        "che",
        "chi",
        "ci",
        "con",
        "da",
        "dal",
        "dalla",
        "dalle",
        "dei",
        "del",
        "della",
        "delle",
        "dei",
        "di",
        "e",
        "ed",
        "gli",
        "i",
        "il",
        "in",
        "la",
        "le",
        "lo",
        "ma",
        "ne",
        "nei",
        "nel",
        "nella",
        "nelle",
        "non",
        "o",
        "per",
        "piu",
        "quale",
        "quali",
        "quando",
        "se",
        "si",
        "sono",
        "su",
        "sul",
        "sulla",
        "tra",
        "un",
        "una",
        "uno",
        "come",
        "cosa",
        "quanto",
        "quanti",
        "questo",
        "questa",
        "essere",
        "ha",
        "hanno",
        "sia",
        "anche",
    ]
)


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def tokenize(text: str) -> list[str]:
    """Lowercase, strip accents, drop stopwords, crude Italian suffix trimming."""
    out = []
    for t in re.findall(r"[a-z0-9]+", _fold(text)):
        if t in _STOP:
            continue
        if not t.isdigit() and len(t) > 5:
            t = re.sub(r"(azioni|azione|mente|ioni|ione|ità|ita|ati|ate|ato|ata|ere|are|ire|i|e|o|a)$", "", t)
        out.append(t)
    return out


@dataclass
class Passage:
    doc_id: int
    doc_label: str
    url: str
    anchor: str
    heading: str
    text: str
    subject: str = ""

    @property
    def topic(self) -> str:
        """The distinctive tail of the act's subject ("Bonus donne 2026. Prime indicazioni operative").

        INPS subjects start with the long title of the decree they implement, identical across
        sibling circulars; only the tail names the incentive. Indexing it with every paragraph
        lets "Bonus donne" route to the right circular instead of any text mentioning women.
        """
        tail = self.subject.rsplit("”.", 1)[-1] if "”." in self.subject else self.subject
        return tail.strip()


def build_bm25(passages: list[Passage], k: int) -> BM25Retriever:
    docs = [
        Document(page_content=f"{p.topic}\n{p.heading}\n{p.text}", metadata={"i": i}) for i, p in enumerate(passages)
    ]
    return BM25Retriever.from_documents(docs, k=k, preprocess_func=tokenize)


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> Any: ...  # (n, d) array of unit vectors


class FastEmbedder:
    """Local multilingual sentence embeddings (fastembed, ONNX on CPU, no API, no cost)."""

    def __init__(self, model: str = EMBED_MODEL, cache_dir: Path = MODELS_DIR) -> None:
        from fastembed import TextEmbedding

        self.name = model
        self._model = TextEmbedding(model, cache_dir=str(cache_dir), local_files_only=True)

    def embed(self, texts: list[str]) -> Any:
        import numpy as np

        vecs = np.array(list(self._model.embed(texts)), dtype=np.float32)
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True).clip(min=1e-9)


def try_embedder() -> Embedder | None:
    """The embedder if its model files are already on disk; never downloads at serving time."""
    if os.environ.get("RADAR_EMBEDDINGS", "off") != "on":  # off by default: see docs/DECISIONS.md ADR-2
        return None
    try:
        return FastEmbedder()
    except Exception:  # model missing, no ONNX runtime, ...: lexical retrieval still works
        return None


class HybridRetriever:
    """BM25 and dense retrieval fused by reciprocal rank (k=60); falls back to BM25 alone."""

    RRF_K = 60

    def __init__(
        self, passages: list[Passage], k: int = 10, embedder: Embedder | None = None, cache_dir: Path | None = None
    ) -> None:
        self.k = k
        self.bm25 = build_bm25(passages, k=k * 3)
        self.embedder = embedder
        self.vecs = None
        if embedder is not None:
            texts = [f"{p.topic}. {p.heading}. {p.text}" for p in passages]
            self.vecs = self._cached_vectors(texts, embedder, cache_dir)

    @staticmethod
    def _cached_vectors(texts: list[str], embedder: Embedder, cache_dir: Path | None) -> Any:
        import numpy as np

        if cache_dir is None:
            return embedder.embed(texts)
        key = hashlib.sha256((embedder.name + "\x00" + "\x00".join(texts)).encode()).hexdigest()[:20]
        path = cache_dir / f"emb-{key}.npy"
        if path.exists():
            return np.load(path)
        vecs = embedder.embed(texts)
        cache_dir.mkdir(parents=True, exist_ok=True)
        np.save(path, vecs)
        return vecs

    def invoke(self, query: str) -> list[Document]:
        lexical = [d.metadata["i"] for d in self.bm25.invoke(query)]
        scores: dict[int, float] = {}
        for rank, i in enumerate(lexical):
            scores[i] = scores.get(i, 0.0) + 1.0 / (self.RRF_K + rank)
        if self.embedder is not None and self.vecs is not None:
            import numpy as np

            qv = self.embedder.embed([query])[0]
            dense = np.argsort(-(self.vecs @ qv))[: self.k * 3]
            for rank, i in enumerate(dense.tolist()):
                scores[i] = scores.get(i, 0.0) + 1.0 / (self.RRF_K + rank)
        top = sorted(scores, key=lambda i: -scores[i])[: self.k]
        return [Document(page_content="", metadata={"i": i}) for i in top]


def build_retriever(
    passages: list[Passage], k: int = 10, embedder: Embedder | None = None, cache_dir: Path | None = None
) -> HybridRetriever:
    return HybridRetriever(passages, k=k, embedder=embedder, cache_dir=cache_dir)


class Answerer(Protocol):
    def __call__(self, system: str, user: str, tool: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]: ...


SYSTEM = """Rispondi a domande sugli incentivi all'assunzione usando SOLO i passaggi forniti
(circolari e messaggi INPS). Ogni passaggio ha un identificatore come [D15258 4.p2].
- Rispondi in italiano, in modo breve e preciso; numeri e date esattamente come nel testo.
- Ogni affermazione deve avere almeno una citazione: identificatore + frase copiata parola per parola.
- Se i passaggi non contengono la risposta, imposta answerable=false e non inventare nulla.
- Non dare consulenza: la verifica finale spetta a un consulente del lavoro."""

TOOL = {
    "name": "answer",
    "description": "Give the grounded answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "answerable": {"type": "boolean"},
            "answer": {"type": "string"},
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"passage_id": {"type": "string"}, "quote": {"type": "string"}},
                    "required": ["passage_id", "quote"],
                },
            },
        },
        "required": ["answerable", "answer", "citations"],
    },
}


@dataclass
class Answer:
    question: str
    answer: str
    answerable: bool
    citations: list[dict[str, Any]] = field(default_factory=list)
    rejected_citations: int = 0
    retrieved: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


def unsupported_numbers(text: str, question: str, cited: list[Passage]) -> list[str]:
    """Figures in ``text`` that no cited paragraph states (as digits or as an Italian number word).

    Numbers from the question and from the cited acts' titles (e.g. "Bonus Giovani 2026",
    "Circolare n. 55") are allowed: they name the topic, they are not claims.
    """
    allowed: set[str] = set(NUMBER.findall(canon(question)))
    for p in cited:
        words = canon(f"{p.text} {p.topic} {p.doc_label}").split()
        allowed.update(w for w in words if w.isdigit())
        allowed.update(str(NUMBER_WORDS[w]) for w in words if w in NUMBER_WORDS)
    return [n for n in NUMBER.findall(canon(text)) if n not in allowed]


def pid(p: Passage) -> str:
    return f"D{p.doc_id} {p.anchor}"


def answer(question: str, passages: list[Passage], retriever: HybridRetriever, llm: Answerer) -> Answer:
    hits = [passages[d.metadata["i"]] for d in retriever.invoke(question)]
    by_id = {pid(p): p for p in hits}
    context = "\n\n".join(f"[{pid(p)}] ({p.doc_label}: {p.topic} — {p.heading})\n{p.text}" for p in hits)
    raw, usage = llm(SYSTEM, f"PASSAGGI:\n{context}\n\nDOMANDA: {question}", TOOL)

    good, bad = [], 0
    for c in raw.get("citations", []) or []:
        p = by_id.get(str(c.get("passage_id", "")).strip("[] "))
        if p and quote_found(str(c.get("quote", "")), p.text):
            good.append(
                {
                    "passage_id": pid(p),
                    "doc_label": p.doc_label,
                    "url": p.url,
                    "anchor": p.anchor,
                    "quote": c["quote"],
                    "paragraph": p.text,
                }
            )
        else:
            bad += 1
    # Runtime faithfulness: every figure in the answer must appear in a verified cited paragraph,
    # so one real quote cannot launder an invented number.
    # figures must come from the retrieved source text (any retrieved paragraph), never from the model
    unsupported = unsupported_numbers(raw.get("answer", ""), question, hits)
    ok = bool(raw.get("answerable")) and bool(good) and not unsupported
    return Answer(
        question, raw.get("answer", "") if ok else REFUSAL, ok, good if ok else [], bad, [pid(p) for p in hits], usage
    )


def anthropic_llm(model: str = DEFAULT_MODEL, max_tokens: int = 1500) -> Answerer:
    def call(system: str, user: str, tool: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        from radar.llm import call_json

        return call_json(system, user, tool["input_schema"], model, max_tokens, purpose="qa")

    return call
