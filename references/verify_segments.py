# -*- coding: utf-8 -*-
"""分片归属校验 —— 逐字稿成稿后的三项自检（多说话人录音必做）。

用法：
    PYTHON="python3"
    "$PYTHON" verify_segments.py <逐字稿.md> <原始ASR目录> <分片前缀，如 rec2>

检查三项：
  1) 每节正文能否在原始分片中命中（定位该节实际来自哪些分片）
  2) 有没有同一分片被两节引用          → 内容重复
  3) 有没有分片完全没被任何一节引用      → 内容遗漏

第 2、3 项任一非空，就说明分段边界错了，必须回到锚点重新切分。
"""
import os
import re
import sys
import io

PUNCT = re.compile(r'[\s\u3000-\u303f\uff00-\uffef,.\-*\u2018\u2019\u201c\u201d]')


def norm(s):
    """去掉角色标记、标点、空白，便于跨稿比对。"""
    s = re.sub(r'\*\*.*?\*\*', '', s)
    for junk in ('（待核对）', '（说话人不确定）', '（没有。）'):
        s = s.replace(junk, '')
    return PUNCT.sub('', s)


def load_chunks(d, prefix):
    ch = {}
    for f in sorted(os.listdir(d)):
        m = re.match(re.escape(prefix) + r'_(\d+)\.txt$', f)
        if m:
            ch[int(m.group(1))] = io.open(os.path.join(d, f), encoding='utf-8').read()
    return ch


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 1

    md, d, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    ch = load_chunks(d, prefix)
    if not ch:
        print('未在 %s 找到 %s_*.txt' % (d, prefix))
        return 1

    nchunk = {i: norm(s) for i, s in ch.items()}
    L = io.open(md, encoding='utf-8').read().replace('\r\n', '\n').split('\n')

    secs = [(i, l) for i, l in enumerate(L) if l.startswith('### [') or l.startswith('## ')]
    secs.append((len(L), 'EOF'))

    print('分片数：%d   分节数：%d\n' % (len(ch), len(secs) - 1))

    hit_by_sec = []
    for k in range(len(secs) - 1):
        a, h = secs[k]
        b = secs[k + 1][0]
        body = re.sub(r'^\s*>.*$', '', '\n'.join(L[a + 1:b]), flags=re.M)
        n = norm(body)
        print('%-52s 正文 %5d 字' % (h[:52], len(n)))
        if not n:
            hit_by_sec.append(set())
            print('     （空段）')
            continue
        hits = set()
        step = 200
        for st in range(0, max(1, len(n) - 25), step):
            probe = n[st:st + 26]
            for idx, s in nchunk.items():
                if probe in s:
                    hits.add(idx)
        hit_by_sec.append(hits)
        print('     命中分片：%s' % sorted(hits))

    seen = {}
    for k, hs in enumerate(hit_by_sec):
        for i in hs:
            seen.setdefault(i, []).append(k)

    dup = {i: v for i, v in seen.items() if len(v) > 1}
    miss = sorted(set(ch) - set(seen))

    print('\n' + '=' * 60)
    print('① 重复检查：%s' % ('❌ 以下分片被多节引用 → 内容重复，分段错了\n    %s' % dup if dup else '✅ 无'))
    print('② 遗漏检查：%s' % ('❌ 以下分片未被任何一节引用 → 内容丢失，分段错了\n    %s' % miss if miss else '✅ 无'))
    print('③ 覆盖率：%d/%d 分片被引用' % (len(seen), len(ch)))
    print('=' * 60)
    print('\n注：命中数偏少通常是因为标注稿加了标点/角色标记，属正常；'
          '关键是 ① 必须为空、② 应尽量为空（少量未命中要能人工定位到"归属到相邻节"）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
