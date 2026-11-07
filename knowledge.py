"""Search the user's own notes. Files stay on this machine; only matching excerpts reach the model."""
import contextlib
import logging
from pathlib import Path
import re
import sqlite3
import time

import numpy as np

log = logging.getLogger("uvicorn.error")
TEXT_SUFFIXES = {".md", ".txt"}
DOCUMENT_SUFFIXES = {".pdf", ".docx"}
SUFFIXES = TEXT_SUFFIXES | DOCUMENT_SUFFIXES
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
CHUNK_CHARS = 400
CHUNK_OVERLAP = 50
# Bump when chunks change shape, so old indexes are rebuilt instead of mixing formats.
INDEX_VERSION = 6
SCHEMA = """CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, modified INTEGER NOT NULL, size INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(path UNINDEXED, body UNINDEXED, tokens);
CREATE TABLE IF NOT EXISTS vectors (chunk INTEGER PRIMARY KEY, vector BLOB NOT NULL);"""
# Each list contributes this many candidates before fusion.
CANDIDATES = 20
# Reciprocal rank fusion constant from the original paper; it damps the gap between first and second place.
RRF_K = 60
# A vector search always returns its nearest chunks, even unrelated ones. On short sentences related pairs
# scored 0.53 to 0.65 and unrelated 0.35 to 0.45, but on 198 real notes answers scored 0.50 to 0.83 while
# unrelated questions reached 0.65. 0.75 was the best tested setting there, and still not better than words.
MIN_SIMILARITY = 0.75
# Filling vectors for old chunks during a question must not run past the tool's timeout.
SEARCH_EMBED_SECONDS = 2.0
WORDS = re.compile(r"[一-鿿]+|[A-Za-z0-9]+")
CHINESE_NUMBER = "一二三四五六七八九十百零〇"
# Level by numbering style, most general first. Only short lines that do not end a sentence count.
NUMBERED_HEADINGS = [
    (re.compile(rf"^第[{CHINESE_NUMBER}\d]+[章篇部分]"), 1),
    (re.compile(rf"^第[{CHINESE_NUMBER}\d]+节"), 2),
    (re.compile(rf"^[{CHINESE_NUMBER}]+、"), 2),
    (re.compile(rf"^[（(][{CHINESE_NUMBER}]+[）)]"), 3),
    (re.compile(r"^\d+\.\d+"), 4),
    (re.compile(r"^\d+[、.](?!\d)"), 3),
]
MAX_HEADING_CHARS = 30
CODE_PUNCTUATION = re.compile(r"[{}()\[\];=<>`]|:=|\+\+")
# Question words appear in almost every note, so they matched 红烧肉怎么做 to technical notes.
QUESTION_WORDS = sorted(["怎么样", "怎么办", "怎么", "为什么", "什么", "哪些", "哪个", "哪里", "哪儿", "如何", "是否", "有没有",
                         "可以", "能不能", "一下", "请问", "告诉我", "吗", "呢", "吧", "啊"], key=len, reverse=True)
QUESTION_PATTERN = re.compile("|".join(QUESTION_WORDS))
# Obsidian writes a table of contents of [[#section]] links and a YAML header; both name every topic.
OBSIDIAN_TOC = re.compile(r"^\s*[*-]\s*\[\[#.*$", re.MULTILINE)
FRONT_MATTER = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def query_tokens(query):
    return list(dict.fromkeys(tokens(QUESTION_PATTERN.sub(" ", query))))


def strip_noise(text):
    return OBSIDIAN_TOC.sub("", FRONT_MATTER.sub("", text.replace("\r\n", "\n")))


def tokens(text):
    """Chinese has no spaces, so index overlapping pairs: 报销流程 becomes 报销 销流 流程."""
    found = []
    for word in WORDS.findall(text):
        if word.isascii():
            found.append(word.lower())
        elif len(word) == 1:
            found.append(word)
        else:
            found.extend(word[i:i + 2] for i in range(len(word) - 1))
    return found


def heading(line):
    """Return (level, title) when a line is a heading: Markdown #, or 第一章 / 一、 / （一） / 1. / 1.1 numbering."""
    line = line.strip()
    marked = re.match(r"^(#{1,6})\s+(.+)$", line)
    if marked:
        return len(marked.group(1)), marked.group(2).strip()
    if not line or len(line) > MAX_HEADING_CHARS or line[-1] in "。；;，,：:！？!?":
        return None
    # Numbered code listings look like "5.     for {"; real headings do not carry code punctuation.
    if CODE_PUNCTUATION.search(line):
        return None
    # Line-numbered code pasted as plain text ("9.         break") is indented and has no Chinese.
    if line[0].isdigit() and ("  " in line or not re.search(r"[一-鿿]", line)):
        return None
    return next(((level, line) for pattern, level in NUMBERED_HEADINGS if pattern.match(line)), None)


