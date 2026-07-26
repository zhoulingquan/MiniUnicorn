---
name: document-processing
description: "创建、编辑、转换、分析文档(PDF/Word/Excel/PowerPoint)。触发场景:用户要求创建/编辑/转换 .docx/.xlsx/.pptx/.pdf 文件,提取表格或图片,做格式转换(.docx→PDF/.xlsx→CSV),操作 Word 修订追踪与批注,或从文档中提取结构化数据。不用于纯文本读取(直接用 read_file 工具即可)。"
metadata: {"miniunicorn":{"emoji":"📄"}}
---

# 文档处理

创建、编辑、转换、分析 PDF/Word/Excel/PowerPoint 文档。所有脚本均用纯 Python 实现,无需安装外部 CLI。

## 读取/分析

**直接用 `read_file` 工具** — 它已内置支持:
- `.pdf` (pypdf)、`.docx` (python-docx)、`.xlsx` (openpyxl)、`.pptx` (python-pptx)
- `.txt/.md/.csv/.json/.xml/.html/.yaml/.toml/.ini/.cfg/.log`
- 图片(返回视觉块,由视觉模型处理)

无需调用本 skill 的脚本即可读取文档内容。

## 创建新文档

### Word (.docx)

```bash
python {baseDir}/scripts/create_docx.py output.docx --title "标题" --paragraphs "第一段" "第二段"
```

支持参数:`--heading2`、`--bullet`、`--table`、`--page-break`。

**创建复杂文档**(多级标题/表格/页码/页眉):先读取 `references/docx_advanced.md` 了解 python-docx 的高级用法。

### Excel (.xlsx)

```bash
python {baseDir}/scripts/create_xlsx.py output.xlsx --sheet "Sheet1" --data '[["姓名","年龄"],["张三",25],["李四",30]]'
```

### PowerPoint (.pptx)

```bash
python {baseDir}/scripts/create_pptx.py output.pptx --title "演示标题" --slides '[{"title":"页1","content":"内容1"},{"title":"页2","content":"内容2"}]'
```

## 编辑现有文档

### Word 编辑三步流程(unpack → 改 XML → pack)

```bash
# 步骤1:解包为 XML
python {baseDir}/scripts/unpack_docx.py input.docx unpacked/

# 步骤2:用 edit_file 工具修改 unpacked/word/document.xml
# (用 Edit 工具直接做字符串替换,不要写 Python 脚本)

# 步骤3:重新打包
python {baseDir}/scripts/pack_docx.py unpacked/ output.docx --original input.docx
```

**为什么用 unpack/pack 而不是 python-docx?** python-docx 不支持修订追踪(`<w:ins>/<w:del>`)、批注(`<w:comment>`)、复杂 OOXML 结构。直接编辑 XML 可以精确控制这些。

### 修订追踪(Tracked Changes)

在 `<w:r>` 中插入/删除文本时,用以下 XML 模式:

```xml
<!-- 插入 -->
<w:ins w:id="1" w:author="miniunicorn" w:date="2026-07-16T00:00:00Z">
  <w:r><w:t>新文本</w:t></w:r>
</w:ins>

<!-- 删除 -->
<w:del w:id="2" w:author="miniunicorn" w:date="2026-07-16T00:00:00Z">
  <w:r><w:delText>旧文本</w:delText></w:r>
</w:del>
```

**关键规则**:
- `<w:del>` 内必须用 `<w:delText>` 而非 `<w:t>`
- `w:id` 必须全局唯一(递增整数即可)
- 保留原 `<w:rPr>` 格式属性到新的 run 中
- 删除整段时,在 `<w:pPr><w:rPr>` 内加 `<w:del/>` 标记段落合并

**接受所有修订**(生成干净版本):
```bash
python {baseDir}/scripts/accept_changes.py input.docx output.docx
```

**添加批注**:
```bash
python {baseDir}/scripts/add_comment.py input.docx output.docx --paragraph 0 --text "批注内容"
```

## 格式转换

```bash
# .docx → .pdf
python {baseDir}/scripts/convert_format.py input.docx --to pdf

# .docx → .txt / .md
python {baseDir}/scripts/convert_format.py input.docx --to txt

# .xlsx → .csv (每个 sheet 一个文件)
python {baseDir}/scripts/convert_format.py input.xlsx --to csv

# .pptx → .txt
python {baseDir}/scripts/convert_format.py input.pptx --to txt
```

**注**:`.docx→.pdf` 纯 Python 方案仅支持基础排版(文字+表格+图片)。复杂排版(自定义字体/页眉页脚/精细样式)建议用户用 LibreOffice 手动转换。

## 表格提取

```bash
# 从 .docx 提取所有表格为 JSON
python {baseDir}/scripts/extract_tables.py input.docx --format json

# 从 .xlsx 提取所有 sheet 为 JSON
python {baseDir}/scripts/extract_tables.py input.xlsx --format json

# 从 .pdf 提取表格为 CSV
python {baseDir}/scripts/extract_tables.py input.pdf --format csv
```

## 校验 OOXML

```bash
python {baseDir}/scripts/validate_docx.py input.docx
```

检查:.docx 是否为有效 ZIP、`[Content_Types].xml` 是否存在、`word/document.xml` 是否存在且可解析。

## 单位换算参考(DXA)

| 单位 | 换算 |
|------|------|
| 1 英寸 | 1440 DXA |
| 1 厘米 | 567 DXA |
| 1 磅 (pt) | 20 DXA |
| US Letter | 12240 × 15840 DXA (8.5" × 11") |
| A4 | 11906 × 16838 DXA (210mm × 297mm) |

python-docx 中用 `Inches()`, `Cm()`, `Pt()` 辅助函数,无需手算 DXA。

## 关键陷阱

1. **不要用 Unicode 项目符号** — 用 `style='List Bullet'` / `'List Number'`
2. **表格需设双宽度** — `table.columns[i].width` 和 `cell.width` 都要设
3. **图片必须指定 `type` 参数**
4. **`<w:t>` 含前后空格时需加 `xml:space="preserve"`**
5. **智能引号**:专业排版用 `&#x201C;` `&#x201D;` `&#x2018;` `&#x2019;` 而非直引号
6. **python-docx 限制**:不支持修订追踪、批注、复杂页眉页脚 — 这些场景用 unpack/pack

## 高级主题

需要深入了解时,用 `read_file` 读取:
- `references/docx_advanced.md` — python-docx 高级用法(多级目录/页眉页脚/分节/图片)
- `references/ooxml_reference.md` — OOXML XML 模式参考(段落/表格/样式/修订)
