import io
from zipfile import ZipFile

import pytest
from docx import Document
from openpyxl import load_workbook
from openpyxl.styles import Font
from pptx import Presentation

from ydisk_plugin.account import Problem
from ydisk_plugin.office import Edit, OfficeContent, Slide, apply_edits, create, records


def saved(doc):
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_word_preserves_runs_and_table():
    doc = Document()
    p = doc.add_paragraph()
    p.add_run("a").bold = True
    p.add_run("bc").italic = True
    doc.add_table(1, 1).cell(0, 0).text = "old"
    original = saved(doc)
    edited = apply_edits(
        ".docx",
        original,
        [Edit(target="p:1", value="XYZ"), Edit(target="t:1:1:1:1", value="новое")],
    )
    doc = Document(io.BytesIO(edited))
    assert doc.paragraphs[0].text == "XYZ"
    assert doc.paragraphs[0].runs[0].bold and doc.paragraphs[0].runs[1].italic
    assert doc.tables[0].cell(0, 0).text == "новое"
    with ZipFile(io.BytesIO(original)) as a, ZipFile(io.BytesIO(edited)) as b:
        assert [n for n in a.namelist() if a.read(n) != b.read(n)] == [
            "word/document.xml"
        ]


def test_workbook_preserves_formula_style_and_new_cells():
    raw = create(".xlsx", OfficeContent(sheets={"Итог": [[2, 3]]}))
    book = load_workbook(io.BytesIO(raw))
    book.active["C1"] = "=A1+B1"
    book.active["A1"].font = Font(bold=True)
    book.active.merge_cells("C4:D4")
    raw = saved(book)
    patched = apply_edits(
        ".xlsx",
        raw,
        [Edit(target="Итог!A1", value=7), Edit(target="Итог!D10", value="=literal")],
    )
    sheet = load_workbook(io.BytesIO(patched)).active
    assert sheet["A1"].value == 7 and sheet["A1"].font.bold
    assert sheet["C1"].value == "=A1+B1" and sheet["D10"].data_type == "s"
    assert ("Итог!C1", "=A1+B1") in [
        (r["target"], r["text"]) for r in records(".xlsx", patched)
    ]
    with pytest.raises(Problem):
        apply_edits(".xlsx", raw, [Edit(target="Итог!D4", value=0)])


def test_slides_and_pagination_addresses():
    raw = create(
        ".pptx",
        OfficeContent(
            slides=[Slide(title="Один", paragraphs=["Доклад"]), Slide(title="Два")]
        ),
    )
    row = next(row for row in records(".pptx", raw) if row["text"] == "Доклад")
    changed = apply_edits(".pptx", raw, [Edit(target=row["target"], value="Обновлено")])
    deck = Presentation(io.BytesIO(changed))
    assert deck.slides[0].placeholders[1].text == "Обновлено"
    assert deck.slides[1].shapes.title.text == "Два"