def sections(text):
    """Split at headings into (path, body); the path lists every heading above, outermost first."""
    found, stack, lines = [], [], []

    def flush():
        body = "\n".join(lines).strip()
        if body:
            found.append((" > ".join(title for _, title in stack), body))
        lines.clear()
    fenced = False
    for line in text.replace("\r\n", "\n").split("\n"):
        # Inside a fenced code block, "# comment" and "1. step" are code, not headings.
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        title = None if fenced or line.lstrip().startswith(("```", "~~~")) else heading(line)
        if title is None:
            lines.append(line)
            continue
        flush()
        while stack and stack[-1][0] >= title[0]:
            stack.pop()
        stack.append(title)
    flush()
    return found


def chunks(text):
    """Label every section with its headings, then merge small sections up to CHUNK_CHARS.

    The label lets 一线城市 500 元 be found by asking about 住宿标准. Merging matters too: measured on
    short documents, one chunk per section scored worse, because words a question shares with nearby
    sections, such as 出差 for a hotel limit, ended up in another chunk.
    """
    blocks = [f"【{path}】\n{piece}" if path else piece for path, body in sections(text) for piece in pack(body)]
    merged, current = [], ""
    for block in blocks:
        if current and len(current) + len(block) + 1 > CHUNK_CHARS:
            merged.append(current)
            current = block
        else:
            current = f"{current}\n{block}" if current else block
    return [*merged, current] if current else merged


def pack(text):
    """Group paragraphs up to CHUNK_CHARS; a longer paragraph is cut with overlap so no sentence is lost."""
    pieces, current = [], ""
    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if len(current) + len(paragraph) + 1 <= CHUNK_CHARS:
            current = f"{current}\n{paragraph}".strip()
            continue
        if current:
            pieces.append(current)
        while len(paragraph) > CHUNK_CHARS:
            pieces.append(paragraph[:CHUNK_CHARS])
            paragraph = paragraph[CHUNK_CHARS - CHUNK_OVERLAP:]
        current = paragraph
    return [piece for piece in [*pieces, current] if piece]


def read_text(path):
    data = path.read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Notes saved by older Windows tools.
        return data.decode("gbk", "replace")


def read_document(path):
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if suffix == ".docx":
        from docx import Document
        document = Document(path)

        def line(paragraph):
            # Word marks headings with a style, not with text, so turn them into Markdown ones.
            level = re.search(r"(?:Heading|标题)\s*(\d)", paragraph.style.name if paragraph.style else "")
            return f"{'#' * int(level.group(1))} {paragraph.text}" if level and paragraph.text.strip() else paragraph.text
        cells = [cell.text for table in document.tables for row in table.rows for cell in row.cells]
        return "\n\n".join([line(paragraph) for paragraph in document.paragraphs] + cells)
    return read_text(path)


def size_limit(name):
    return MAX_DOCUMENT_BYTES if Path(name).suffix.lower() in DOCUMENT_SUFFIXES else MAX_FILE_BYTES


