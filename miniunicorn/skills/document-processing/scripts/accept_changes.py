#!/usr/bin/env python3
"""接受 Word 文档中的所有修订(Tracked Changes)。

用法:
    python accept_changes.py input.docx output.docx

通过 unpack → 移除 <w:ins> 标签(保留内容)→ 移除 <w:del> 标签(连同内容)→ pack 实现。
"""

from __future__ import annotations

import re
import sys
import tempfile
import zipfile
from pathlib import Path


def _accept_in_document_xml(xml_text: str) -> str:
    """在 document.xml 中接受所有修订。"""
    # 1. 移除 <w:ins ...> 和 </w:ins> 标签,保留内部内容
    xml_text = re.sub(r'<w:ins\b[^>]*>', '', xml_text)
    xml_text = xml_text.replace('</w:ins>', '')
    # 2. 移除整个 <w:del>...</w:del> 块(连同内容)
    xml_text = re.sub(r'<w:del\b[^>]*>.*?</w:del>', '', xml_text, flags=re.DOTALL)
    # 3. 移除段落属性中的 <w:del/> 标记(段落合并标记)
    xml_text = re.sub(r'<w:del\s*/>', '', xml_text)
    # 4. 移除 <w:rPr><w:del .../></w:rPr> 中的 del 标记
    xml_text = re.sub(r'<w:del\b[^/>]*/>', '', xml_text)
    return xml_text


def accept_changes(input_path: Path, output_path: Path) -> None:
    """接受所有修订并保存。"""
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

        # 处理所有 XML 文件
        for xml_file in tmp_dir.rglob("*.xml"):
            try:
                content = xml_file.read_text(encoding="utf-8")
                new_content = _accept_in_document_xml(content)
                xml_file.write_text(new_content, encoding="utf-8")
            except Exception:
                pass

        # 重新打包
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in tmp_dir.rglob("*"):
                if path.is_file():
                    rel = path.relative_to(tmp_dir).as_posix()
                    zf.writestr(rel, path.read_bytes())

    print(f"已接受所有修订: {output_path}")


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    input_path = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()
    if not input_path.exists():
        print(f"错误:文件不存在: {input_path}", file=sys.stderr)
        sys.exit(1)
    accept_changes(input_path, output_path)


if __name__ == "__main__":
    main()
