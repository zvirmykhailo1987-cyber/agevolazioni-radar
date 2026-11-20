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

import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol

from langchain_community.retrievers import BM25Retriever
from langchain_core.documents import Document

from radar.rules import NUMBER_WORDS
from radar.text import canon, quote_found

NUMBER = re.compile(r"\b\d+\b")

DEFAULT_MODEL = os.environ.get("RADAR_QA_MODEL", "claude-sonnet-5-5")
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


def build_retriever(passages: list[Passage], k: int = 10) -> BM25Retriever:
    docs = [
        Document(page_content=f"{p.topic}\n{p.heading}\n{p.text}", metadata={"i": i}) for i, p in enumerate(passages)
    ]
    return BM25Retriever.from_documents(docs, k=k, preprocess_func=tokenize)


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


def answer(question: str, passages: list[Passage], retriever: BM25Retriever, llm: Answerer) -> Answer:
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
    unsupported = unsupported_numbers(raw.get("answer", ""), question, [by_id[c["passage_id"]] for c in good])
    ok = bool(raw.get("answerable")) and bool(good) and not unsupported
    return Answer(
        question, raw.get("answer", "") if ok else REFUSAL, ok, good if ok else [], bad, [pid(p) for p in hits], usage
    )


def anthropic_llm(model: str = DEFAULT_MODEL, max_tokens: int = 1500) -> Answerer:
    def call(system: str, user: str, tool: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        from radar.llm import call_json

        return call_json(system, user, tool["input_schema"], model, max_tokens)

    return call
