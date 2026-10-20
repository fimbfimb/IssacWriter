"""EPUB 3 packaging with the standard library — no external dependency.

Pipeline: accepted chapters → text cleanup (shared module) → XHTML
(title page, per chapter `<h2>` + `<p>` paragraphs, optional `<figure>` after
anchored paragraphs, optional cover page) → zip with `mimetype` stored
uncompressed as the first entry.
"""

from __future__ import annotations

import datetime
import io
import re
import zipfile
from html import escape
from typing import Any, Callable, Dict, List, Optional, Tuple

from .cleanup import cleanup_paragraphs

MIME_XHTML = "application/xhtml+xml"
MIME_CSS = "text/css"
MIME_PNG = "image/png"
MIME_JPG = "image/jpeg"
MIME_WEBP = "image/webp"

_IMAGE_MIMES = {".png": MIME_PNG, ".jpg": MIME_JPG, ".jpeg": MIME_JPG, ".webp": MIME_WEBP}

_CSS = """body { margin: 4%; line-height: 1.5; }
h1 { text-align: center; margin-top: 22%; font-size: 1.9em; }
h2 { margin-top: 1.6em; page-break-after: avoid; }
p { text-indent: 1.2em; margin: 0 0 0.2em 0; text-align: justify; }
p.first { text-indent: 0; }
/* Illustration = full page plate: the image sits ALONE on its own page
   (break before + after), the text resumes on the following page. */
figure.plate {
  margin: 0;
  padding: 0;
  text-align: center;
  page-break-before: always;
  page-break-after: always;
  break-before: page;
  break-after: page;
  page-break-inside: avoid;
}
figure.plate img { max-width: 96%; height: auto; max-height: 90vh; object-fit: contain; }
.cover-illustration { max-width: 100%; height: auto; margin: 0 auto; }
"""


def _esc(text: Any) -> str:
    return escape(str(text or ""), quote=False)


def slugify_filename(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "book").lower()).strip("-")
    return (slug[:64] or "book") + ".epub"


def image_suffix(path: str) -> str:
    lowered = (path or "").lower()
    for ext in _IMAGE_MIMES:
        if lowered.endswith(ext):
            return ext
    return ".png"


def media_type_of(path: str) -> str:
    return _IMAGE_MIMES[image_suffix(path)]


def chapter_heading(chapter: Dict[str, Any]) -> str:
    number = int(chapter["id"])
    name = (chapter.get("name") or "").strip() or f"Kapitel {number + 1}"
    return name if name.lower().startswith(("kapitel", "chapter")) else f"Kapitel {number + 1}: {name}"


def _xhtml_doc(title: str, body: str, lang: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<!DOCTYPE html>\n"
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'lang="{_esc(lang or "en")}">\n'
        "<head>\n"
        f"<title>{_esc(title)}</title>\n"
        '<meta charset="utf-8"/>\n'
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n'
        "</head>\n"
        f"<body>\n{body}</body>\n</html>\n"
    )


def _chapter_doc_parts(
    chapter: Dict[str, Any], illustrations: List[Dict[str, Any]], image_names: Dict[str, str]
) -> List[Tuple[str, str]]:
    """Chapter split into spine documents so each illustration becomes a hard
    page boundary (readers ignore CSS breaks; spine documents never).

    Returns [(filename, body)] in reading order:
      chNNNN.xhtml     → heading + text before the first image
      chNNNN-i01.xhtml → ONE illustration (full-page plate)
      chNNNN-02.xhtml  → the following text, and so on
    """
    cid = int(chapter["id"])
    paragraphs = cleanup_paragraphs(chapter.get("content") or "")
    by_index: Dict[int, List[Dict[str, Any]]] = {}
    for illustration in illustrations:
        index = illustration.get("paragraph_index")
        if isinstance(index, int) and index >= 0 and illustration.get("id") in image_names:
            by_index.setdefault(index, []).append(illustration)

    def _plate(illustration: Dict[str, Any]) -> str:
        # no captions in the book — the image stands alone on its plate page
        return (
            "<figure class=\"plate\">"
            f'<img src="images/{image_names[illustration["id"]]}" alt=""/>'
            "</figure>"
        )

    documents: List[Tuple[str, str]] = []
    text_buffer: List[str] = [f"<h2>{_esc(chapter_heading(chapter))}</h2>"]
    text_seq = 1
    un_indented = False

    def _flush_text() -> None:
        nonlocal text_buffer, text_seq
        if not text_buffer:
            return
        filename = f"ch{cid:04d}.xhtml" if text_seq == 1 else f"ch{cid:04d}-{text_seq:02d}.xhtml"
        documents.append((filename, "\n".join(text_buffer)))
        text_buffer = []
        text_seq += 1

    for index, paragraph in enumerate(paragraphs):
        class_ = "first" if (index == 0 or un_indented) else ""
        text_buffer.append(f'<p class="{class_}">{_esc(paragraph)}</p>')
        un_indented = False
        for illustration in by_index.get(index, []):
            _flush_text()
            documents.append((f"ch{cid:04d}-{illustration['id']}.xhtml", _plate(illustration)))
            un_indented = True
    _flush_text()
    return documents


