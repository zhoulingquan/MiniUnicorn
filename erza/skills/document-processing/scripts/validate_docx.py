#!/usr/bin/env python3
"""校验 .docx 文件结构。

用法:
    python validate_docx.py input.docx

检查项:
- 是否为有效 ZIP
- [Content_Types].xml 存在且可解析
- word/document.xml 存在且可解析
- word/_rels/document.xml.rels 存在
- 基本命名空间正确
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from xml.dom import minidom


def validate(docx_path: Path) -> bool:
    """校验 .docx 结构,返回是否通过。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not docx_path.exists():
        print(f"错误:文件不存在: {docx_path}", file=sys.stderr)
        return False

    # 检查 ZIP
    try:
        zf = zipfile.ZipFile(docx_path, "r")
    except zipfile.BadZipFile:
        print(f"错误:不是有效的 ZIP 文件: {docx_path}", file=sys.stderr)
        return False

    with zf:
        names = set(zf.namelist())

        # 必要文件
        required_files = {
            "[Content_Types].xml": "内容类型声明",
            "word/document.xml": "主文档",
        }
        for path, desc in required_files.items():
            if path not in names:
                errors.append(f"缺少 {desc}: {path}")

        # 建议文件
        recommended = {
            "word/_rels/document.xml.rels": "关系文件",
            "word/styles.xml": "样式表",
            "_rels/.rels": "根关系",
        }
        for path, desc in recommended.items():
            if path not in names:
                warnings.append(f"缺少 {desc}: {path}")

        # 检查关键 XML 可解析
        for xml_path in ["[Content_Types].xml", "word/document.xml"]:
            if xml_path in names:
                try:
                    data = zf.read(xml_path)
                    minidom.parseString(data)
                except Exception as e:
                    errors.append(f"{xml_path} XML 解析失败: {e}")

        # 检查 document.xml 命名空间
        if "word/document.xml" in names:
            try:
                data = zf.read("word/document.xml").decode("utf-8")
                if "w:document" not in data and "w:" not in data:
                    warnings.append("document.xml 可能缺少 Word 命名空间 (xmlns:w)")
                if "w:body" not in data:
                    warnings.append("document.xml 可能缺少 <w:body>")
            except Exception:
                pass

    # 输出
    if warnings:
        for w in warnings:
            print(f"⚠️  {w}")
    if errors:
        for e in errors:
            print(f"❌ {e}")
        print(f"\n校验失败: {len(errors)} 个错误")
        return False

    print(f"✅ 校验通过: {docx_path}")
    if warnings:
        print(f"   ({len(warnings)} 个警告)")
    return True


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    docx_path = Path(sys.argv[1]).resolve()
    sys.exit(0 if validate(docx_path) else 1)


if __name__ == "__main__":
    main()
