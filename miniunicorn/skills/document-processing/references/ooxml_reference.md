# OOXML XML 模式参考

本文档涵盖 Word OOXML(Office Open XML)的常用 XML 模式,用于 unpack/pack 流程中直接编辑 XML。

## 目录

- [文档结构](#文档结构)
- [段落](#段落)
- [文本运行](#文本运行)
- [表格](#表格)
- [修订追踪](#修订追踪)
- [批注](#批注)
- [样式](#样式)
- [命名空间](#命名空间)

## 文档结构

```xml
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <!-- 段落、表格等内容 -->
    <w:p>...</w:p>
    <w:tbl>...</w:tbl>
    <!-- 文档结束属性(页码等) -->
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/>
    </w:sectPr>
  </w:body>
</w:document>
```

## 段落

### 基本段落

```xml
<w:p>
  <w:pPr>
    <w:pStyle w:val="Heading1"/>
    <w:jc w:val="center"/>
  </w:pPr>
  <w:r>
    <w:t>段落文本</w:t>
  </w:r>
</w:p>
```

### 段落属性顺序(必须遵守)

`<w:pPr>` 内元素顺序:
1. `<w:pStyle>` — 样式引用
2. `<w:numPr>` — 编号/列表
3. `<w:spacing>` — 行距
4. `<w:ind>` — 缩进
5. `<w:jc>` — 对齐
6. `<w:rPr>` — 段落标记的字体属性

### 对齐方式

| 值 | 对齐 |
|----|------|
| `left` | 左对齐 |
| `center` | 居中 |
| `right` | 右对齐 |
| `both` | 两端对齐 |

### 列表

```xml
<!-- 项目符号 -->
<w:p>
  <w:pPr>
    <w:numPr>
      <w:ilvl w:val="0"/>
      <w:numId w:val="1"/>
    </w:numPr>
  </w:pPr>
  <w:r><w:t>列表项</w:t></w:r>
</w:p>
```

## 文本运行

### 基本文本

```xml
<w:r>
  <w:rPr>
    <w:b/>        <!-- 加粗 -->
    <w:i/>        <!-- 斜体 -->
    <w:u w:val="single"/>  <!-- 下划线 -->
    <w:sz w:val="28"/>     <!-- 字号(半磅,28=14pt) -->
    <w:color w:val="FF0000"/>  <!-- 颜色 -->
  </w:rPr>
  <w:t>文本内容</w:t>
</w:r>
```

### 空格保留

```xml
<!-- 含前后空格时必须加 xml:space="preserve" -->
<w:t xml:space="preserve"> 带空格的文本 </w:t>
```

### 换行

```xml
<w:r>
  <w:br/>
</w:r>
```

## 表格

### 基本表格

```xml
<w:tbl>
  <w:tblPr>
    <w:tblStyle w:val="TableGrid"/>
    <w:tblW w:w="9360" w:type="dxa"/>
    <w:tblBorders>
      <w:top w:val="single" w:sz="4" w:color="CCCCCC"/>
      <w:bottom w:val="single" w:sz="4" w:color="CCCCCC"/>
      <w:left w:val="single" w:sz="4" w:color="CCCCCC"/>
      <w:right w:val="single" w:sz="4" w:color="CCCCCC"/>
    </w:tblBorders>
  </w:tblPr>
  <w:tblGrid>
    <w:gridCol w:w="4680"/>
    <w:gridCol w:w="4680"/>
  </w:tblGrid>
  <w:tr>
    <w:tc>
      <w:tcPr><w:tcW w:w="4680" w:type="dxa"/></w:tcPr>
      <w:p><w:r><w:t>单元格</w:t></w:r></w:p>
    </w:tc>
  </w:tr>
</w:tbl>
```

### 合并单元格

```xml
<!-- 横向合并 -->
<w:tc>
  <w:tcPr>
    <w:gridSpan w:val="2"/>  <!-- 跨 2 列 -->
  </w:tcPr>
</w:tc>

<!-- 纵向合并(第一格) -->
<w:tc>
  <w:tcPr>
    <w:vMerge w:val="restart"/>
  </w:tcPr>
</w:tc>

<!-- 纵向合并(后续格) -->
<w:tc>
  <w:tcPr>
    <w:vMerge/>  <!-- 继续合并 -->
  </w:tcPr>
</w:tc>
```

## 修订追踪

### 插入文本

```xml
<w:ins w:id="1" w:author="miniunicorn" w:date="2026-07-16T00:00:00Z">
  <w:r>
    <w:t>新插入的文本</w:t>
  </w:r>
</w:ins>
```

### 删除文本

```xml
<w:del w:id="2" w:author="miniunicorn" w:date="2026-07-16T00:00:00Z">
  <w:r>
    <w:delText>被删除的文本</w:delText>  <!-- 注意:用 delText 而非 t -->
  </w:r>
</w:del>
```

### 删除整段(含段落标记)

```xml
<w:p>
  <w:pPr>
    <w:rPr>
      <w:del w:id="3" w:author="miniunicorn" w:date="2026-07-16T00:00:00Z"/>
    </w:rPr>
  </w:pPr>
  <w:del w:id="4" w:author="miniunicorn" w:date="2026-07-16T00:00:00Z">
    <w:r><w:delText>整段内容</w:delText></w:r>
  </w:del>
</w:p>
```

### 格式修改

```xml
<w:pPr>
  <w:pPrChange w:id="5" w:author="miniunicorn" w:date="2026-07-16T00:00:00Z">
    <w:pPr>
      <!-- 旧的段落属性 -->
    </w:pPr>
  </w:pPrChange>
  <!-- 新的段落属性 -->
</w:pPr>
```

## 批注

### 在 document.xml 中标记

```xml
<w:p>
  <w:commentRangeStart w:id="0"/>
  <w:r><w:t>被批注的文本</w:t></w:r>
  <w:commentRangeEnd w:id="0"/>
  <w:r>
    <w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
    <w:commentReference w:id="0"/>
  </w:r>
</w:p>
```

### comments.xml

```xml
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:comment w:id="0" w:author="miniunicorn" w:date="2026-07-16T00:00:00Z" w:initials="M">
    <w:p>
      <w:r><w:t>批注内容</w:t></w:r>
    </w:p>
  </w:comment>
</w:comments>
```

### 批注回复

```xml
<w:comment w:id="1" w:author="张三" w:date="..." w:initials="Z">
  <w:p>
    <w:pPr>
      <w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
    </w:pPr>
    <w:r>
      <w:annotationRef/>
    </w:r>
    <w:r><w:t>回复内容</w:t></w:r>
  </w:p>
</w:comment>
```

## 样式

### styles.xml

```xml
<w:style w:type="paragraph" w:styleId="MyHeading">
  <w:name w:val="My Heading"/>
  <w:basedOn w:val="Normal"/>
  <w:next w:val="Normal"/>
  <w:pPr>
    <w:spacing w:before="240" w:after="120"/>
    <w:outlineLvl w:val="0"/>
  </w:pPr>
  <w:rPr>
    <w:b/>
    <w:sz w:val="32"/>
  </w:rPr>
</w:style>
```

### 覆盖内置标题样式

必须用精确的 styleId:`Heading1`、`Heading2`、`Heading3`...

```xml
<w:style w:type="paragraph" w:styleId="Heading1">
  <w:name w:val="heading 1"/>
  <w:basedOn w:val="Normal"/>
  <w:next w:val="Normal"/>
  <w:qFormat/>
  <w:pPr>
    <w:outlineLvl w:val="0"/>
  </w:pPr>
  <w:rPr>
    <w:b/>
    <w:sz w:val="32"/>
  </w:rPr>
</w:style>
```

## 命名空间

| 前缀 | URI | 用途 |
|------|-----|------|
| `w` | `http://schemas.openxmlformats.org/wordprocessingml/2006/main` | Word 主命名空间 |
| `r` | `http://schemas.openxmlformats.org/officeDocument/2006/relationships` | 关系 |
| `wp` | `http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing` | 绘图 |
| `a` | `http://schemas.openxmlformats.org/drawingml/2006/main` | 绘图主 |
| `pic` | `http://schemas.openxmlformats.org/drawingml/2006/picture` | 图片 |

## DXA 单位

- 1 英寸 = 1440 DXA
- 1 厘米 = 567 DXA
- 1 磅 = 20 DXA
- US Letter: 12240 × 15840 (8.5" × 11")
- A4: 11906 × 16838 (210mm × 297mm)

## 常见陷阱

1. **`<w:pPr>` 内元素顺序必须遵守 Schema** — 否则 Word 无法打开
2. **`<w:t>` 含空格时必须加 `xml:space="preserve"`** — 否则空格丢失
3. **`<w:del>` 内用 `<w:delText>` 而非 `<w:t>`**
4. **RSID 必须 8 位十六进制** — 如 `00AB1234`
5. **`w:id` 全局唯一** — 修订/批注的 ID 不能重复
