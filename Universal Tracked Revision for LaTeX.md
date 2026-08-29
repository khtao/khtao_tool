# Universal Tracked Revision for LaTeX 代码说明

## 1. 程序简介

`universal_tracked_revision_v5.py` 是一个面向 LaTeX 文档的通用修订痕迹生成工具。程序以“原始稿 `.tex`”和“修订稿 `.tex`”为输入，自动比较两个版本之间的差异，并生成带有新增、删除等修订痕迹的 LaTeX 文件。

与简单的逐行文本比较不同，本程序首先识别正文、章节标题、公式、表格、图片等 LaTeX 结构，再在相同或相似结构之间进行内容比较。程序的设计目标是尽可能保持修订稿原有的 LaTeX 结构和排版命令，仅对真正发生变化的可见内容添加修订标记，从而降低 `\begin`、`\end`、`\label`、`\item`、`\includegraphics` 等控制命令被误标记后导致编译失败的风险。代码采用语义块匹配处理正文、标题、公式和浮动体，并针对表格建立了独立的结构化比较机制。

程序默认使用：

```latex
\added{新增内容}
\deleted{删除内容}
```

表示文本修改，并通过 `xcolor` 与 `ulem` 实现新增内容的蓝色显示和删除内容的红色删除线显示。

---

## 2. 主要设计特点

### 2.1 面向 LaTeX 结构，而不是简单字符串替换

程序的核心原则是：

> **LaTeX 控制结构保持原样，仅对可见内容添加修订痕迹。**

例如，对于：

```latex
\section{Methods}
\label{sec:methods}
```

如果标题发生修改，程序只处理 `Methods` 这一可见参数，而不会修改：

```latex
\section
\label
sec:methods
```

等结构或标识符。

因此不会产生类似：

```latex
\label{\deleted{sec:old}}
```

或者：

```latex
\deleted{\begin{table}}
```

这类可能破坏 `.aux`、目录、交叉引用或环境结构的代码。

程序还在输出阶段主动检查 `label`、`ref`、`cite`、环境名称等控制参数中是否错误嵌入了修订宏。

---

### 2.2 基于语义的文档块匹配

对于正文，程序并不要求旧稿和修订稿具有完全相同的行号或段落位置，而是先将 LaTeX 文档划分为不同类型的结构块，再根据可见文本和上下文进行匹配。

主要处理对象包括：

* 普通正文段落；
* `section`、`subsection`、`subsubsection` 等标题；
* 公式环境；
* figure；
* table；
* abstract；
* 普通 LaTeX 命令和注释。

程序利用文本相似度、上下文及结构类型进行旧稿与修订稿之间的匹配，因此即使修订过程中增加了段落、移动了图片或调整了部分结构，也不要求两个 `.tex` 文件逐行对应。其语义匹配过程会构建旧稿和新稿的可见文本表示，并计算文本相似程度。

---

### 2.3 通用化的表格修订机制

表格是本程序重点处理的对象之一。

程序没有预设“AUC”“Precision”“患者信息”“数据集”等特定论文语义，而是完全依据：

* LaTeX 表格结构；
* 单元格可见文本；
* Unicode 文本相似度；
* 行、列位置；
* 行列之间的一对一对应关系；

完成表格匹配。

因此该算法不依赖医学、计算机、工程或其他具体学科，可以处理中文、英文以及其他 Unicode 文本。单元格匹配采用字符序列、文本 token 和字符 n-gram 等通用相似性信息，而不存在针对某一篇论文变量名称的人工映射。

程序目前能够识别多种常见表格型环境，包括：

```text
tabular
tabular*
tabularx
tabulary
longtable
tabu
longtabu
supertabular
xtabular
mpxtabular
tblr
longtblr
NiceTabular
NiceTabularX
```

这些环境及其参数均按照 LaTeX 语法结构解析，而非根据具体手稿内容判断。

对于：

```latex
\multicolumn
\multirow
```

程序保留其跨行、跨列等结构参数，只比较最后实际显示的文本内容。也就是说，修订标记不会包裹整个 `\multicolumn` 或 `\multirow` 命令。

例如：

```latex
\multicolumn{2}{c}{Old title}
```

修改为：

```latex
\multicolumn{2}{c}{New title}
```

程序的目标是保持：

```latex
\multicolumn{2}{c}{...}
```

的结构不变，仅在实际显示内容中体现删除和新增。

对于删除行、新增行、单元格修改以及一定程度的列重新排列，也会通过通用行列匹配算法进行处理。

---

### 2.4 图片采用“保留修订稿图片”的策略

图片与普通文字不同，`\includegraphics` 属于文件加载控制命令，因此程序不会生成：

```latex
\deleted{\includegraphics{old.pdf}}
```

这种结构。

对于旧稿与新稿中匹配的 figure：

* caption 的文字变化正常显示修订痕迹；
* 如果图片文件发生替换，只保留修订稿中的新图片；
* `\includegraphics` 本身不进入 `\added` 或 `\deleted`。

对于已经从修订稿中完全删除的图片，旧 figure 会直接忽略，而不是在最终文档中保留一幅带“删除线”的图片。代码对此进行了明确限制。

---

### 2.5 强调编译安全

程序不仅生成修订痕迹，还包含一个专门的修复和清理阶段。

首先，程序自动配置：

```latex
\usepackage{xcolor}
\usepackage[normalem]{ulem}
```

并定义 robust revision commands：

```latex
\DeclareRobustCommand{\added}[1]{...}
\DeclareRobustCommand{\deleted}[1]{...}
\DeclareRobustCommand{\addedmath}[1]{...}
```

对于使用 `hyperref` 的文档，还专门设置 `\pdfstringdefDisableCommands`，避免修订宏进入 PDF bookmark 等字符串后引起错误。

