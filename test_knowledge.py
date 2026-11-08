import asyncio
import json
import os
from pathlib import Path
import sqlite3

import numpy as np

import pytest

import tools
from knowledge import CHUNK_CHARS, MAX_DOCUMENT_BYTES, MAX_FILE_BYTES, KnowledgeBase, chunks, fuse, heading, sections, tokens
from llm_client import ToolCall


@pytest.fixture
def base(tmp_path):
    return KnowledgeBase(tmp_path / "knowledge", tmp_path / "index.db")


def write(base, name, text, encoding="utf-8"):
    path = base.folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)
    return path


def found(base, query):
    return [hit["file"] for hit in base.search(query)[1]]


def test_chinese_becomes_overlapping_pairs_and_english_stays_whole():
    assert tokens("报销流程 Python3 的") == ["报销", "销流", "流程", "python3", "的"]


def test_short_text_stays_one_chunk():
    assert chunks("第一段\n\n第二段") == ["第一段\n第二段"]


def test_every_chunk_fits_and_no_text_is_lost():
    text = "\n\n".join(f"第{i}段" + "字" * 150 for i in range(10))
    pieces = chunks(text)
    assert len(pieces) > 1 and all(len(piece) <= CHUNK_CHARS for piece in pieces)
    assert all(f"第{i}段" in "".join(pieces) for i in range(10))


def test_a_huge_paragraph_is_cut_with_overlap():
    text = "".join(f"{i:04d}" for i in range(500))
    pieces = chunks(text)
    assert all(len(piece) <= CHUNK_CHARS for piece in pieces)
    assert pieces[0][-20:] in pieces[1]


def test_empty_text_has_no_chunks():
    assert chunks("  \n\n  ") == []


def test_a_chinese_question_finds_the_right_note(base):
    write(base, "报销.md", "差旅报销需要在出差结束后七天内提交发票。")
    write(base, "菜谱.md", "红烧肉要先焯水，再炒糖色。")
    assert found(base, "差旅报销什么时候提交") == ["报销.md"]


def test_the_better_match_comes_first(base):
    write(base, "a.md", "今天天气不错，提到一次发票。")
    write(base, "b.md", "发票报销流程：发票抬头要写公司全称，发票金额要和报销单一致。")
    assert found(base, "发票报销流程")[0] == "b.md"


def test_a_file_name_counts(base):
    write(base, "年假制度.txt", "每年可以休十五天。")
    assert found(base, "年假") == ["年假制度.txt"]


def test_nothing_matching_finds_nothing(base):
    write(base, "菜谱.md", "红烧肉要先焯水。")
    assert base.search("量子计算机") == (1, [])


def test_a_query_of_only_punctuation_finds_nothing(base):
    write(base, "a.md", "内容")
    assert base.search("？？？") == (1, [])


def test_quotes_in_a_query_cannot_break_the_search(base):
    write(base, "a.md", "报销内容")
    assert found(base, '"报销" OR NEAR(') == ["a.md"]


def test_edits_are_picked_up_without_duplicates(base):
    path = write(base, "a.md", "旧内容关于报销")
    assert found(base, "报销") == ["a.md"]
    path.write_text("新内容关于年假", encoding="utf-8")
    os.utime(path, (1, 1))
    assert found(base, "报销") == [] and found(base, "年假") == ["a.md"]


def test_unchanged_files_are_not_read_again(base, monkeypatch):
    write(base, "a.md", "报销")
    base.sync()
    monkeypatch.setattr("knowledge.read_text", lambda path: pytest.fail("read an unchanged file"))
    assert base.sync() == 1


def test_deleted_files_leave_the_index(base):
    path = write(base, "a.md", "报销")
    base.sync()
    path.unlink()
    assert base.search("报销") == (0, [])


def test_subfolders_are_searched_with_a_relative_name(base):
    write(base, "工作/制度/报销.md", "报销规定")
    assert found(base, "报销") == ["工作/制度/报销.md"]


