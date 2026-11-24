"""Read office packages and change only the addressed OOXML parts."""

import io
import posixpath
import re
import zipfile
from pathlib import PurePosixPath
from typing import Literal

from docx import Document
from lxml import etree
from openpyxl import Workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from pptx import Presentation
from pydantic import BaseModel, Field
from pypdf import PdfReader

from .account import Problem

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
X = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SPACE = "{http://www.w3.org/XML/1998/namespace}space"
MAX_TEXT = 1000000
TEXT_TYPES = {".txt", ".md", ".csv", ".tsv", ".json", ".log"}
SUPPORTED = TEXT_TYPES | {".docx", ".xlsx", ".pptx", ".pdf"}
Scalar = str | float | int | bool | None


class Slide(BaseModel):
    title: str = ""
    paragraphs: list[str] = Field(default_factory=list)


class OfficeContent(BaseModel):
    paragraphs: list[str] = Field(default_factory=list)
    tables: list[list[list[str]]] = Field(default_factory=list)
    sheets: dict[str, list[list[Scalar]]] = Field(default_factory=dict)
    slides: list[Slide] = Field(default_factory=list)


class Edit(BaseModel):
    target: str = Field(
        description="Address returned by read_office, e.g. p:1, t:1:2:3:1, Sheet1!B5, s:1:3:1"
    )
    value: Scalar
    kind: Literal["value", "formula"] = "value"


def create(ext, spec):
    dest = io.BytesIO()
    if ext == ".docx":
        if spec.sheets or spec.slides:
            raise Problem("Для DOCX используйте paragraphs и tables.")
        doc = Document()
        for p in spec.paragraphs:
            doc.add_paragraph(p)
        for grid in spec.tables:
            width = max((len(row) for row in grid), default=0)
            if not width:
                raise Problem("В таблице нужен хотя бы один столбец.")
            table = doc.add_table(rows=len(grid), cols=width)
            for r, row in enumerate(grid):
                for c, value in enumerate(row):
                    table.cell(r, c).text = value
        doc.save(dest)
    elif ext == ".xlsx":
        if spec.paragraphs or spec.tables or spec.slides:
            raise Problem("Для XLSX используйте sheets.")
        book = Workbook()
        book.remove(book.active)
        for title, rows in (spec.sheets or {"Лист1": []}).items():
            if not title or len(title) > 31 or re.search(r"[\\/*?:\[\]]", title):
                raise Problem("Некорректное имя листа.")
            sheet = book.create_sheet(title)
            if sheet.title != title:
                raise Problem("Имена листов должны быть уникальны.")
            for r, row in enumerate(rows, 1):
                for c, value in enumerate(row, 1):
                    cell = sheet.cell(r, c, value)
                    if isinstance(value, str):
                        cell.data_type = "s"
        book.save(dest)
    elif ext == ".pptx":
        if spec.sheets or spec.tables or spec.paragraphs:
            raise Problem("Для PPTX используйте slides.")
        deck = Presentation()
        for item in spec.slides or [Slide()]:
            slide = deck.slides.add_slide(deck.slide_layouts[1])
            slide.shapes.title.text = item.title
            slide.placeholders[1].text_frame.text = "\n".join(item.paragraphs)
        deck.save(dest)
    else:
        raise Problem("Создание поддерживает DOCX, XLSX и PPTX.")
    return dest.getvalue()


def cell_ref(ref):
    m = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]{0,6})", ref)
    if not m:
        raise Problem("Некорректный адрес ячейки.")
    c, r = column_index_from_string(m[1]), int(m[2])
    if c > 16384 or r > 1048576:
        raise Problem("Адрес за пределами листа.")
    return r, c


class Package:
    def __init__(self, content):
        self.z = zipfile.ZipFile(io.BytesIO(content))
        infos = self.z.infolist()
        self.names = set(self.z.namelist())
        if (
            len(infos) > 8000
            or sum(i.file_size for i in infos) > 96000000
            or len(self.names) != len(infos)
        ):
            raise Problem("Пакет слишком большой или имеет повторяющиеся части.")
        self.cache = {}
        self.dirty = set()

    def xml(self, name, dirty=False):
        if name not in self.cache:
            raw = self.z.read(name)
            parser = etree.XMLParser(
                resolve_entities=False, no_network=True, load_dtd=False
            )
            root = etree.fromstring(raw, parser)
            if root.getroottree().docinfo.doctype:
                raise Problem("XML с DTD не поддерживается.")
            self.cache[name] = root
        if dirty:
            self.dirty.add(name)
        return self.cache[name]

    def related(self, part, rid):
        rels = posixpath.join(
            posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels"
        )
        for rel in self.xml(rels):
            if rel.get("Id") == rid and rel.get("TargetMode") != "External":
                target = rel.get("Target", "")
                return posixpath.normpath(
                    posixpath.join(posixpath.dirname(part), target)
                ).lstrip("/")
        raise Problem("В пакете отсутствует связанная часть.")

    def save(self):
        if any(n.lower().startswith("_xmlsignatures/") for n in self.names):
            raise Problem("Редактирование изменит цифровую подпись документа.")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for info in self.z.infolist():
                name = info.filename
                data = (
                    etree.tostring(
                        self.cache[name], encoding="UTF-8", xml_declaration=True
                    )
                    if name in self.dirty
                    else self.z.read(info)
                )
                z.writestr(info, data)
        return out.getvalue()