此外，程序还针对几个常见的 LaTeX 编译风险进行了特殊处理：

* footnote 不直接嵌套在 `ulem` 修订范围中；
* display math 从普通 `\added{}` 中拆离；
* 删除态 `\includegraphics` 自动清除；
* 空 figure 自动清理；
* 纯空格、缩进和换行不会生成无意义的 `\added{}` 或 `\deleted{}`；
* `\item` 等列表控制结构与其后的正文分离处理。

这些规则主要用于避免 `ulem`、moving argument、list environment 和辅助文件读写过程中产生编译错误。

---

## 3. 程序基本处理流程

程序的整体工作流程可以概括为：

```text
原始稿 old.tex
        │
        ├── LaTeX结构解析
        │
修订稿 revised.tex
        │
        ↓
正文/标题/公式/表格/图片分块
        ↓
旧稿—新稿语义匹配
        ↓
普通文本差异分析
        ↓
表格行—列—单元格结构匹配
        ↓
图片与caption处理
        ↓
生成 \added / \deleted 修订痕迹
        ↓
LaTeX结构安全修复
        ↓
自动结构验证
        ↓
tracked.tex
+
tracked_report.txt
```

程序以修订稿的技术性 preamble 为基础构造最终文档，再将完成匹配和修订处理后的正文写入输出文件。

---

## 4. 使用方法

### 方法一：直接运行，一键选择文件

如果计算机安装了 Python，并且支持 `tkinter`，可以直接运行：

```bash
python universal_tracked_revision_v5.py
```

不提供任何参数时，程序会自动打开文件选择窗口。

依次选择：

```text
1. ORIGINAL / OLD LaTeX file
2. REVISED LaTeX file
```

即：

```text
原始稿.tex
↓
修订稿.tex
```

程序随后自动在修订稿所在目录生成：

```text
修订稿文件名_tracked.tex
修订稿文件名_tracked_report.txt
```

GUI 模式以及自动生成输出文件名和 report 文件的逻辑已经包含在脚本中。

---

### 方法二：命令行运行

也可以直接指定两个文件：

```bash
python universal_tracked_revision_v5.py old.tex revised.tex
```

默认会生成：

```text
revised_tracked.tex
revised_tracked_report.txt
```

如果需要指定输出文件：

```bash
python universal_tracked_revision_v5.py old.tex revised.tex -o output_tracked.tex
```

还可以单独指定检查报告：

```bash
python universal_tracked_revision_v5.py old.tex revised.tex \
    -o output_tracked.tex \
    --report output_report.txt
```

脚本支持 `old`、`revised`、`-o/--output` 和 `--report` 参数。

---

## 5. 输出文件说明

程序通常生成两个文件。

### （1）Tracked LaTeX 文件

例如：

```text
main_tracked.tex
```

其中：

```latex
\added{...}
```

表示新增内容，

```latex
\deleted{...}
```

表示删除内容。

表格、标题、正文和 caption 等均会根据其结构进行相应处理。

### （2）诊断报告

例如：

```text
main_tracked_report.txt
```

其中记录：

```text
old blocks
revised blocks
semantic matches
matched tables
ignored deleted figures
added occurrences
deleted occurrences
addedmath occurrences
brace balance
validation
```

用于快速判断生成结果是否存在明显结构异常。

正常情况下最后应出现：

```text
brace balance=0
validation=OK
```

---

## 6. 自动验证机制

生成完成后，程序不会立即认为输出一定正确，而是继续进行结构检查。

主要检查内容包括：

1. `{}` 大括号是否平衡；
2. 是否正确加载 `ulem[normalem]`；
3. `\label{}` 等标识符内部是否错误出现修订宏；
4. `ref/cite` 等引用 key 是否被修改；
5. `\begin{}`、`\end{}` 的环境名称是否被添加修订痕迹；
6. `\added{}`、`\deleted{}` 内部是否错误嵌套 LaTeX 控制命令；
7. 是否出现纯空白修订范围；
8. 是否仍然存在 `\deleted{\includegraphics...}`。

如果发现上述问题，诊断报告中的：

```text
validation
```

会显示失败信息，而不是静默输出。

---

## 7. 适用范围

该程序适合用于：

* SCI论文初稿与修订稿比较；
* Supplementary Information 修订；
* 学位论文不同版本比较；
* 投稿返修稿修订痕迹生成；
* 中文或英文 LaTeX 文稿比较；
* 包含普通表格、复杂表格、公式和图片的科研论文；
* 希望保留 LaTeX 原始结构，而又需要人工可读修订痕迹的文档。

由于表格匹配与正文匹配不依赖某一篇论文中的变量名称，因此同一个脚本可以用于不同研究方向和不同稿件，而不需要针对“AUC”“患者信息”“实验指标”等内容重新编写匹配规则。

---

## 8. 使用注意事项

本程序的目标是尽可能提高不同 LaTeX 文档之间的通用性和编译稳定性，但 LaTeX 宏系统本身具有高度可扩展性。如果文档大量使用自定义命令、自定义环境或特殊期刊 class，建议生成后仍进行一次实际 LaTeX 编译检查。

如果此前的错误版本已经生成过：

```text
.aux
.out
.toc
.lof
.lot
```

等辅助文件，在修改 tracked `.tex` 后建议先删除这些旧辅助文件，再重新编译，以避免损坏的辅助信息被 LaTeX 再次读取。

总体而言，该脚本采用的是：

> **结构优先、内容追踪、控制命令保护、表格通用匹配、生成后自动校验**

的设计思路，在保持修订痕迹可读性的同时，尽可能降低自动修订标记对 LaTeX 原始语法结构和编译过程的影响。
