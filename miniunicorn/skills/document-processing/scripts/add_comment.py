#!/usr/bin/env python3
"""为 Word 文档添加批注(Comment)。

用法:
    python add_comment.py input.docx output.docx --paragraph 0 --text "这是批注"
    python add_comment.py input.docx output.docx --paragraph 2 --text "批注" --author "张三"

注:此脚本实现基础的批注功能。对于复杂批注(回复/范围标记),
建议用 unpack → 手动编辑 XML → pack 流程。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import zipfile
from pathlib import Path


def add_comment(
    input_path: Path,
    output_path: Path,
    *,
    paragraph_index: int,
    text: str,
    author: str = "miniunicorn",
) -> None:
    """在指定段落添加批注。"""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp) / "unpacked"
        tmp_dir.mkdir()

        # 解包
        with zipfile.ZipFile(input_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = tmp_dir / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(info.filename))

        word_dir = tmp_dir / "word"
        document_xml = word_dir / "document.xml"
        comments_xml = word_dir / "comments.xml"

        if not document_xml.exists():
            print("错误:word/document.xml 不存在", file=sys.stderr)
            sys.exit(1)

        # 读取 document.xml
        doc_content = document_xml.read_text(encoding="utf-8")

        # 找到第 N 个 <w:p> 段落
        p_tag = "<w:p "
        p_index = doc_content.find(p_tag)
        if p_index == -1:
            p_tag = "<w:p>"
            p_index = doc_content.find(p_tag)

        for _ in range(paragraph_index):
            p_index = doc_content.find(p_tag, p_index + 1)
            if p_index == -1:
                print(f"错误:找不到第 {paragraph_index} 个段落(只有少量段落)", file=sys.stderr)
                sys.exit(1)

        # 在该段落开头插入 commentRangeStart,末尾插入 commentRangeEnd + commentReference
        p_end = doc_content.find("</w:p>", p_index)
        if p_end == -1:
            print(f"错误:段落 {paragraph_index} 结构异常", file=sys.stderr)
            sys.exit(1)

        comment_id = 0
        # 读取已有的最大 comment id
        if comments_xml.exists():
            try:
                existing = comments_xml.read_text(encoding="utf-8")
                import re

                ids = [int(m) for m in re.findall(r'w:id="(\d+)"', existing)]
                if ids:
                    comment_id = max(ids) + 1
            except Exception:
                pass

        # 插入标记
        range_start = f'<w:commentRangeStart w:id="{comment_id}"/>'
        range_end = f'<w:commentRangeEnd w:id="{comment_id}"/>'
        ref = f'<w:r><w:rPr><w:rStyle w:val="CommentReference"/></w:rPr><w:commentReference w:id="{comment_id}"/></w:r>'

        # 在段落第一个 <w:r> 之前插入 rangeStart,在 </w:p> 之前插入 rangeEnd + ref
        # 简化:直接在 <w:p ...> 之后插入 rangeStart,在 </w:p> 之前插入
        p_tag_end = doc_content.find(">", p_index) + 1
        new_doc = (
            doc_content[:p_tag_end]
            + range_start
            + doc_content[p_tag_end:p_end]
            + range_end
            + ref
            + doc_content[p_end:]
        )
        document_xml.write_text(new_doc, encoding="utf-8")

        # 创建/更新 comments.xml
        comment_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:comment w:id="{comment_id}" w:author="{author}" w:date="2026-07-16T00:00:00Z" w:initials="{author[:1]}">
    <w:p>
      <w:r>
        <w:t>{text}</w:t>
      </w:r>
    </w:p>
  </w:comment>
</w:comments>"""

        if comments_xml.exists():
            # 合并到已有 comments.xml(简单追加)
            existing = comments_xml.read_text(encoding="utf-8")
            new_comment_block = f"""  <w:comment w:id="{comment_id}" w:author="{author}" w:date="2026-07-16T00:00:00Z" w:initials="{author[:1]}">
    <w:p>
      <w:r>
        <w:t>{text}</w:t>
      </w:r>
    </w:p>
  </w:comment>
</w:comments>"""
            existing = existing.replace("</w:comments>", new_comment_block)
            comments_xml.write_text(existing, encoding="utf-8")
        else:
            comments_xml.write_text(comment_xml, encoding="utf-8")

        # 更新 [Content_Types].xml 添加 comments 的 override
        ct_path = tmp_dir / "[Content_Types].xml"
        if ct_path.exists():
            ct = ct_path.read_text(encoding="utf-8")
            if "comments.xml" not in ct:
                override = '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>'
                ct = ct.replace("</Types>", f"{override}</Types>")
                ct_path.write_text(ct, encoding="utf-8")

        # 重新打包
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in tmp_dir.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(tmp_dir).as_posix()
                    zf.writestr(rel, path.read_bytes())

    print(f"已添加批注: {output_path} (段落 {paragraph_index})")


def main() -> None:
    parser = argparse.ArgumentParser(description="添加 Word 批注")
    parser.add_argument("input", help="输入 .docx")
    parser.add_argument("output", help="输出 .docx")
    parser.add_argument("--paragraph", type=int, required=True, help="段落索引(0 开始)")
    parser.add_argument("--text", required=True, help="批注内容")
    parser.add_argument("--author", default="miniunicorn", help="作者名")
    args = parser.parse_args()

    add_comment(
        Path(args.input).resolve(),
        Path(args.output).resolve(),
        paragraph_index=args.paragraph,
        text=args.text,
        author=args.author,
    )


if __name__ == "__main__":
    main()
