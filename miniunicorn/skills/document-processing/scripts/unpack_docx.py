#!/usr/bin/env python3
"""解包 .docx 文件为 XML 目录,便于直接编辑 OOXML。

用法:
    python unpack_docx.py input.docx unpacked/

输出目录结构:
    unpacked/
    ├── [Content_Types].xml
    ├── _rels/
    │   └── .rels
    ├── word/
    │   ├── document.xml      ← 主要编辑目标
    │   ├── styles.xml
    │   ├── header1.xml
    │   └── ...
    └── docProps/
        └── ...
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from xml.dom import minidom


def unpack_docx(docx_path: Path, output_dir: Path) -> None:
    """解包 .docx 为 XML 文件,pretty-print 便于阅读编辑。"""
    if not docx_path.exists():
        print(f"错误:文件不存在: {docx_path}", file=sys.stderr)
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(docx_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = output_dir / info.filename
                target.parent.mkdir(parents=True, exist_ok=True)
                data = zf.read(info.filename)
                # 对 XML 文件做 pretty-print
                if info.filename.endswith(".xml") or info.filename.endswith(".rels"):
                    try:
                        dom = minidom.parseString(data)
                        pretty = dom.toprettyxml(indent="  ", encoding="UTF-8")
                        target.write_bytes(pretty)
                    except Exception:
                        # 非 XML 或解析失败,原样写出
                        target.write_bytes(data)
                else:
                    target.write_bytes(data)
    except zipfile.BadZipFile:
        print(f"错误:不是有效的 .docx 文件(ZIP 格式损坏): {docx_path}", file=sys.stderr)
        sys.exit(1)

    print(f"已解包到 {output_dir}/")
    print(f"主文档: {output_dir}/word/document.xml")
    print("编辑后用 pack_docx.py 重新打包。")


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    docx_path = Path(sys.argv[1]).resolve()
    output_dir = Path(sys.argv[2]).resolve()
    unpack_docx(docx_path, output_dir)


if __name__ == "__main__":
    main()
