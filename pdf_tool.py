#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_tool.py — 离线批量 PDF 提取工具（敏感文件专用）

设计原则：
  * 纯本地处理，运行时零网络请求，文件绝不离开本机
  * 批量输入：支持多个文件、目录（递归）、glob 通配
  * 三种提取模式：
      info    查看页数/元数据/是否加密/是否扫描件
      text    提取全文（按页分隔）
      find    关键字/正则检索，带页码+行号+上下文，可导出 CSV
      tables  表格提取，每张表导出一个 CSV

依赖：PyMuPDF（仅安装时需联网一次：pip install pymupdf，之后完全离线）

用法示例：
  python3 pdf_tool.py info   合同.pdf 敏感资料/
  python3 pdf_tool.py text   敏感资料/ -o out/
  python3 pdf_tool.py find   敏感资料/ -q 金额 -q 身份证 -C 2 -o 报告.csv
  python3 pdf_tool.py tables 合同.pdf -o tables_out/

Windows 提示：把 python3 换成 python 即可。
"""

import argparse
import csv
import glob
import os
import re
import sys
from pathlib import Path


def load_pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        try:
            import fitz as pymupdf  # 旧版包名
            return pymupdf
        except ImportError:
            sys.exit("[错误] 缺少 PyMuPDF。请先执行：pip install pymupdf（安装后即可离线使用）")


def setup_stdout():
    """Windows 控制台默认编码可能不是 UTF-8，强制修正。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def collect_pdfs(paths):
    """收集文件：目录递归扫描 / 单文件 / 未被 shell 展开的 glob。"""
    files, missing = [], []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            files += sorted(str(f) for f in pp.rglob("*")
                            if f.is_file() and f.suffix.lower() == ".pdf")
        elif pp.is_file():
            files.append(str(pp))
        else:
            hits = [h for h in glob.glob(p) if h.lower().endswith(".pdf")]
            if hits:
                files += sorted(hits)
            else:
                missing.append(p)
    seen, out = set(), []
    for f in files:
        key = os.path.abspath(f)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out, missing


def open_doc(m, path):
    doc = m.open(path)
    if doc.needs_pass:
        if not doc.authenticate(""):
            doc.close()
            raise RuntimeError("已加密且空密码验证失败（请先解密再处理）")
    return doc


def page_text(page, use_ocr=False):
    if use_ocr:
        try:
            tpx = page.get_textpage_ocr()
            return page.get_text(textpage=tpx)
        except Exception as e:
            raise RuntimeError(
                f"OCR 失败：{e}\n"
                "  提示：OCR 需要本机安装 Tesseract（tesseract 及中文语言包）。"
                "无 OCR 时请仅处理含文字层的 PDF。"
            )
    return page.get_text("text")


# ---------------- 子命令 ----------------

def cmd_info(args, m):
    files, missing = collect_pdfs(args.paths)
    if missing:
        print(f"[警告] 未找到：{', '.join(missing)}")
    if not files:
        sys.exit("[错误] 没有找到任何 PDF 文件")
    for path in files:
        try:
            doc = open_doc(m, path)
        except Exception as e:
            print(f"\n== {path}\n   [失败] {e}")
            continue
        n = doc.page_count
        meta = doc.metadata or {}
        empty = 0
        total_chars = 0
        for i in range(n):
            t = doc[i].get_text("text").strip()
            total_chars += len(t)
            if len(t) < 5:
                empty += 1
        doc.close()
        print(f"\n== {path}")
        print(f"   页数: {n} | 加密: 否(或已解密) | 文字层总字符: {total_chars}")
        if meta.get("title"):
            print(f"   标题: {meta['title']}")
        if meta.get("author"):
            print(f"   作者: {meta['author']}")
        if empty == n and n > 0:
            print("   [提示] 全文无文字层 → 疑似扫描件，需 OCR（tesseract）才能提取")
        elif empty:
            print(f"   [提示] {empty} 页几乎无文字（可能是图片页/封面）")


def cmd_text(args, m):
    files, missing = collect_pdfs(args.paths)
    if missing:
        print(f"[警告] 未找到：{', '.join(missing)}")
    if not files:
        sys.exit("[错误] 没有找到任何 PDF 文件")
    outdir = None
    if args.output:
        outdir = Path(args.output)
        if outdir.suffix:  # 给的是文件路径 → 合并写入单文件
            outdir = outdir.parent
        outdir.mkdir(parents=True, exist_ok=True)
    merged = []
    for path in files:
        try:
            doc = open_doc(m, path)
        except Exception as e:
            print(f"[失败] {path}: {e}")
            continue
        chunks = []
        for i in range(doc.page_count):
            chunks.append(f"----- 第 {i + 1} 页 -----")
            try:
                chunks.append(page_text(doc[i], use_ocr=args.ocr).rstrip())
            except RuntimeError as e:
                chunks.append(f"[OCR错误] {e}")
        doc.close()
        body = "\n\n".join(chunks) + "\n"
        if outdir is None:
            print(f"\n===== {path} =====")
            print(body)
        else:
            txt_path = outdir / (Path(path).stem + ".txt")
            txt_path.write_text(body, encoding="utf-8")
            print(f"[已导出] {txt_path}")
        merged.append((path, body))
    if args.output and str(args.output).lower().endswith((".txt", ".md")):
        Path(args.output).write_text(
            "\n\n".join(f"===== {p} =====\n{b}" for p, b in merged), encoding="utf-8")
        print(f"[已合并导出] {args.output}")


def build_pattern(args):
    if not args.query:
        sys.exit("[错误] find 模式必须提供至少一个 -q 关键词")
    flags = 0 if args.case_sensitive else re.IGNORECASE
    if args.regex:
        return re.compile("|".join(f"(?:{q})" for q in args.query), flags)
    return re.compile("|".join(re.escape(q) for q in args.query), flags)


