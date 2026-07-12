"""Build a local searchable legal corpus from archived official sources."""

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def clean_text(text: str) -> str:
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def read_html(path: Path) -> str:
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    for node in soup(["script", "style", "nav", "footer", "header"]):
        node.decompose()
    candidates = [
        soup.select_one(".pages_content"),
        soup.select_one(".article"),
        soup.select_one(".TRS_Editor"),
        soup.select_one("#UCAP-CONTENT"),
        soup.select_one("main"),
        soup.body,
    ]
    content = next((node for node in candidates if node and len(node.get_text(strip=True)) > 300), soup)
    return clean_text(content.get_text("\n", strip=True))


def read_pdf(path: Path) -> str:
    reader = PdfReader(str(path))
    return clean_text("\n".join(page.extract_text() or "" for page in reader.pages))


def chunk_text(text: str, size: int = 1100, overlap: int = 160):
    paragraphs = [part.strip() for part in text.splitlines() if part.strip()]
    chunks, current = [], ""
    for paragraph in paragraphs:
        if len(current) + len(paragraph) + 1 <= size:
            current = f"{current}\n{paragraph}".strip()
            continue
        if current:
            chunks.append(current)
        tail = current[-overlap:] if current else ""
        current = f"{tail}\n{paragraph}".strip()
    if current:
        chunks.append(current)
    return chunks


def main():
    sources = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    records = []
    stats = []
    for source in sources:
        path = DATA / source["local_file"]
        if not path.exists():
            stats.append({"source_id": source["id"], "status": "missing", "chunks": 0})
            continue
        text = read_pdf(path) if path.suffix.lower() == ".pdf" else read_html(path)
        chunks = chunk_text(text)
        for index, chunk in enumerate(chunks):
            records.append({
                "chunk_id": f"{source['id']}-{index + 1:04d}",
                "source_id": source["id"],
                "title": source["title"],
                "authority": source["authority"],
                "url": source["url"],
                "retrieved_at": source["retrieved_at"],
                "text": chunk,
            })
        stats.append({"source_id": source["id"], "status": "indexed", "characters": len(text), "chunks": len(chunks)})

    (DATA / "legal_index.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / "index_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Indexed {len(records)} chunks from {sum(item['status'] == 'indexed' for item in stats)} sources")


if __name__ == "__main__":
    main()
