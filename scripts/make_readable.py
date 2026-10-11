#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把说话人分离逐字稿（逐字稿.md）转成不截断的可读稿，供 AI 通读。

为什么需要它：
  1. Read 工具单行超过 2000 字符会截断；ASR 的长轮次经常一次说几千字。
  2. `textwrap.wrap()` 对**无空格的中文无效**（整段被视作一个长单词），必须按字数硬切片。

用法：
    python make_readable.py <逐字稿所在目录或.md路径> [输出路径]

约定：
  - 输入行格式  `[HH:MM:SS] **说话人N** 正文...`
  - 输出把每个轮次改成 `### [HH:MM:SS] 说话人N` 标题 + 每 CHUNK 字一行的正文。
  - 中文按 CHUNK=78 硬切；遇到英文/数字连续串不拆断。
"""
import os
import re
import sys

CHUNK = 78
PREFIX_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s+\*\*(.+?)\*\*\s*(.*)$")
KEEP_RE = re.compile(r"^(#{1,6}\s|[-*]\s|\||>|={3,})")


def hard_wrap(text, chunk=CHUNK):
    """按字符数硬切，但尽量不切断连续的拉丁字母/数字串。"""
    out, line = [], ""
    for ch in text:
        line += ch
        if len(line) >= chunk and (ch in "。！？；，、）》」』" or len(line) >= chunk + 8):
            out.append(line)
            line = ""
    if line:
        out.append(line)
    return out


def convert(md_path, out_path):
    # 关键：Windows 下 open() 不指定编码会按 GBK 解码 UTF-8 文件，直接乱码/报错
    with open(md_path, encoding="utf-8") as fh:
        lines = fh.read().replace("\r\n", "\n").split("\n")

    buf, turns = [], 0
    for ln in lines:
        m = PREFIX_RE.match(ln)
        if m:
            ts, sp, body = m.group(1), m.group(2), m.group(3)
            turns += 1
            buf.append("")
            buf.append("### [%s] %s" % (ts, sp))
            buf.extend(hard_wrap(body))
        elif KEEP_RE.match(ln):
            buf.append(ln)

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(buf))
    return turns


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = sys.argv[1]
    if os.path.isdir(src):
        src = os.path.join(src, "逐字稿.md")
    if not os.path.exists(src):
        print("找不到 %s" % src)
        return 1
    if len(sys.argv) > 2:
        dst = sys.argv[2]
    else:
        dst = os.path.join(os.path.dirname(os.path.abspath(src)), "_可读稿.md")
    n = convert(src, dst)
    print("turns=%d  ->  %s" % (n, dst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
