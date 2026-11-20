# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
from conftest import FakeLLM

from radar.rag import REFUSAL, Passage, answer, build_retriever, tokenize

PS = [
    Passage(
        1,
        "Circolare n. 57",
        "u1",
        "1.p15",
        "1. Premessa",
        "L’importo dell’esonero è aumentato nel limite massimo di importo pari a 800 euro su base mensile.",
    ),
    Passage(
        2,
        "Circolare n. 56",
        "u2",
        "1.p4",
        "1. Premessa",
        "I datori di lavoro devono occupare fino a un massimo di 10 dipendenti.",
    ),
    Passage(
        3,
        "Messaggio n. 1",
        "u3",
        "2.p1",
        "2. Procedura",
        "La domanda si presenta tramite il portale delle agevolazioni.",
    ),
]


def test_tokenize_folds_accents_and_drops_stopwords():
    assert tokenize("L'età della lavoratrice è") == ["l", "eta", "lavoratric"]


def test_retrieval_finds_the_right_paragraph():
    r = build_retriever(PS, k=1)
    assert r.invoke("quanti dipendenti al massimo?")[0].metadata["i"] == 1


def test_verified_citation_is_kept():
    llm = FakeLLM(
        {
            "answerable": True,
            "answer": "Fino a 800 euro al mese.",
            "citations": [{"passage_id": "D1 1.p15", "quote": "pari a 800 euro su base mensile"}],
        }
    )
    a = answer("importo esonero mensile in euro", PS, build_retriever(PS, k=2), llm)
    assert a.answerable and a.answer.startswith("Fino") and a.citations[0]["anchor"] == "1.p15"
    assert a.rejected_citations == 0 and "D1 1.p15" in a.retrieved


def test_unverifiable_citation_turns_answer_into_refusal():
    llm = FakeLLM(
        {
            "answerable": True,
            "answer": "Fino a 900 euro.",
            "citations": [
                {"passage_id": "D1 1.p15", "quote": "pari a 900 euro"},
                {"passage_id": "D9 9.p9", "quote": "x"},
            ],
        }
    )
    a = answer("importo esonero mensile in euro", PS, build_retriever(PS, k=2), llm)
    assert "D1 1.p15" in a.retrieved, "the cited passage was retrieved; only the quote is wrong"
    assert not a.answerable and a.answer == REFUSAL and a.rejected_citations == 2 and a.citations == []


def test_model_refusal_is_respected():
    llm = FakeLLM({"answerable": False, "answer": "Non so", "citations": []})
    a = answer("IRPEF?", PS, build_retriever(PS, k=2), llm)
    assert a.answer == REFUSAL


def test_invented_number_next_to_a_real_quote_is_refused():
    llm = FakeLLM(
        {
            "answerable": True,
            "answer": "Fino a 950 euro al mese.",
            "citations": [{"passage_id": "D1 1.p15", "quote": "pari a 800 euro su base mensile"}],
        }
    )
    a = answer("importo esonero mensile in euro", PS, build_retriever(PS, k=2), llm)
    assert not a.answerable and a.answer == REFUSAL


def test_numbers_written_as_words_or_named_in_titles_are_supported():
    from radar.rag import unsupported_numbers

    p = Passage(
        1,
        "Circolare n. 55 del 14/05/2026",
        "u",
        "1.p3",
        "1. Premessa",
        "giovani che non hanno compiuto il trentacinquesimo anno di età, per ventiquattro mesi",
        "Articolo 2 … del caporalato digitale”. Bonus Giovani 2026. Prime indicazioni operative",
    )
    assert unsupported_numbers("Bonus Giovani 2026: sotto i 35 anni, per 24 mesi", "Quanto dura?", [p]) == []
    assert unsupported_numbers("per 36 mesi", "Quanto dura?", [p]) == ["36"]