def doc_targets(pkg):
    part = "word/document.xml"
    body = pkg.xml(part).find(f"{{{W}}}body")
    for i, p in enumerate(body.findall(f"{{{W}}}p"), 1):
        yield f"p:{i}", part, p, W
    for t, table in enumerate(body.findall(f"{{{W}}}tbl"), 1):
        for r, row in enumerate(table.findall(f"{{{W}}}tr"), 1):
            for c, cell in enumerate(row.findall(f"{{{W}}}tc"), 1):
                for i, p in enumerate(cell.findall(f"{{{W}}}p"), 1):
                    yield f"t:{t}:{r}:{c}:{i}", part, p, W


def slide_targets(pkg):
    part = "ppt/presentation.xml"
    for number, sid in enumerate(pkg.xml(part).iter(f"{{{P}}}sldId"), 1):
        slide = pkg.related(part, sid.get(f"{{{R}}}id"))
        for shape in pkg.xml(slide).iter(f"{{{P}}}sp"):
            shape_id = shape.find(f"./{{{P}}}nvSpPr/{{{P}}}cNvPr").get("id")
            for i, para in enumerate(shape.findall(f"./{{{P}}}txBody/{{{A}}}p"), 1):
                yield f"s:{number}:{shape_id}:{i}", slide, para, A


def sheets(pkg):
    part = "xl/workbook.xml"
    return {
        s.get("name"): pkg.related(part, s.get(f"{{{R}}}id"))
        for s in pkg.xml(part).iter(f"{{{X}}}sheet")
    }


def text_of(element, ns):
    return "".join(t.text or "" for t in element.iter(f"{{{ns}}}t"))


def xlsx_records(pkg):
    shared = []
    if "xl/sharedStrings.xml" in pkg.names:
        shared = [
            text_of(item, X)
            for item in pkg.xml("xl/sharedStrings.xml").iter(f"{{{X}}}si")
        ]
    for title, path in sheets(pkg).items():
        yield {"target": title, "type": "sheet", "text": title}
        for c in pkg.xml(path).iter(f"{{{X}}}c"):
            v = c.findtext(f"{{{X}}}v", "")
            formula = c.find(f"{{{X}}}f")
            if formula is not None:
                value = "=" + (formula.text or "")
            elif c.get("t") == "s":
                value = shared[int(v)]
            elif c.get("t") == "inlineStr":
                value = text_of(c, X)
            else:
                value = v
            yield {"target": title + "!" + c.get("r"), "text": value}


def records(ext, content):
    if ext in TEXT_TYPES:
        for i, line in enumerate(content.decode("utf-8-sig").splitlines(), 1):
            yield {"target": str(i), "text": line}
    elif ext == ".pdf":
        pdf = PdfReader(io.BytesIO(content))
        if pdf.is_encrypted:
            raise Problem("Для зашифрованного PDF извлечение недоступно.")
        for i, page in enumerate(pdf.pages, 1):
            yield {"target": f"page:{i}", "text": page.extract_text() or ""}
    elif ext in (".docx", ".xlsx", ".pptx"):
        pkg = Package(content)
        if ext == ".xlsx":
            yield from xlsx_records(pkg)
        else:
            for key, _, p, ns in (
                doc_targets(pkg) if ext == ".docx" else slide_targets(pkg)
            ):
                yield {"target": key, "text": text_of(p, ns)}
    else:
        raise Problem("Для этого формата используйте скачивание файла.")


