# python-docx 高级用法

本文档涵盖 python-docx 的高级功能:多级目录、页眉页脚、分节、图片插入、样式定制。
当 `create_docx.py` 的基础参数无法满足需求时,参考本文档用 `exec` 工具执行自定义 Python 脚本。

## 目录

- [多级标题与目录(TOC)](#多级标题与目录toc)
- [页眉页脚](#页眉页脚)
- [分节(Section)](#分节section)
- [图片插入](#图片插入)
- [样式定制](#样式定制)
- [表格高级用法](#表格高级用法)

## 多级标题与目录(TOC)

```python
from docx import Document
from docx.shared import Pt

doc = Document()

# 添加多级标题
doc.add_heading("一级标题", level=1)
doc.add_heading("二级标题", level=2)
doc.add_heading("三级标题", level=3)

# 添加 TOC 字段(Word 打开时按 F9 更新)
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

paragraph = doc.add_paragraph()
run = paragraph.add_run()
fldChar = OxmlElement('w:fldChar')
fldChar.set(qn('w:fldCharType'), 'begin')
run._r.append(fldChar)

run = paragraph.add_run()
instrText = OxmlElement('w:instrText')
instrText.set(qn('xml:space'), 'preserve')
instrText.text = 'TOC \\o "1-3" \\h \\z \\u'
run._r.append(instrText)

run = paragraph.add_run()
fldChar = OxmlElement('w:fldChar')
fldChar.set(qn('w:fldCharType'), 'end')
run._r.append(fldChar)
```

**注意**:python-docx 不自动生成 TOC,只插入字段。用户在 Word 中打开后按 `F9` 更新才会显示。

## 页眉页脚

```python
from docx import Document
from docx.shared import Inches

doc = Document()
section = doc.sections[0]

# 页眉
header = section.header
header.paragraphs[0].text = "公司名称 | 机密"
header.paragraphs[0].alignment = 1  # 居中

# 页脚(带页码)
footer = section.footer
p = footer.paragraphs[0]
p.alignment = 2  # 右对齐

# 添加页码字段
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

run = p.add_run()
fldChar1 = OxmlElement('w:fldChar')
fldChar1.set(qn('w:fldCharType'), 'begin')
run._r.append(fldChar1)

run = p.add_run("页码: ")
instrText = OxmlElement('w:instrText')
instrText.text = "PAGE"
run._r.append(instrText)

run = p.add_run()
fldChar2 = OxmlElement('w:fldChar')
fldChar2.set(qn('w:fldCharType'), 'end')
run._r.append(fldChar2)
```

## 分节(Section)

```python
from docx.enum.section import WD_ORIENT

# 横向页面
section = doc.add_section()
section.orientation = WD_ORIENT.LANDSCAPE
new_width, new_height = section.page_height, section.page_width
section.page_width = new_width
section.page_height = new_height

# 不同首页页眉
section.different_first_page_header_footer = True
section.first_page_header.paragraphs[0].text = "封面页眉"

# 多节文档
doc.add_section()  # 新节,新页
```

## 图片插入

```python
from docx.shared import Inches, Cm, Pt

# 基本插入
doc.add_picture("image.png", width=Inches(4))

# 居中
last_paragraph = doc.paragraphs[-1]
last_paragraph.alignment = 1  # 居中

# 带说明文字
doc.add_paragraph("图 1: 示例图").alignment = 1
```

## 样式定制

```python
from docx.shared import Pt, RGBColor

# 修改 Normal 样式
style = doc.styles['Normal']
style.font.name = 'Arial'
style.font.size = Pt(12)
style.font.color.rgb = RGBColor(0, 0, 0)

# 创建自定义段落样式
custom = doc.styles.add_style('MyStyle', 1)  # 1 = WD_STYLE_TYPE.PARAGRAPH
custom.font.name = 'Arial'
custom.font.size = Pt(14)
custom.font.bold = True
custom.font.color.rgb = RGBColor(0x1F, 0x49, 0x7D)

# 应用样式
p = doc.add_paragraph("自定义样式文本", style='MyStyle')

# 中文字体设置
from docx.oxml.ns import qn
run = doc.add_paragraph().add_run("中文文本")
run.font.name = 'Arial'  # 西文字体
run._element.rPr.rFonts.set(qn('w:eastAsia'), '宋体')  # 中文字体
```

## 表格高级用法

```python
from docx.shared import Inches, Cm

# 创建表格并设样式
table = doc.add_table(rows=3, cols=3)
table.style = 'Table Grid'

# 设列宽(需要同时设列和单元格)
for i, width in enumerate([Inches(2), Inches(3), Inches(1)]):
    for cell in table.columns[i].cells:
        cell.width = width

# 合并单元格
cell_a = table.cell(0, 0)
cell_b = table.cell(0, 1)
merged = cell_a.merge(cell_b)
merged.text = "合并的标题"

# 单元格背景色
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

cell = table.cell(0, 0)
shading = OxmlElement('w:shd')
shading.set(qn('w:fill'), 'D5E8F0')
cell._tc.get_or_add_tcPr().append(shading)

# 单元格内多段落
cell = table.cell(0, 0)
cell.text = "第一行"
p = cell.add_paragraph("第二行")
```

## 常见陷阱

1. **`cell.width` 必须与 `column.width` 一致** — 否则渲染异常
2. **中文字体需设 `w:eastAsia`** — 仅设 `font.name` 不影响中文
3. **TOC 需手动更新** — python-docx 只插入字段,不生成目录内容
4. **页码需用字段** — `PAGE` 字段,不能直接写数字
5. **图片路径用绝对路径** — 避免 working directory 问题
