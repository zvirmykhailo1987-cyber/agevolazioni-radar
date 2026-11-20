# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
from radar.text import content_hash, decode_field, normalize_space, paragraphs, quote_found


def test_paragraphs_are_anchored_to_bold_numbered_sections(c55_detail):
    ps = paragraphs(decode_field(c55_detail["testoCompleto"]["value"]))
    anchors = [p.anchor for p in ps]
    assert len(anchors) == len(set(anchors)), "anchors must be unique"
    assert ps[0].anchor == "1.p1" and ps[0].heading == "1. Premessa"
    assert not any(p.section == "0" for p in ps), "table of contents must be skipped"
    assert {p.section for p in ps} >= {str(i) for i in range(1, 11)}


def test_heading_needs_bold_and_short_text():
    html = (
        "<p>INDICE</p><p><em>1. Premessa</em></p><p><strong>1. Premessa</strong></p><p>Testo.</p>"
        "<p>2. Non e' un titolo perche' non e' in grassetto ma e' una frase numerata qualsiasi.</p>"
    )
    ps = paragraphs(html)
    assert [(p.anchor, p.text[:10]) for p in ps] == [("1.p1", "Testo."), ("1.p2", "2. Non e' ")]


def test_tables_become_pipe_separated_rows():
    ps = paragraphs("<p><strong>1. Tab</strong></p><table><tr><td>A</td><td>500 euro</td></tr></table>")
    assert ps[0].text == "A | 500 euro"


def test_quote_found_tolerates_typographic_quotes_and_spacing():
    src = "L’esonero è pari al 100 per cento  dei contributi, «nel limite» di 500 euro."
    assert quote_found("L'esonero è pari al 100 per cento dei contributi", src)
    assert quote_found('"nel limite" di 500 euro', src)
    assert not quote_found("pari al 90 per cento", src)
    assert not quote_found("   ", src)


def test_hash_ignores_whitespace_only_changes():
    assert content_hash("a  b\n c") == content_hash("a b c")
    assert content_hash("a b c") != content_hash("a b d")
    assert normalize_space("&nbsp;x&amp;y ") == "x&y"