def set_paragraph(p, ns, value):
    if any(
        etree.QName(n).localname
        in {
            "fldChar",
            "instrText",
            "tab",
            "br",
            "drawing",
            "fld",
            "del",
            "ins",
            "hyperlink",
        }
        for n in p.iter()
    ):
        raise Problem(
            "Абзац содержит поле, ссылку или сложное оформление. Откройте оригинал."
        )
    texts = list(p.iter(f"{{{ns}}}t"))
    if not texts:
        run = etree.Element(f"{{{ns}}}r")
        end = p.find(f"{{{ns}}}endParaRPr")
        p.insert(p.index(end) if end is not None else len(p), run)
        texts = [etree.SubElement(run, f"{{{ns}}}t")]
    for node in texts[:-1]:
        n = len(node.text or "")
        node.text, value = value[:n], value[n:]
        node.set(SPACE, "preserve")
    texts[-1].text = value
    texts[-1].set(SPACE, "preserve")


def set_cell(pkg, sheet_paths, edit):
    title, sep, ref = edit.target.rpartition("!")
    if not sep or title not in sheet_paths:
        raise Problem("Лист не найден.")
    r, col = cell_ref(ref)
    root = pkg.xml(sheet_paths[title], dirty=True)
    for merged in root.iter(f"{{{X}}}mergeCell"):
        edges = merged.get("ref").split(":")
        a, b = cell_ref(edges[0]), cell_ref(edges[-1])
        if a != (r, col) and a[0] <= r <= b[0] and a[1] <= col <= b[1]:
            raise Problem("Измените верхнюю левую ячейку объединённой области.")
    data = root.find(f"{{{X}}}sheetData")
    row = data.find(f"{{{X}}}row[@r='{r}']")
    if row is None:
        row = etree.Element(f"{{{X}}}row", r=str(r))
        data.insert(
            next((i for i, n in enumerate(data) if int(n.get("r")) > r), len(data)), row
        )
    c = row.find(f"{{{X}}}c[@r='{ref}']")
    if c is None:
        c = etree.Element(f"{{{X}}}c", r=ref)
        row.insert(
            next(
                (
                    i
                    for i, n in enumerate(row)
                    if n.get("r") and cell_ref(n.get("r"))[1] > col
                ),
                len(row),
            ),
            c,
        )
    for child in list(c):
        if child.tag == f"{{{X}}}f" and child.get("t"):
            raise Problem(
                "Групповая формула требует редактирования в офисном приложении."
            )
        if child.tag in {f"{{{X}}}f", f"{{{X}}}v", f"{{{X}}}is"}:
            c.remove(child)
    c.attrib.pop("t", None)
    value = edit.value
    if edit.kind == "formula":
        if not isinstance(value, str) or not value.startswith("="):
            raise Problem("Формула должна начинаться со знака =.")
        etree.SubElement(c, f"{{{X}}}f").text = value[1:]
    elif isinstance(value, str):
        c.set("t", "inlineStr")
        t = etree.SubElement(etree.SubElement(c, f"{{{X}}}is"), f"{{{X}}}t")
        t.set(SPACE, "preserve")
        t.text = value
    elif value is not None:
        c.set("t", "b" if isinstance(value, bool) else "n")
        etree.SubElement(c, f"{{{X}}}v").text = str(
            int(value) if isinstance(value, bool) else value
        )
    dimension = root.find(f"{{{X}}}dimension")
    if dimension is not None:
        endpoints = [cell_ref(v) for v in dimension.get("ref").split(":")] + [(r, col)]
        rows, cols = zip(*endpoints)
        dimension.set(
            "ref",
            f"{get_column_letter(min(cols))}{min(rows)}:{get_column_letter(max(cols))}{max(rows)}",
        )
    calc = pkg.xml("xl/workbook.xml").find(f"{{{X}}}calcPr")
    if calc is not None:
        pkg.dirty.add("xl/workbook.xml")
        calc.set("fullCalcOnLoad", "1")


def apply_edits(ext, content, edits):
    if not edits or len(edits) > 500 or ext not in {".docx", ".xlsx", ".pptx"}:
        raise Problem("Нужны 1–500 правок для DOCX, XLSX или PPTX.")
    pkg = Package(content)
    if ext == ".xlsx":
        sheet_paths = sheets(pkg)
        for edit in edits:
            set_cell(pkg, sheet_paths, edit)
    else:
        targets = {
            k: (part, p, ns)
            for k, part, p, ns in (
                doc_targets(pkg) if ext == ".docx" else slide_targets(pkg)
            )
        }
        for edit in edits:
            if (
                edit.target not in targets
                or not isinstance(edit.value, str)
                or edit.kind != "value"
            ):
                raise Problem("Для указанного абзаца нужен текст и существующий адрес.")
            part, p, ns = targets[edit.target]
            set_paragraph(p, ns, edit.value)
            pkg.dirty.add(part)
    return pkg.save()


def extension(path):
    return PurePosixPath(path).suffix.lower()
