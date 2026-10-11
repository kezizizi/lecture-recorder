# -*- coding: utf-8 -*-
"""说话人分离转写的成稿自检（`transcribe_diarize.py` 的配套校验）。

用法：
    PYTHON="python3"
    "$PYTHON" verify_diarized.py "<产物目录>"          # 目录内含 逐字稿.json / 逐字稿.md / asr/req_*.json

五项检查：
  A. 请求完整性 —— 每个 ASR 请求的 duration 是否与计划切片时长一致（**防静默截断**）
  B. 时间轴     —— 全局时间戳单调不减、不自重叠、落在总时长内
  C. 说话人     —— 编号连续、每人段数/时长与统计表一致、无孤儿段
  D. 去重账    —— 被丢弃的时长是否约等于「片数−1」×重叠秒（丢弃异常 → 去重规则出错）
  E. 成稿一致性 —— 逐字稿.md 正文（去掉标记）是否与 逐字稿.json 的段文本逐字相等（**防丢字/改字**）

任一 ❌ 都必须回头查原因，不要带着问题交稿。
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

PUNCT = re.compile(r'[\s\u3000-\u303f\uff00-\uffef,.\-*\u2018\u2019\u201c\u201d]')
TURN = re.compile(r'^\[(\d\d):(\d\d):(\d\d)\] \*\*(.+?)\*\*\u3000?(.*)$')
OK, BAD = "✅", "❌"


def norm(s):
    return PUNCT.sub("", s)


def hhmmss(sec):
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%02d" % (h, m, s)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    d = sys.argv[1]
    pj = os.path.join(d, "逐字稿.json")
    pm = os.path.join(d, "逐字稿.md")
    if not os.path.isfile(pj) or not os.path.isfile(pm):
        print("找不到 %s 或 %s" % (pj, pm))
        return 1
    J = json.load(io.open(pj, encoding="utf-8"))
    MD = io.open(pm, encoding="utf-8").read().replace("\r\n", "\n")
    segs = J["segments"]
    plan = [(p["start"], p["end"]) for p in J["plan"]]
    fails = []

    print("源文件：%s" % J["source"])
    print("模型：%s   时长 %.1fs   片数 %d   段数 %d"
          % (J["model"], J["duration"], len(plan), len(segs)))
    print("=" * 72)

    # ---------------- A. 请求完整性
    print("\nA. 请求完整性（防静默截断，容差 3s）")
    reqs = []
    for i, plan_seg in enumerate(plan):
        f = os.path.join(d, "asr", "req_%02d.json" % i)
        if not os.path.isfile(f):
            print("   %s 片%d 缺 req_%02d.json" % (BAD, i + 1, i))
            fails.append("A%d 缺原始返回" % (i + 1))
            continue
        R = json.load(io.open(f, encoding="utf-8"))
        want = plan_seg[1] - plan_seg[0]
        got = float(R.get("duration") or 0)
        raw = sum(s["end"] - s["start"] for s in R["segments"])
        flag = OK if abs(got - want) <= 3 else BAD
        if flag == BAD:
            fails.append("A%d duration %.1f vs 计划 %.1f" % (i + 1, got, want))
        print("   %s 片%-2d 计划 %8.1fs | 返回 duration %8.1fs | 段内合计 %8.1fs (%d 段)"
              % (flag, i + 1, want, got, raw, len(R["segments"])))
        reqs.append(R)
    if not reqs:
        print("   无原始返回可比对（跳过后续依赖项）")

    # ---------------- B. 时间轴
    print("\nB. 时间轴（单调 / 不自重叠 / 在总时长内）")
    bad_ord = [i for i in range(1, len(segs)) if segs[i]["start"] < segs[i - 1]["end"] - 1e-6]
    bad_out = [i for i, s in enumerate(segs)
               if s["start"] < -1e-6 or s["end"] > J["duration"] + 2 or s["end"] <= s["start"]]
    if bad_ord:
        print("   %s 时间倒退/自重叠：%d 处，首个 idx=%d（%.2f→%.2f）"
              % (BAD, len(bad_ord), bad_ord[0], segs[bad_ord[0] - 1]["end"], segs[bad_ord[0]]["start"]))
        fails.append("B 时间轴不单调")
    else:
        print("   %s 单调不减、无自重叠" % OK)
    if bad_out:
        print("   %s 越界/零长段 %d 处：%s" % (BAD, len(bad_out), bad_out[:10]))
        fails.append("B 越界段")
    else:
        print("   %s 全部落在 [0, %.1fs] 内" % (OK, J["duration"]))

    total = max((s["end"] for s in segs), default=0)
    holes, cur = [], 0.0
    for s in segs:
        if s["start"] - cur > 5:
            holes.append((cur, s["start"]))
        cur = max(cur, s["end"])
    if total - cur > 5:
        holes.append((cur, total))
    cov = sum(s["end"] - s["start"] for s in segs)
    print("   有效语音 %.1f%%；>5s 的空档 %d 处%s"
          % (100 * cov / total if total else 0, len(holes),
             ("（最长 %.0fs @ %s）" % (max(h[1] - h[0] for h in holes),
                                      hhmmss(max(holes, key=lambda x: x[1] - x[0])[0]))
              if holes else "")))
    print("   注：空档＝静音/未识别，属正常；若某空档超过 60s 建议抽听确认。")

    # ---------------- C. 说话人
    print("\nC. 说话人编号")
    spk = J["speakers"]
    labels = list(spk.keys())
    want = ["说话人%d" % i for i in range(1, len(labels) + 1)]
    if labels != want:
        print("   %s 编号不连续或非「说话人N」形态：%s" % (BAD, labels))
        fails.append("C 编号不连续")
    else:
        print("   %s 编号连续：%s" % (OK, "、".join(labels)))
    cnt = {}
    dur = {}
    for s in segs:
        cnt[s["speaker"]] = cnt.get(s["speaker"], 0) + 1
        dur[s["speaker"]] = dur.get(s["speaker"], 0) + s["end"] - s["start"]
    for k in labels:
        m = (cnt.get(k, 0) == spk[k]["n"]) and abs(dur.get(k, 0) - spk[k]["dur"]) <= 1.5
        if not m:
            print("   %s %s 统计不一致：表 %s 段/%.0fs，实际 %d 段/%.0fs"
                  % (BAD, k, spk[k]["n"], spk[k]["dur"], cnt.get(k, 0), dur.get(k, 0)))
            fails.append("C %s 统计不一致" % k)
    orphan = {s["speaker"] for s in segs} - set(labels)
    if orphan:
        print("   %s 孤儿说话人：%s" % (BAD, orphan))
        fails.append("C 孤儿说话人")
    else:
        print("   %s 无孤儿段；每人段数/时长与统计表一致" % OK)
    for k in labels:
        print("      %-6s %6.0fs %4d 段  首 %s  末 %s%s"
              % (k, spk[k]["dur"], spk[k]["n"], hhmmss(spk[k]["first"]),
                 hhmmss(spk[k]["last"]), "  ⚠️跨片未对齐" if spk[k].get("unaligned") else ""))
    n_un = sum(1 for k in labels if spk[k].get("unaligned"))
    if len(labels) > 6:
        print("   ⚠️ 共 %d 个编号。录音越长、说话人越多，**过分割越明显**"
              "（片内编号数 ≠ 真实人数）。落名前请按《说话人对照表》的"
              "「各片编号分布」逐个比对，**不要按编号数推断人数**。" % len(labels))
    if n_un:
        print("   ⚠️ %d/%d 个编号跨片未对齐：重叠区只覆盖到当时在说话的人，"
              "其余必须人工归并（对照表已列出各片编号的时段与样例句）。" % (n_un, len(labels)))

    # ---------------- D. 去重账
    print("\nD. 片间去重账")
    if len(plan) == 1:
        print("   %s 单片请求，无重叠、无去重" % OK)
    elif reqs:
        raw = sum(s["end"] - s["start"] for R in reqs for s in R["segments"])
        dropped = raw - cov
        expect = J.get("overlap_seconds") or 300.0
        print("   原始段合计 %.1fs − 成稿覆盖 %.1fs = 丢弃 %.1fs；"
              "理论重叠总量 ≤ %.1fs" % (raw, cov, dropped, expect * (len(plan) - 1)))
        if dropped > expect * (len(plan) - 1) * 1.5 + 60:
            print("   %s 丢弃量远超重叠总量，去重规则可能多删了内容，请比对 asr/req_*.json" % BAD)
            fails.append("D 去重量异常")
        else:
            print("   %s 丢弃量在重叠范围内，去重合理" % OK)
        print("   交界处（%d 处）：%s" % (len(J.get("seams", [])),
              "；".join("第%d片起 @ %s（重叠 %s）"
                        % (s["req"], hhmmss(s["kept_from"]),
                           "–".join(hhmmss(x) for x in s["overlap"]))
                        for s in J.get("seams", [])) or "无跨界段"))
        if len(plan) - 1 != len(J.get("seams", [])):
            print("   ⚠️ 计划 %d 个片间交界，实记 %d 个：可能有某片整段被前片覆盖（不可怕，但要抽听确认）。"
                  % (len(plan) - 1, len(J.get("seams", []))))

    # ---------------- E. 成稿一致性
    print("\nE. 成稿一致性（逐字稿.md ↔ 逐字稿.json）")
    body = MD.split("\n---\n", 1)[-1]
    turns, unmatched = [], 0
    for line in body.split("\n"):
        if line.startswith(">") or not line.strip():
            continue
        m = TURN.match(line)
        if m:
            turns.append((int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)),
                          m.group(4), m.group(5)))
        elif line.startswith("["):
            unmatched += 1
    print("   解析到 %d 轮%s" % (len(turns), "" if not unmatched else "（%d 行格式异常）" % unmatched))
    joined = norm("".join(s["text"] for s in segs))
    md_txt = norm("".join(t[2] for t in turns))
    if joined == md_txt:
        print("   %s 正文字数与 JSON 段文本**完全一致**（%d 字），无丢字无改字" % (OK, len(joined)))
    else:
        print("   %s 不一致：JSON %d 字 vs 成稿 %d 字（差 %d）"
              % (BAD, len(joined), len(md_txt), len(md_txt) - len(joined)))
        n = min(len(joined), len(md_txt))
        i = next((k for k in range(n) if joined[k] != md_txt[k]), n)
        print("      首个分歧在第 %d 字：JSON「%s」/ 成稿「%s」"
              % (i, joined[i:i + 30], md_txt[i:i + 30]))
        fails.append("E 成稿与 JSON 不一致")
    badtime = [t for i, t in enumerate(turns) if i and t[0] < turns[i - 1][0]]
    if badtime:
        print("   %s 成稿时间戳有倒退 %d 处" % (BAD, len(badtime)))
        fails.append("E 成稿时间戳倒退")
    else:
        print("   %s 成稿时间戳单调" % OK)

    # ---------------- 汇总
    print("\n" + "=" * 72)
    if fails:
        print("%s 未通过 %d 项：%s" % (BAD, len(fails), "；".join(fails)))
        return 1
    print("%s 五项全过。提醒：编号只保证「同一请求内同一编号＝同一人」，"
          "真实身份仍需按《说话人对照表》人工落名。" % OK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