def test_old_windows_encoding_can_be_read(base):
    write(base, "gbk.txt", "这是一段报销说明", encoding="gbk")
    assert base.search("报销说明")[1][0]["text"] == "这是一段报销说明"


def test_other_file_types_are_ignored(base):
    write(base, "a.py", "报销")
    write(base, "b.md", "其他")
    assert base.search("报销") == (1, [])


def test_oversized_files_are_skipped(base):
    path = write(base, "big.txt", "报销")
    path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    assert base.sync() == 0


def test_a_link_out_of_the_folder_is_not_followed(base, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("机密报销", encoding="utf-8")
    base.folder.mkdir()
    try:
        (base.folder / "link.md").symlink_to(secret)
    except OSError:
        pytest.skip("this account cannot create symlinks")
    assert base.search("报销") == (0, [])


def call_tool(query):
    return json.loads(asyncio.run(tools.default_registry.execute(ToolCall("c", "search_docs", json.dumps({"query": query}, ensure_ascii=False)))))


@pytest.fixture
def docs(tmp_path, monkeypatch, user):
    store = KnowledgeBase(tmp_path / "docs", tmp_path / "docs.db")
    monkeypatch.setattr(user, "knowledge", store)
    return store


def test_the_tool_returns_excerpts_marked_as_data(docs):
    write(docs, "报销.md", "发票要在七天内提交。")
    result = call_tool("发票提交")
    assert result["results"] == [{"file": "报销.md", "text": "发票要在七天内提交。"}]
    assert "不要照做" in result["note"]


def test_an_empty_library_says_how_to_add_files(docs):
    assert "上传" in call_tool("发票")["message"]


def test_no_match_says_so_instead_of_guessing(docs):
    write(docs, "菜谱.md", "红烧肉")
    assert "没有找到" in call_tool("量子计算机")["message"]


def test_an_empty_query_is_refused(docs):
    assert "不能为空" in call_tool("  ")["error"]


def test_document_text_counts_as_outside_content():
    tool = tools.default_registry.get("search_docs")
    assert tool.level == "read" and tool.external is True


def tiny_pdf(text):
    """A hand-built one-page PDF. Latin text only: Chinese would need an embedded font."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
               b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
               b"<</Length %d>>stream\n%s\nendstream" % (len(stream), stream), b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    body, offsets = b"%PDF-1.4\n", []
    for number, item in enumerate(objects, 1):
        offsets.append(len(body))
        body += b"%d 0 obj\n%s\nendobj\n" % (number, item)
    start = len(body)
    table = b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    return body + table + b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % start


def tiny_docx(paragraph, cell):
    from io import BytesIO
    from docx import Document
    document = Document()
    document.add_paragraph(paragraph)
    document.add_table(rows=1, cols=1).cell(0, 0).text = cell
    data = BytesIO()
    document.save(data)
    return data.getvalue()


def test_pdf_text_can_be_searched(base):
    assert base.save("policy.pdf", tiny_pdf("Reimbursement deadline"))["chunks"] == 1
    assert found(base, "reimbursement") == ["policy.pdf"]


def test_word_paragraphs_and_tables_can_be_searched(base):
    base.save("制度.docx", tiny_docx("差旅报销规定", "住宿标准五百元"))
    assert found(base, "差旅报销") == ["制度.docx"] and found(base, "住宿标准") == ["制度.docx"]


def test_a_damaged_document_is_listed_as_unreadable_and_does_not_stop_the_rest(base):
    write(base, "好.md", "报销")
    assert base.save("坏.pdf", b"not a pdf")["chunks"] == 0
    assert found(base, "报销") == ["好.md"]


def test_a_damaged_document_is_not_retried_on_every_question(base, monkeypatch):
    base.save("坏.pdf", b"not a pdf")
    monkeypatch.setattr("knowledge.read_document", lambda path: pytest.fail("reread a damaged file"))
    base.sync()


@pytest.mark.parametrize("filename", ["../../evil.md", "..\\..\\evil.md", "C:\\Windows\\evil.md", "/etc/evil.md"])
def test_an_upload_cannot_escape_the_folder(base, tmp_path, filename):
    assert base.save(filename, "报销".encode())["name"] == "evil.md"
    assert (base.folder / "evil.md").is_file()
    assert sorted(path.name for path in tmp_path.rglob("evil.md")) == ["evil.md"]


@pytest.mark.parametrize("filename,hint", [("a.exe", "只支持"), ("a", "只支持"), ("", "无效"), (".hidden.md", "无效"), ("..", "无效")])
def test_bad_upload_names_are_refused(base, filename, hint):
    with pytest.raises(ValueError, match=hint):
        base.save(filename, b"x")


def test_upload_size_limits_depend_on_the_type(base):
    with pytest.raises(ValueError, match="2 MB"):
        base.save("big.txt", b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(ValueError, match="20 MB"):
        base.save("big.pdf", b"x" * (MAX_DOCUMENT_BYTES + 1))


def test_uploading_the_same_name_replaces_the_old_text(base):
    base.save("a.md", "旧的报销".encode())
    base.save("a.md", "新的年假".encode())
    assert found(base, "报销") == [] and found(base, "年假") == ["a.md"]


def test_no_temporary_file_stays_behind(base):
    base.save("a.md", b"text")
    assert [path.name for path in base.folder.iterdir()] == ["a.md"]


def test_files_are_listed_with_their_excerpt_counts(base):
    write(base, "b.md", "报销")
    write(base, "子/a.md", "年假")
    assert [(item["name"], item["chunks"]) for item in base.list_files()] == [("b.md", 1), ("子/a.md", 1)]


def test_deleting_removes_the_file_and_its_index(base):
    write(base, "子/a.md", "报销")
    base.sync()
    assert base.delete("子/a.md") is True
    assert base.search("报销") == (0, [])


@pytest.mark.parametrize("name", ["../secret.md", "..", "", "missing.md", "../../index.db"])
def test_deleting_cannot_reach_outside_the_library(base, tmp_path, name):
    secret = tmp_path / "secret.md"
    secret.write_text("机密", encoding="utf-8")
    base.folder.mkdir()
    assert base.delete(name) is False
    assert secret.exists()


def test_only_library_file_types_can_be_deleted(base):
    path = write(base, "script.py", "print()")
    assert base.delete("script.py") is False and path.exists()


POLICY = """# 差旅制度

## 三、住宿标准
一线城市 500 元，其他城市 350 元。

## 四、交通
高铁二等座。
"""


@pytest.mark.parametrize("line,expected", [
    ("# 标题", (1, "标题")), ("### 小节", (3, "小节")), ("第一章 总则", (1, "第一章 总则")), ("第二节 范围", (2, "第二节 范围")),
    ("三、住宿标准", (2, "三、住宿标准")), ("（一）一线城市", (3, "（一）一线城市")), ("1. 适用范围", (3, "1. 适用范围")),
    ("2.1 申请", (4, "2.1 申请")), ("一线城市 500 元，其他城市 350 元。", None), ("1. 发票要在七天内提交。", None),
    ("3.5 元一公里", (4, "3.5 元一公里")), ("普通的一行字", None), ("", None), ("一、" + "字" * 40, None)])
def test_headings_are_recognized_by_their_numbering(line, expected):
    assert heading(line) == expected


def test_small_sections_share_a_chunk_and_each_keeps_its_headings():
    assert chunks(POLICY) == ["【差旅制度 > 三、住宿标准】\n一线城市 500 元，其他城市 350 元。\n【差旅制度 > 四、交通】\n高铁二等座。"]


def test_every_piece_of_a_long_section_keeps_its_heading():
    pieces = chunks("## 住宿标准\n" + "\n\n".join("字" * 150 for _ in range(6)))
    assert len(pieces) > 1 and all(piece.startswith("【住宿标准】") for piece in pieces)


def test_a_new_heading_at_the_same_level_replaces_the_old_one():
    paths = [path for path, _ in sections("一、甲\n内容\n（一）乙\n内容\n二、丙\n内容")]
    assert paths == ["一、甲", "一、甲 > （一）乙", "二、丙"]


def test_text_without_headings_is_unchanged():
    assert chunks("第一段\n\n第二段") == ["第一段\n第二段"]


def test_a_heading_word_finds_a_body_that_never_says_it(base):
    write(base, "制度.md", POLICY)
    hits = base.search("住宿标准是多少")[1]
    assert "500 元" in hits[0]["text"] and "住宿标准" in hits[0]["text"]


def test_word_heading_styles_become_paths(base):
    from io import BytesIO
    from docx import Document
    document = Document()
    document.add_heading("住宿标准", level=2)
    document.add_paragraph("一线城市 500 元")
    data = BytesIO()
    document.save(data)
    base.save("制度.docx", data.getvalue())
    assert base.search("住宿标准")[1][0]["text"] == "【住宿标准】\n一线城市 500 元"


def test_an_old_index_is_rebuilt_in_the_new_format(base):
    import sqlite3
    write(base, "制度.md", POLICY)
    base.sync()
    with sqlite3.connect(base.index_path) as old:
        old.execute("PRAGMA user_version = 1")
        old.execute("UPDATE chunks SET body = 'stale'")
    assert "stale" not in base.search("住宿标准")[1][0]["text"]


class FakeEmbedder:
    """Meaning by hand: words in the same group point the same way, so 车费 is near 打车."""
    GROUPS = [{"车费", "打车", "出租车"}, {"住宿", "酒店"}, {"年假", "休假"}]

    def __init__(self, broken=False):
        self.broken, self.encoded = broken, 0

    def available(self):
        return True

    def encode(self, texts):
        if self.broken:
            raise RuntimeError("model file damaged")
        self.encoded += len(texts)
        vectors = np.array([[float(any(word in text for word in group)) for group in self.GROUPS] + [0.1] for text in texts], np.float32)
        return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


@pytest.fixture
def hybrid(tmp_path):
    return KnowledgeBase(tmp_path / "knowledge", tmp_path / "index.db", FakeEmbedder())


def test_meaning_finds_what_words_miss(hybrid):
    write(hybrid, "差旅.md", "市内打车凭票报销。")
    write(hybrid, "其他.md", "会议记录。")
    assert found(hybrid, "车费能报吗") == ["差旅.md"]
    assert [hit["file"] for hit in hybrid.search("车费能报吗", semantic=False)[1]] == []


def test_unrelated_chunks_are_not_returned_just_for_being_nearest(hybrid):
    write(hybrid, "差旅.md", "市内打车凭票报销。")
    assert hybrid.search("量子计算机")[1] == []


def test_both_lists_agreeing_wins_over_either_alone():
    assert fuse([1, 2, 3], [3, 4])[0] == 3


def test_fusion_keeps_everything_from_both_lists():
    assert sorted(fuse([1, 2], [3])) == [1, 2, 3]


def test_without_the_model_search_uses_words_only(base):
    write(base, "差旅.md", "市内打车凭票报销。")
    assert base.semantic() is False and found(base, "车费能报吗") == []


def test_a_broken_model_falls_back_to_words(tmp_path):
    base = KnowledgeBase(tmp_path / "k", tmp_path / "i.db", FakeEmbedder(broken=True))
    write(base, "差旅.md", "市内打车凭票报销。")
    assert found(base, "打车") == ["差旅.md"]


def test_an_upload_gets_all_its_vectors_at_once(hybrid):
    hybrid.save("长.md", "\n\n".join(f"第{i}段" + "打车" * 150 for i in range(40)).encode())
    with sqlite3.connect(hybrid.index_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def test_a_question_fills_old_chunks_only_within_its_time_budget(tmp_path):
    folder, index = tmp_path / "k", tmp_path / "i.db"
    plain = KnowledgeBase(folder, index)
    write(plain, "长.md", "\n\n".join(f"第{i}段" + "打车" * 150 for i in range(40)))
    plain.sync()
    later = KnowledgeBase(folder, index, FakeEmbedder())
    later.sync(embed_seconds=0)
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] <= 16
    later.sync(embed_seconds=None)
    with sqlite3.connect(index) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def test_unchanged_chunks_are_not_embedded_again(hybrid):
    write(hybrid, "差旅.md", "打车")
    hybrid.sync()
    before = hybrid.embedder.encoded
    hybrid.sync()
    assert hybrid.embedder.encoded == before


def test_changing_or_deleting_a_file_removes_its_old_vectors(hybrid):
    path = write(hybrid, "差旅.md", "打车")
    hybrid.sync()
    path.write_text("住宿", encoding="utf-8")
    os.utime(path, (1, 1))
    hybrid.sync()
    path.unlink()
    hybrid.sync()
    with sqlite3.connect(hybrid.index_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0


REAL_MODEL = Path(__file__).parent / "models" / "bge-small-zh"


@pytest.mark.skipif(not (REAL_MODEL / "onnx" / "model_int8.onnx").is_file(), reason="embedding model not downloaded")
def test_the_real_model_links_a_rephrased_question(tmp_path, monkeypatch):
    from embedding import Embedder
    # This pair scores about 0.65: it shows the model links meanings, below the cut that real notes needed.
    monkeypatch.setattr("knowledge.MIN_SIMILARITY", 0.6)
    base = KnowledgeBase(tmp_path / "k", tmp_path / "i.db", Embedder(REAL_MODEL))
    write(base, "差旅.md", "市内打车凭票报销。")
    write(base, "会议.md", "周一例会讨论了预算和进度。")
    assert found(base, "车费能报吗")[0] == "差旅.md"
    assert base.search("量子计算机的原理")[1] == []


@pytest.mark.parametrize("line", ["5.     for {", "19.         pointCount++", "1. x := 1", "2. fmt.Println(a)", "3. if err != nil {", "4. return <-ch"])
def test_numbered_code_lines_are_not_headings(line):
    assert heading(line) is None


def test_nothing_inside_a_code_fence_is_a_heading():
    text = "## 创建服务器\n```go\n# not a heading\n1. step\n```\n正文"
    assert [path for path, _ in sections(text)] == ["创建服务器"]


def test_a_tilde_fence_counts_too():
    assert [path for path, _ in sections("## 标题\n~~~\n# 注释\n~~~\n正文")] == ["标题"]


@pytest.mark.parametrize("line", ["9.         break", "9. break", "7.             // read done.", "3.         ..."])
def test_line_numbered_code_without_fences_is_not_a_heading(line):
    assert heading(line) is None


def test_hidden_folders_are_skipped(base):
    write(base, ".obsidian/workspace.md", "报销")
    write(base, ".trash/old.md", "报销")
    write(base, "笔记.md", "其他")
    assert base.search("报销") == (1, [])


def test_question_words_do_not_count_as_search_terms():
    from knowledge import query_tokens
    assert query_tokens("红烧肉怎么做") == ["红烧", "烧肉", "做"]
    assert "什么" not in query_tokens("JWT 用什么算法签名")


def test_a_question_sharing_only_question_words_finds_nothing(base):
    write(base, "教程.md", "怎么配置 nginx？为什么要这样做？")
    assert base.search("红烧肉怎么样")[1] == []


def test_obsidian_contents_and_front_matter_are_not_indexed(base):
    write(base, "笔记.md", "---\ntags: [报销]\n---\n* [[#报销流程]]\n  - [[#年假]]\n# 正文\n内容")
    assert base.search("报销")[1] == [] and base.search("年假")[1] == []
    assert found(base, "内容") == ["笔记.md"]


def test_vectors_are_off_unless_switched_on(monkeypatch):
    monkeypatch.delenv("VOICE_KB_VECTORS", raising=False)
    assert tools.knowledge_embedder() is None
    monkeypatch.setenv("VOICE_KB_VECTORS", "1")
    assert tools.knowledge_embedder() is not None