def cmd_find(args, m):
    files, missing = collect_pdfs(args.paths)
    if missing:
        print(f"[警告] 未找到：{', '.join(missing)}")
    if not files:
        sys.exit("[错误] 没有找到任何 PDF 文件")
    pattern = build_pattern(args)
    rows = []
    for path in files:
        try:
            doc = open_doc(m, path)
        except Exception as e:
            print(f"[失败] {path}: {e}")
            continue
        for i in range(doc.page_count):
            lines = doc[i].get_text("text").splitlines()
            for ln, line in enumerate(lines):
                if pattern.search(line):
                    ctx_before = lines[max(0, ln - args.context):ln]
                    ctx_after = lines[ln + 1:ln + 1 + args.context]
                    context = " ⏎ ".join(ctx_before + ["【↑匹配↑】"] + ctx_after)
                    rows.append({
                        "文件": path,
                        "页码": i + 1,
                        "行号": ln + 1,
                        "匹配行": line.strip(),
                        "上下文": context,
                    })
        doc.close()

    if not rows:
        print("[结果] 没有匹配项")
        return

    for r in rows:
        print(f"{r['文件']}  p.{r['页码']} L{r['行号']}: {r['匹配行']}")
    print(f"\n[共 {len(rows)} 条匹配]")

    if args.output:
        outp = Path(args.output)
        outp.parent.mkdir(parents=True, exist_ok=True)
        fields = ["文件", "页码", "行号", "匹配行", "上下文"]
        if outp.suffix.lower() == ".md":
            lines = ["| " + " | ".join(fields) + " |",
                     "|" + "|".join(["---"] * len(fields)) + "|"]
            lines += ["| " + " | ".join(str(r[f]).replace("|", "\\|") for f in fields) + " |"
                      for r in rows]
            outp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:  # 默认 CSV，utf-8-sig 保证 Excel 正确识别中文
            with open(outp, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=fields)
                w.writeheader()
                w.writerows(rows)
        print(f"[已导出] {outp}")


def cmd_tables(args, m):
    files, missing = collect_pdfs(args.paths)
    if missing:
        print(f"[警告] 未找到：{', '.join(missing)}")
    if not files:
        sys.exit("[错误] 没有找到任何 PDF 文件")
    outdir = Path(args.output or "tables_out")
    outdir.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in files:
        try:
            doc = open_doc(m, path)
        except Exception as e:
            print(f"[失败] {path}: {e}")
            continue
        stem = Path(path).stem
        for i in range(doc.page_count):
            page = doc[i]
            try:
                tables = page.find_tables().tables  # 策略一：矢量线检测
                if not tables:
                    tables = page.find_tables(      # 兜底：文本对齐检测（无线框表）
                        strategy="text").tables
            except Exception as e:
                print(f"[警告] {path} 第{i+1}页表格解析失败: {e}")
                continue
            for ti, tab in enumerate(tables):
                rows = tab.extract()
                if not rows:
                    continue
                csv_path = outdir / f"{stem}__p{i+1}_t{ti+1}.csv"
                with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
                    w = csv.writer(fh)
                    for r in rows:
                        w.writerow(["" if c is None else str(c).replace("\n", " ")
                                    for c in r])
                count += 1
                print(f"[表格] {path} 第{i+1}页 第{ti+1}张 → {csv_path} ({len(rows)}行)")
        doc.close()
    print(f"\n[完成] 共导出 {count} 张表 → {outdir}/")
    if count == 0:
        print("[提示] 未检出表格。可能是扫描件（需OCR）或表格为图片绘制，"
              "可改用 find/text 提取文字后人工整理")


def main():
    setup_stdout()
    ap = argparse.ArgumentParser(
        prog="pdf_tool.py",
        description="离线批量 PDF 提取工具（纯本地运行，零网络请求）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法示例：")[-1] if "用法示例：" in __doc__ else None,
    )
    ap.add_argument("--version", action="version", version="pdf_tool 1.1")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="查看页数/元数据/加密与扫描件状态")
    p_info.add_argument("paths", nargs="+", help="PDF 文件 / 目录 / glob")

    p_text = sub.add_parser("text", help="提取全文")
    p_text.add_argument("paths", nargs="+")
    p_text.add_argument("-o", "--output", help="输出目录（或单个 .txt/.md 合并文件）；缺省打印到屏幕")
    p_text.add_argument("--ocr", action="store_true",
                        help="对无文字层的页启用 OCR（需本机安装 tesseract）")

    p_find = sub.add_parser("find", help="关键字/正则检索（页码+行号+上下文）")
    p_find.add_argument("paths", nargs="+")
    p_find.add_argument("-q", "--query", action="append",
                        help="关键词，可重复使用多个")
    p_find.add_argument("--regex", action="store_true", help="按正则表达式匹配")
    p_find.add_argument("-C", "--context", type=int, default=2,
                        help="上下文行数，默认 2")
    p_find.add_argument("--case-sensitive", action="store_true", help="区分大小写")
    p_find.add_argument("-o", "--output", help="导出报告（.csv 或 .md）")

    p_tab = sub.add_parser("tables", help="表格提取为 CSV")
    p_tab.add_argument("paths", nargs="+")
    p_tab.add_argument("-o", "--output", help="输出目录，默认 ./tables_out")

    args = ap.parse_args()
    m = load_pymupdf()
    {"info": cmd_info, "text": cmd_text,
     "find": cmd_find, "tables": cmd_tables}[args.cmd](args, m)


if __name__ == "__main__":
    main()