def build_epub(
    *,
    title: str,
    chapters: List[Dict[str, Any]],
    language: str = "en",
    author: str = "",
    layout: Optional[Dict[str, Any]] = None,
    load_image: Optional[Callable[[str], Optional[bytes]]] = None,
) -> Tuple[bytes, List[str]]:
    """Build the EPUB. `load_image(path)` loads image bytes from a data path.

    Returns (epub_bytes, warnings). Warnings report skipped illustrations
    (missing paragraph anchor or unreadable file) — the book is built without
    them so an incomplete layout never blocks the export.
    """
    warnings: List[str] = []
    layout = layout or {}
    chapters = sorted(chapters, key=lambda c: int(c["id"]))
    chapter_ids = {int(c["id"]) for c in chapters}

    illustrations = [e for e in layout.get("illustrations") or [] if e.get("id")]
    by_chapter: Dict[int, List[Dict[str, Any]]] = {}
    for entry in illustrations:
        if not entry.get("image_path"):
            continue
        cid = entry.get("chapter_id")
        if cid is None:
            warnings.append(f"illustration {entry['id']}: no chapter anchor — skipped")
            continue
        cid = int(cid)
        if cid not in chapter_ids:
            warnings.append(f"illustration {entry['id']}: chapter {cid} not written — skipped")
            continue
        by_chapter.setdefault(cid, []).append(entry)

    # ---- collect images first: an unreadable file drops its <figure> too ----
    image_names: Dict[str, str] = {}  # illustration id → package file name

    def _load(image_path: str, illustration_id: str) -> Optional[str]:
        name = f"{illustration_id}{image_suffix(image_path)}"
        data = load_image(image_path) if load_image else None
        if not data:
            warnings.append(f"illustration {illustration_id}: image file unreadable — skipped")
            return None
        image_names[illustration_id] = name
        bytes_by_name[name] = data
        return name

    bytes_by_name: Dict[str, bytes] = {}
    for _, entries in by_chapter.items():
        for entry in entries:
            if entry.get("image_path"):
                _load(str(entry["image_path"]), str(entry["id"]))
    # illustrations whose chapter has no image file still need the filename
    # registered for _chapter_body's filter — already done above via _load.

    cover_file: Optional[str] = None
    cover_path = str(layout.get("cover_image_path") or "").strip()
    if cover_path:
        data = load_image(cover_path) if load_image else None
        if data:
            cover_file = f"cover{image_suffix(cover_path)}"
            bytes_by_name[cover_file] = data
        else:
            warnings.append("cover image unreadable — EPUB built without cover")

    # ---- documents -----------------------------------------------------------
    title_body = f"<h1>{_esc(title)}</h1>\n"
    if author:
        title_body += f'<p style="text-align:center; margin-top:1em">{_esc(author)}</p>\n'
    title_doc = _xhtml_doc(title, title_body, language)
    cover_doc = _xhtml_doc(
        "Cover", f'<figure><img class="cover-illustration" src="images/{cover_file}" alt="Cover"/></figure>', language
    ) if cover_file else None

    # spine docs in reading order: per chapter, text segments interleaved with
    # one full-page plate document per illustration
    spine_docs: List[Tuple[str, str]] = []  # (filename, xml_doc)
    segment_label: Dict[str, str] = {}  # filename → toc/manifest label (first segment only)
    for chapter in chapters:
        cid = int(chapter["id"])
        entries = by_chapter.get(cid, [])
        usable = [e for e in entries if str(e["id"]) in image_names]
        for filename, body in _chapter_doc_parts(chapter, usable, image_names):
            spine_docs.append((filename, _xhtml_doc(chapter_heading(chapter), body, language)))
            segment_label.setdefault(filename, chapter_heading(chapter))

    nav_body = '<nav epub:type="toc" id="toc"><h1>Inhaltsverzeichnis</h1>\n<ol>\n'
    for chapter in chapters:
        cid = int(chapter["id"])
        nav_body += f'<li><a href="ch{cid:04d}.xhtml">{_esc(chapter_heading(chapter))}</a></li>\n'
    nav_body += "</ol></nav>\n"
    nav_doc = _xhtml_doc("Inhaltsverzeichnis", nav_body, language)

    # ---- package: content.opf -----------------------------------------------
    manifest = [
        f'    <item id="doc-title" href="title.xhtml" media-type="{MIME_XHTML}"/>',
        f'    <item id="doc-nav" href="nav.xhtml" media-type="{MIME_XHTML}" properties="nav"/>',
        f'    <item id="css" href="style.css" media-type="{MIME_CSS}"/>',
    ]
    for filename, _body in spine_docs:
        doc_id = "doc-" + filename[:-6]  # ch0000 → doc-ch0000 / ch0000-i01 → doc-ch0000-i01
        manifest.append(f'    <item id="{doc_id}" href="{_esc(filename)}" media-type="{MIME_XHTML}"/>')
    if cover_doc:
        manifest.append(f'    <item id="doc-cover" href="cover.xhtml" media-type="{MIME_XHTML}"/>')
    for illustration_id in sorted(image_names):
        name = image_names[illustration_id]
        manifest.append(f'    <item id="img-{illustration_id}" href="images/{name}" media-type="{media_type_of(name)}"/>')
    if cover_file:
        manifest.append(
            f'    <item id="cover-image" href="images/{cover_file}" media-type="{media_type_of(cover_file)}" properties="cover-image"/>'
        )
    spine = ["    <itemref idref=\"doc-title\"/>"]
    if cover_file:
        spine.append('    <itemref idref="doc-cover"/>')
    for filename, _body in spine_docs:
        doc_id = "doc-" + filename[:-6]
        spine.append(f'    <itemref idref="{doc_id}"/>')
    spine.append('    <itemref idref="doc-nav" linear="no"/>')

    modified = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    language_tag = _esc(language or "en")
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id" xml:lang="{language_tag}">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f'    <dc:identifier id="book-id">urn:storyforge:{_esc(title).lower()}</dc:identifier>\n'
        f"    <dc:title>{_esc(title)}</dc:title>\n"
        f"    <dc:creator>{_esc(author or 'Unbekannt')}</dc:creator>\n"
        f"    <dc:language>{language_tag}</dc:language>\n"
        f'    <meta property="dcterms:modified">{modified}</meta>\n'
        '    <meta name="generator">StoryForge</meta>\n'
        "  </metadata>\n"
        "  <manifest>\n" + "\n".join(manifest) + "\n  </manifest>\n"
        "  <spine>\n" + "\n".join(spine) + "\n  </spine>\n"
        "</package>\n"
    )

    container = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
        "  <rootfiles>\n"
        '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
        "  </rootfiles>\n"
        "</container>\n"
    )

    # ---- zip ---------------------------------------------------------------
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/style.css", _CSS)
        archive.writestr("OEBPS/title.xhtml", title_doc)
        if cover_doc:
            archive.writestr("OEBPS/cover.xhtml", cover_doc)
        archive.writestr("OEBPS/nav.xhtml", nav_doc)
        for filename, document in spine_docs:
            archive.writestr(f"OEBPS/{filename}", document)
        for name, data in bytes_by_name.items():
            archive.writestr(f"OEBPS/images/{name}", data)
    return buffer.getvalue(), warnings