def fuse(*rankings):
    """Reciprocal rank fusion: rank, not score, so BM25 and cosine need no common scale."""
    scores = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking):
            scores[chunk] = scores.get(chunk, 0) + 1 / (RRF_K + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


class KnowledgeBase:
    def __init__(self, folder, index_path, embedder=None):
        self.folder, self.index_path, self.embedder = Path(folder), Path(index_path), embedder

    def semantic(self):
        return self.embedder is not None and self.embedder.available()

    def connect(self):
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.index_path)
        if connection.execute("PRAGMA user_version").fetchone()[0] != INDEX_VERSION:
            connection.executescript("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS files; DROP TABLE IF EXISTS vectors;")
            connection.execute(f"PRAGMA user_version = {INDEX_VERSION}")
        connection.executescript(SCHEMA)
        return connection

    def files(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        root = self.folder.resolve()
        for path in self.folder.rglob("*"):
            # Hidden folders hold tool state (.git, .obsidian, .trash), not notes.
            if any(part.startswith(".") for part in path.relative_to(self.folder).parts):
                continue
            # A link out of the folder must not pull other private files into the model's reach.
            if path.suffix.lower() in SUFFIXES and path.is_file() and path.resolve().is_relative_to(root):
                if path.stat().st_size <= size_limit(path.name):
                    yield path

    def sync(self, embed_seconds=SEARCH_EMBED_SECONDS):
        """Bring the index up to date, reading only files that changed. Returns how many files are indexed.

        embed_seconds caps vector filling; None fills everything, as an upload does.
        """
        found = {path.relative_to(self.folder).as_posix(): path for path in self.files()}
        with contextlib.closing(self.connect()) as connection, connection:
            known = {row[0]: (row[1], row[2]) for row in connection.execute("SELECT path, modified, size FROM files")}
            for name in known.keys() - found.keys():
                self._forget(connection, name)
            for name, path in found.items():
                stat = path.stat()
                if known.get(name) != (stat.st_mtime_ns, stat.st_size):
                    self._forget(connection, name)
                    self._add(connection, name, path, stat)
        self.fill_vectors(embed_seconds)
        return len(found)

    def fill_vectors(self, seconds=None):
        """Vectors for chunks that have none: new ones, or every chunk after the model is first added.

        Committed batch by batch, so a library too big to finish now keeps its progress.
        """
        if not self.semantic():
            return
        deadline = None if seconds is None else time.monotonic() + seconds
        while deadline is None or time.monotonic() < deadline:
            with contextlib.closing(self.connect()) as connection, connection:
                rows = connection.execute("SELECT rowid, body FROM chunks WHERE rowid NOT IN (SELECT chunk FROM vectors) LIMIT 16").fetchall()
                if not rows:
                    return
                try:
                    vectors = self.embedder.encode([body for _, body in rows])
                except Exception:
                    log.exception("could not compute vectors; searching by words only")
                    return
                connection.executemany("INSERT INTO vectors (chunk, vector) VALUES (?, ?)",
                                       [(rowid, vector.tobytes()) for (rowid, _), vector in zip(rows, vectors)])

    @staticmethod
    def _forget(connection, name):
        connection.execute("DELETE FROM vectors WHERE chunk IN (SELECT rowid FROM chunks WHERE path = ?)", (name,))
        connection.execute("DELETE FROM chunks WHERE path = ?", (name,))
        connection.execute("DELETE FROM files WHERE path = ?", (name,))

    @staticmethod
    def _add(connection, name, path, stat):
        title = " ".join(tokens(path.stem))
        try:
            text = read_document(path)
        except Exception:
            # Encrypted or damaged. Recorded with no text, so it shows as unreadable and is not retried.
            log.warning("could not read %s", name)
            text = ""
        for piece in chunks(strip_noise(text)):
            connection.execute("INSERT INTO chunks (path, body, tokens) VALUES (?, ?, ?)", (name, piece, f"{title} {' '.join(tokens(piece))}"))
        connection.execute("INSERT INTO files (path, modified, size) VALUES (?, ?, ?)", (name, stat.st_mtime_ns, stat.st_size))

    def list_files(self):
        """Every indexed file with its excerpt count; zero means no text could be extracted."""
        self.sync()
        with contextlib.closing(self.connect()) as connection:
            counts = dict(connection.execute("SELECT path, COUNT(*) FROM chunks GROUP BY path").fetchall())
            rows = connection.execute("SELECT path, size FROM files ORDER BY path").fetchall()
        return [{"name": name, "size": size, "chunks": counts.get(name, 0)} for name, size in rows]

    def save(self, filename, data):
        """Store an upload and index it now, so a large PDF does not slow the next question."""
        # Browsers send a bare name, but nothing stops a request sending ../../somewhere.
        name = Path(filename.replace("\\", "/")).name.strip()
        if not name or name.startswith("."):
            raise ValueError("文件名无效")
        if Path(name).suffix.lower() not in SUFFIXES:
            raise ValueError("只支持 md、txt、pdf、docx 文件")
        if len(data) > size_limit(name):
            raise ValueError(f"文件太大，上限是 {size_limit(name) // 1024 // 1024} MB")
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.folder / f".{name}.uploading"
        temporary.write_bytes(data)
        temporary.replace(self.folder / name)
        self.sync(embed_seconds=None)
        return next(item for item in self.list_files() if item["name"] == name)

    def delete(self, name):
        """Remove a file for good; returns False if it is not in the library."""
        root = self.folder.resolve()
        path = (self.folder / name).resolve()
        if not path.is_relative_to(root) or path == root or not path.is_file() or path.suffix.lower() not in SUFFIXES:
            return False
        path.unlink()
        self.sync()
        return True

    def search(self, query, limit=5, semantic=True):
        """Return (indexed file count, best excerpts) from words and, when the model is present, meaning."""
        count = self.sync()
        terms = query_tokens(query)
        if not terms:
            return count, []
        expression = " OR ".join(f'"{term}"' for term in terms)
        with contextlib.closing(self.connect()) as connection:
            by_words = [row[0] for row in connection.execute(
                "SELECT rowid FROM chunks WHERE chunks MATCH ? ORDER BY rank LIMIT ?", (expression, CANDIDATES))]
            by_meaning = self.nearest(connection, query) if semantic else []
            chosen = fuse(by_words, by_meaning)[:limit]
            bodies = {row[0]: (row[1], row[2]) for row in connection.execute(
                f"SELECT rowid, path, body FROM chunks WHERE rowid IN ({','.join('?' * len(chosen))})", chosen)}
        return count, [{"file": bodies[chunk][0], "text": bodies[chunk][1]} for chunk in chosen]

    def nearest(self, connection, query):
        """Chunk ids by cosine similarity, closest first, dropping those too far to be related."""
        if not self.semantic():
            return []
        rows = connection.execute("SELECT chunk, vector FROM vectors").fetchall()
        if not rows:
            return []
        try:
            target = self.embedder.encode([query])[0]
        except Exception:
            log.exception("could not embed the question; searching by words only")
            return []
        # A personal library fits in memory: ten thousand chunks of 512 floats is 20 MB.
        matrix = np.frombuffer(b"".join(vector for _, vector in rows), dtype=np.float32).reshape(len(rows), -1)
        scores = matrix @ target
        best = np.argsort(-scores)[:CANDIDATES]
        return [rows[index][0] for index in best if scores[index] >= MIN_SIMILARITY]
