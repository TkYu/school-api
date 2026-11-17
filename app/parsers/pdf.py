"""Any text PDF (a page's attachment on the official site): its text page by page, cut into chunks that keep the
page number, so an answer can cite "p.3". Scanned PDFs without a text layer give no chunks (noted on the version)."""
from __future__ import annotations


def read_pages(data: bytes) -> list[str]:
    import pymupdf
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        # text blocks (paragraphs, table cells) in reading order, a blank line between them
        return ["\n\n".join(b[4].strip() for b in page.get_text("blocks", sort=True) if b[6] == 0 and b[4].strip()) for page in doc]


def chunks(pages: list[str], size: int = 800) -> list[tuple[str, str]]:
    """(section, text): section "p.N" (or "p.N–M" when short pages are joined); paragraphs up to ~size characters."""
    out: list[tuple[str, str]] = []
    for no, text in enumerate(pages, start=1):
        cur = ""
        for para in [" ".join(p.split()) for p in text.split("\n\n") if p.strip()] or [" ".join(text.split())]:
            if not para:
                continue
            if cur and len(cur) + len(para) > size:
                out.append((f"p.{no}", cur))
                cur = ""
            cur = f"{cur}\n{para}" if cur else para
            while len(cur) > size * 2:   # one long block without blank lines
                out.append((f"p.{no}", cur[:size]))
                cur = cur[size:]
        if cur:
            out.append((f"p.{no}", cur))
    return out
