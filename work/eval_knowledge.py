"""How often does the knowledge base find the right excerpt? Run before and after any retrieval change.

Questions live in data/knowledge_eval.json (local, not in git), one object per question:
  {"question": "住宿标准是多少", "file": "差旅制度.md", "expect": "500"}
"file" is the document that holds the answer; "expect", optional, is text the excerpt must contain.

  python work/eval_knowledge.py            score the real library against your questions
  python work/eval_knowledge.py --init     write an example question file to start from
  python work/eval_knowledge.py --demo     score a built-in sample library, no files needed
"""
import argparse
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from knowledge import KnowledgeBase  # noqa: E402

QUESTIONS = ROOT / "data" / "knowledge_eval.json"
TOP = 5
EXAMPLE = [{"question": "住宿标准是多少", "file": "差旅制度.md", "expect": "500"},
           {"question": "年假有几天", "file": "员工手册.pdf", "expect": "15"}]
DEMO_FILES = {
    "差旅制度.md": "# 差旅制度\n\n## 一、适用范围\n全体正式员工。\n\n## 二、住宿标准\n一线城市 500 元，其他城市 350 元。\n\n## 三、交通\n"
                  "高铁二等座，飞机经济舱。市内打车凭票报销。\n\n## 四、报销时限\n出差结束后七天内提交发票。",
    "员工手册.md": "第一章 假期\n\n一、年假\n入职满一年后每年 15 天。\n\n二、病假\n需要医院证明。\n\n第二章 考勤\n\n一、上班时间\n上午九点到下午六点。",
    "会议记录.md": "周一例会讨论了预算和进度。客户需求变化较大，需要重新评估方案。",
}
DEMO_QUESTIONS = [
    {"question": "住宿标准是多少", "file": "差旅制度.md", "expect": "500"},
    {"question": "年假几天", "file": "员工手册.md", "expect": "15"},
    {"question": "几点上班", "file": "员工手册.md", "expect": "九点"},
    {"question": "发票什么时候交", "file": "差旅制度.md", "expect": "七天"},
    {"question": "坐飞机什么舱位", "file": "差旅制度.md", "expect": "经济舱"},
    {"question": "打车能报销吗", "file": "差旅制度.md", "expect": "打车"},
    {"question": "车费能报吗", "file": "差旅制度.md", "expect": "打车"},
    {"question": "请病假要什么材料", "file": "员工手册.md", "expect": "医院证明"},
    {"question": "例会讨论了什么", "file": "会议记录.md", "expect": "预算"},
    {"question": "出差住酒店的上限", "file": "差旅制度.md", "expect": "500"},
]


def score(base, questions, semantic=True):
    """Rank of the first excerpt from the right file that contains the expected text, or None."""
    rows = []
    for item in questions:
        hits = base.search(item["question"], limit=TOP, semantic=semantic)[1]
        rank = next((number for number, hit in enumerate(hits, 1)
                     if hit["file"] == item["file"] and item.get("expect", "") in hit["text"]), None)
        rows.append((item["question"], rank))
    return rows


def compare(base, questions):
    """Same library, same questions, with and without the meaning-based list."""
    base.sync(embed_seconds=None)
    print("== 只用关键词 ==")
    report(score(base, questions, semantic=False))
    if base.semantic():
        print("\n== 关键词 + 向量（混合召回） ==")
        report(score(base, questions, semantic=True))
    else:
        print("\n没有找到向量模型，跳过混合召回。")


def report(rows):
    for question, rank in rows:
        print(f"{'第 %d 名' % rank if rank else '未命中':<6}  {question}")
    total = len(rows)
    hit1 = sum(rank == 1 for _, rank in rows)
    hit5 = sum(rank is not None for _, rank in rows)
    mrr = sum(1 / rank for _, rank in rows if rank) / total
    print(f"\n{total} 题：第一名命中 {hit1}（{hit1 / total:.0%}），前 {TOP} 命中 {hit5}（{hit5 / total:.0%}），MRR {mrr:.2f}")


def main():
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    if args.init:
        if QUESTIONS.exists():
            print(f"已存在，没有覆盖：{QUESTIONS}")
            return 1
        QUESTIONS.parent.mkdir(parents=True, exist_ok=True)
        QUESTIONS.write_text(json.dumps(EXAMPLE, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入示例，请改成你自己的问题：{QUESTIONS}")
        return 0
    if args.demo:
        folder = Path(tempfile.mkdtemp())
        for name, text in DEMO_FILES.items():
            (folder / "docs").mkdir(exist_ok=True)
            (folder / "docs" / name).write_text(text, encoding="utf-8")
        import tools
        compare(KnowledgeBase(folder / "docs", folder / "index.db", tools.knowledge.embedder), DEMO_QUESTIONS)
        return 0
    if not QUESTIONS.exists():
        print(f"还没有问题集。先运行 --init，再编辑 {QUESTIONS}")
        return 1
    import tools
    compare(tools.knowledge, json.loads(QUESTIONS.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
