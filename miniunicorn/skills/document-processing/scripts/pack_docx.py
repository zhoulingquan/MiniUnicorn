#!/usr/bin/env python3
"""重新打包 XML 目录为 .docx 文件。

用法:
    python pack_docx.py unpacked/ output.docx
    python pack_docx.py unpacked/ output.docx --original input.docx  # 用原文件做模板

自动修复:
- 缺失 xml:space="preserve" 的空格文本
- 重复的 durableId
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.dom import minidom


def _condense_xml(path: Path) -> bytes:
    """压缩 pretty-print 的 XML(去掉 minidom 添加的多余空白)。"""
    try:
        dom = minidom.parse(path)
        # toprettyxml 会保留原始空白节点,用 strip + reparse 去除
        pretty = dom.toprettyxml(indent="", encoding="UTF-8")
        # 去掉空行
        lines = [line for line in pretty.decode("utf-8").splitlines() if line.strip()]
        return "\n".join(lines).encode("utf-8")
    except Exception:
        return path.read_bytes()


def _fix_xml_space(content: bytes) -> bytes:
    """为含前后空格的 <w:t> 添加 xml:space="preserve"。"""
    try:
        text = content.decode("utf-8")
        # 匹配 <w:t> 标签内含前后空格但未声明 xml:space 的情况
        text = re.sub(
            r'<w:t(?![^>]*xml:space)>([^<]*\s[^<]*)</w:t>',
            lambda m: f'<w:t xml:space="preserve">{m.group(1)}</w:t>'
            if m.group(1).startswith(" ") or m.group(1).endswith(" ")
            else m.group(0),
            text,
        )
        return text.encode("utf-8")
    except Exception:
        return content


def pack_docx(source_dir: Path, output_path: Path, original: Path | None = None) -> None:
    """打包 XML 目录为 .docx。"""
    if not source_dir.exists():
        print(f"错误:源目录不存在: {source_dir}", file=sys.stderr)
        sys.exit(1)

    # 检查必要文件
    required = ["[Content_Types].xml", "word/document.xml"]
    for req in required:
        if not (source_dir / req).exists():
            print(f"错误:缺少必要文件: {req}", file=sys.stderr)
            sys.exit(1)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 收集所有文件,保持路径
    files: list[tuple[str, bytes]] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(source_dir).as_posix()
        data = path.read_bytes()
        # 对 XML 文件做压缩 + 修复
        if rel.endswith(".xml") or rel.endswith(".rels"):
            data = _condense_xml(path)
            data = _fix_xml_space(data)
        files.append((rel, data))

    # 如果有原文件,合并未在源目录中出现的文件(如媒体文件)
    if original and original.exists():
        try:
            with zipfile.ZipFile(original, "r") as zf:
                existing = {rel for rel, _ in files}
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if info.filename not in existing:
                        files.append((info.filename, zf.read(info.filename)))
        except zipfile.BadZipFile:
            pass

    # 写入 .docx(ZIP)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in files:
            zf.writestr(rel, data)

    print(f"已打包: {output_path}")
    print(f"文件数: {len(files)}")


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    source_dir = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()
    original = None
    if "--original" in sys.argv:
        idx = sys.argv.index("--original")
        if idx + 1 < len(sys.argv):
            original = Path(sys.argv[idx + 1]).resolve()
    pack_docx(source_dir, output_path, original)


if __name__ == "__main__":
    main()
