# -*- coding: utf-8 -*-
"""整片优先的「说话人分离」转写 —— lecture-recorder 的 ASR 主脚本。

模型：SiliconFlow `XingChenAGI/XingChenASR-Diarize-V3.0`（官方价格页标免费；接口同 OpenAI transcriptions）。
返回：segments[] = {start, end, text, speaker}，秒级浮点时间戳 + 匿名说话人编号。

策略（2026-09-28 本机实测确定，详见 references/speaker-diarization.md）：
  * 时长 <= SAFE_LIMIT（默认 8500s ≈ 2h21m）→ **单片直传，不切分**。编号在同一请求内全局一致。
  * 时长 >  SAFE_LIMIT → 等分为 ceil(dur / (SAFE_LIMIT - overlap)) 片，非末片右端多留 `overlap` 秒
    （默认 300s）。片间用重叠区的「说话人聚合文本最长公共子串」对齐编号；对不上的标为未对齐。
    重叠区去重规则：按片序处理，后片中「时间上已被前片覆盖」的段丢弃，跨界段起点裁到覆盖终点。
    **每个时刻至少被一片覆盖，不静默丢内容**，交界处在稿内留显式标记。
  * 200/500/超时一律重试（500 是**偶发**的，实测原样重试即成功）。

产物（默认写到 <音频目录>/_transcripts/_diar/<音频名>/）：
  asr/req_%02d.json   原始返回，**必留**，供人工复核模型原话
  逐字稿.json          归一后的全局时间轴 + 说话人映射 + 对齐证据
  逐字稿.md            按轮次的对话稿（[HH:MM:SS] **说话人N**　正文）
  说话人对照表.md       编号/时长/段数/首末出现/样例句/后处理日志/人工确认栏
  _log.txt            运行日志（追加）

用法：
  PYTHON="python3"
  "$PYTHON" transcribe_diarize.py "input.m4a"
  "$PYTHON" transcribe_diarize.py in.m4a --dry-run                        # 只看切片计划
  "$PYTHON" transcribe_diarize.py in.m4a --alias 3=2,5=1                  # 人工指定 cluster 合并
  "$PYTHON" transcribe_diarize.py in.m4a --safe-limit 600 --overlap 120   # 强制走切片路径（测试用）
  "$PYTHON" transcribe_diarize.py in.m4a --no-noise-merge                 # 关闭噪声 cluster 合并
"""
from __future__ import annotations

import argparse
import datetime as _dt
import io
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------- 常量
ENDPOINT = "https://api.siliconflow.cn/v1/audio/transcriptions"
API_KEY = os.environ.get("SILICONFLOW_API_KEY", "").strip()
MODEL = "XingChenAGI/XingChenASR-Diarize-V3.0"

SAFE_LIMIT = 8500          # 单请求时长硬上限 ≈9000s，留余量
OVERLAP = 300              # 超限切片时片间重叠秒数（模型免费，重叠不增加费用）
MIN_LCS = 25               # 重叠区对齐可信阈值（归一化字数）
NOISE_MAX_SEC = 120.0      # 噪声 cluster 判定：总时长上限
NOISE_AVG_SEG = 3.0        # 噪声 cluster 判定：平均单段时长上限（秒）
NOISE_AVG_CHARS = 5.0      # 噪声 cluster 判定：平均单段字数上限

FFMPEG = None              # 运行时解析
FFPROBE = None

PUNCT = re.compile(r'[\s\u3000-\u303f\uff00-\uffef,.\-*\u2018\u2019\u201c\u201d]')


def log(msg, fh=None):
    line = str(msg)
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def die(msg):
    print("[FATAL] " + str(msg), file=sys.stderr, flush=True)
    sys.exit(2)


def hhmmss(sec):
    sec = max(0.0, float(sec))
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return "%02d:%02d:%02d" % (h, m, s)


# ---------------------------------------------------------------- 工具定位
def find_bin(name):
    """在 PATH 与 winget 安装目录里找 ffmpeg/ffprobe。"""
    from shutil import which
    p = which(name)
    if p:
        return p
    import glob
    pat = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet",
                       "Packages", "Gyan.FFmpeg*", "ffmpeg-*", "bin", name + ".exe")
    hits = glob.glob(pat)
    if hits:
        return hits[0]
    die("找不到 %s，请确认 ffmpeg 已安装并在 PATH 中" % name)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        die("命令失败：%s\n%s" % (" ".join(cmd), (r.stderr or "")[-1500:]))
    return r.stdout


# ---------------------------------------------------------------- 探测与切片
def probe_duration(path):
    out = run([FFPROBE, "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", path]).strip()
    try:
        return float(out)
    except ValueError:
        die("ffprobe 未返回时长：%r" % out)


def plan_split(duration, safe_limit=SAFE_LIMIT, overlap=OVERLAP):
    """返回 [(start, end), ...]，单片时即 [(0.0, duration)]。

    等分 n 片，非末片右端 +overlap；n 取 ceil(dur/(safe_limit-overlap))，
    保证「nominal + overlap <= safe_limit」。
    """
    if duration <= safe_limit:
        return [(0.0, duration)]
    step = safe_limit - overlap
    if step <= 0:
        die("--safe-limit 必须大于 --overlap")
    n = 1
    while duration / n > step:
        n += 1
    nominal = duration / n
    plan = []
    for i in range(n):
        a = i * nominal
        b = min(duration, (i + 1) * nominal + (overlap if i < n - 1 else 0.0))
        plan.append((round(a, 3), round(b, 3)))
    return plan


def extract(src, start, end, dst):
    """抽音 + 重采样 + 压成 32kbps 单声道 mp3（实测与 16k WAV 净字数等价，体积降 8 倍）。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"]
    if start > 0:
        cmd += ["-ss", "%.3f" % start]
    cmd += ["-t", "%.3f" % (end - start), "-i", src,
            "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", dst]
    run(cmd)
    return dst


# ---------------------------------------------------------------- ASR
def call_asr(audio_path, attempts=5):
    import requests
    last = None
    if not API_KEY:
        die("环境变量 SILICONFLOW_API_KEY 未设置。到 https://siliconflow.cn 申请 API Key 后 export SILICONFLOW_API_KEY=... 再运行。")
    for k in range(attempts):
        try:
            with open(audio_path, "rb") as fh:
                r = requests.post(
                    ENDPOINT,
                    files={"file": (os.path.basename(audio_path), fh, "audio/mpeg")},
                    data={"model": MODEL, "language": "zh"},
                    headers={"Authorization": "Bearer " + API_KEY},
                    timeout=3600)
            if r.status_code == 200:
                j = r.json()
                if j.get("segments"):
                    return j, None
                last = "200 但 segments 为空"
            else:
                last = "HTTP %d %s" % (r.status_code, r.text[:200])
        except Exception as e:                                   # noqa: BLE001
            last = "%s: %s" % (type(e).__name__, e)
        if k < attempts - 1:
            time.sleep(min(30, 5 * (k + 1)))                     # 500 属偶发，退避重试即可
    return None, last


# ---------------------------------------------------------------- 归一 / 对齐
def norm(s):
    return PUNCT.sub("", s)


def lcs_len(a, b):
    """最长公共子串长度（滚动数组）。"""
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    prev = [0] * (len(a) + 1)
    best = 0
    for ch in b:
        cur = [0] * (len(a) + 1)
        for i in range(1, len(a) + 1):
            if a[i - 1] == ch:
                cur[i] = prev[i - 1] + 1
                if cur[i] > best:
                    best = cur[i]
        prev = cur
    return best


def speakers_of(req):
    seen = []
    for s in req["segments"]:
        if s["speaker"] not in seen:
            seen.append(s["speaker"])
    return seen


def agg_text(req, spk, t0, t1):
    """某说话人在全局时间窗 [t0,t1) 内的话，拼接后归一化。"""
    off = req.get("_offset", 0.0)
    buf = []
    for s in req["segments"]:
        if s["speaker"] != spk:
            continue
        a, b = s["start"] + off, s["end"] + off
        if b > t0 and a < t1:
            buf.append(s["text"])
    return norm("".join(buf))


def align_req(prev_req, cur_req, ov_start, ov_end, min_lcs=MIN_LCS):
    """用重叠区把 cur 的本地编号映射到 prev 的本地编号。返回 (mapping, evidence)。"""
    mapping, evidence = {}, {}
    prev_spks, cur_spks = speakers_of(prev_req), speakers_of(cur_req)
    prev_txt = {p: agg_text(prev_req, p, ov_start, ov_end) for p in prev_spks}
    for c in cur_spks:
        ct = agg_text(cur_req, c, ov_start, ov_end)
        best, bestl = None, 0
        for p in prev_spks:
            l = lcs_len(ct, prev_txt[p])
            if l > bestl:
                best, bestl = p, l
        if best is not None and bestl >= min_lcs:
            mapping[c] = best
            evidence[c] = bestl
    return mapping, evidence


# ---------------------------------------------------------------- 说话人后处理
def build_profiles(segs):
    prof = {}
    for s in segs:
        p = prof.setdefault(s["_spk"], {"dur": 0.0, "n": 0, "chars": 0,
                                        "first": s["start"], "last": s["end"], "samples": []})
        p["dur"] += s["end"] - s["start"]
        p["n"] += 1
        p["chars"] += len(s["text"])
        p["first"] = min(p["first"], s["start"])
        p["last"] = max(p["last"], s["end"])
        if len(s["text"]) >= 6 and len(p["samples"]) < 6:
            p["samples"].append(s["text"])
    for p in prof.values():
        p["avg_seg"] = p["dur"] / p["n"] if p["n"] else 0.0
        p["avg_chars"] = p["chars"] / p["n"] if p["n"] else 0.0
    return prof


def is_noise(p, max_sec=NOISE_MAX_SEC, avg_seg=NOISE_AVG_SEG, avg_chars=NOISE_AVG_CHARS):
    """又短又碎 → 判定为噪声 cluster（短促插话／重叠话产生的碎片）。"""
    return p["dur"] < max_sec and p["avg_seg"] < avg_seg and p["avg_chars"] < avg_chars


def merge_noise(segs, profiles, enabled=True, **kw):
    """把噪声 cluster 并入时间上最近的正常 cluster。返回 (segs, notes)。"""
    notes = []
    if not enabled:
        return segs, notes
    major = {k: p for k, p in profiles.items() if not is_noise(p, **kw)}
    if not major:
        return segs, notes
    for k, p in list(profiles.items()):
        if k in major:
            continue
        votes = {}
        for s in segs:
            if s["_spk"] != k:
                continue
            best, bd = None, 1e18
            for m in major:
                for t in segs:
                    if t["_spk"] != m:
                        continue
                    d = abs((t["start"] + t["end"]) / 2 - (s["start"] + s["end"]) / 2)
                    if d < bd:
                        bd, best = d, m
            votes[best] = votes.get(best, 0) + 1
        if not votes:
            continue
        tgt = max(votes, key=votes.get)
        for s in segs:
            if s["_spk"] == k:
                s["_spk"] = tgt
        notes.append("自动合并噪声 cluster：%s（%.0fs / %d 段 / 均 %.1fs·%.1f 字）→ %s"
                     % (k, p["dur"], p["n"], p["avg_seg"], p["avg_chars"], tgt))
    return segs, notes


def apply_alias(segs, profiles, alias_spec):
    """--alias 3=2,5=1：按「首次出现时间序」的 1-based 编号合并（也接受内部键名）。"""
    notes = []
    if not alias_spec:
        return segs, notes
    order = sorted(profiles, key=lambda k: profiles[k]["first"])
    lookup = {}
    for i, k in enumerate(order, 1):
        lookup[str(i)] = k
        lookup[k] = k
    for pair in alias_spec.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            die("--alias 格式应为 3=2,5=1，收到 %r" % pair)
        src, dst = [x.strip() for x in pair.split("=", 1)]
        a, b = lookup.get(src), lookup.get(dst)
        if a is None or b is None:
            log("[WARN] --alias %s 引用了不存在的编号（现有：%s）"
                % (pair, ",".join(str(i) for i in range(1, len(order) + 1))))
            continue
        for s in segs:
            if s["_spk"] == a:
                s["_spk"] = b
        notes.append("人工指定合并 cluster：按首次出现序第 %s 位 → 第 %s 位" % (src, dst))
    return segs, notes


def renumber(segs, profiles):
    """按首次出现时间重排为 说话人1..N。返回 (order, remap)。"""
    order = sorted(profiles, key=lambda k: profiles[k]["first"])
    remap = {k: "说话人%d" % i for i, k in enumerate(order, 1)}
    for s in segs:
        s["speaker"] = remap[s["_spk"]]
    return order, remap


# ---------------------------------------------------------------- 主流程
def main():
    global FFMPEG, FFPROBE
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("audio")
    ap.add_argument("--out", default=None,
                    help="产物目录（默认 <音频目录>/_transcripts/_diar/<音频名>）")
    ap.add_argument("--safe-limit", type=float, default=SAFE_LIMIT)
    ap.add_argument("--overlap", type=float, default=OVERLAP)
    ap.add_argument("--cuts", default="",
                    help="按内容锚点指定切片点（秒，逗号分隔），如 \"5567,9000\"；"
                         "用于把一场完整的谈话放进同一片，优于机械等分")
    ap.add_argument("--min-lcs", type=int, default=MIN_LCS)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--alias", default="", help="人工合并 cluster，按首次出现序，如 3=2,5=1")
    ap.add_argument("--no-noise-merge", action="store_true")
    ap.add_argument("--keep-audio", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    FFMPEG, FFPROBE = find_bin("ffmpeg"), find_bin("ffprobe")

    src = os.path.abspath(args.audio)
    if not os.path.isfile(src):
        die("找不到音频：%s" % src)
    stem = os.path.splitext(os.path.basename(src))[0]
    out = os.path.abspath(args.out or os.path.join(
        os.path.dirname(src), "_transcripts", "_diar", stem))
    os.makedirs(os.path.join(out, "asr"), exist_ok=True)
    logfh = io.open(os.path.join(out, "_log.txt"), "a", encoding="utf-8")
    log("=" * 72, logfh)
    log("[%s] %s" % (_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), src), logfh)

    dur = probe_duration(src)
    if args.cuts.strip():
        pts = sorted(float(x) for x in args.cuts.split(",") if x.strip())
        pts = [p for p in pts if 0 < p < dur]
        bounds = [0.0] + pts + [dur]
        plan = []
        for k in range(len(bounds) - 1):
            a = bounds[k]
            b = min(dur, bounds[k + 1] + (args.overlap if k + 1 < len(bounds) - 1 else 0.0))
            if b - a > args.safe_limit:
                die("--cuts 指定后第 %d 片长达 %.0fs，超过 --safe-limit %.0fs，请增补切点"
                    % (k + 1, b - a, args.safe_limit))
            plan.append((round(a, 3), round(b, 3)))
        log("[INFO] 使用人工切点 %s" % pts, logfh)
    else:
        plan = plan_split(dur, args.safe_limit, args.overlap)
    log("[INFO] 时长 %.1fs (%.2fh) → 计划 %d 片" % (dur, dur / 3600, len(plan)), logfh)
    for i, (a, b) in enumerate(plan):
        log("       片%-2d %8.1f → %8.1f  (%.1fs)" % (i + 1, a, b, b - a), logfh)
    if args.dry_run:
        log("[INFO] --dry-run，未上传。", logfh)
        logfh.close()
        return 0

    # ---------- 抽取 + 上传（带片级缓存）
    adir = os.path.join(out, "_audio")
    jobs, reqs = [], [None] * len(plan)
    for i, (a, b) in enumerate(plan):
        raw = os.path.join(out, "asr", "req_%02d.json" % i)
        if os.path.exists(raw) and os.path.getsize(raw) > 0:
            jobs.append(("cached", i, raw, None))
        elif b - a <= 0.05:
            die("片%d 时长过短，无法抽取" % (i + 1))
        else:
            jobs.append(("new", i, raw, extract(src, a, b, os.path.join(adir, "seg_%02d.mp3" % i))))

    def work(job):
        kind, i, raw, apath = job
        if kind == "cached":
            log("[CACHE] 片%d 已有 ASR 结果，跳过" % (i + 1), logfh)
            return i, json.load(io.open(raw, encoding="utf-8")), None
        log("[UPLOAD] 片%d 上传中（%s，%.1fMB）"
            % (i + 1, os.path.basename(apath), os.path.getsize(apath) / 1e6), logfh)
        t0 = time.time()
        j, err = call_asr(apath, attempts=5)
        if j is None:
            return i, None, err
        io.open(raw, "w", encoding="utf-8").write(json.dumps(j, ensure_ascii=False))
        log("[OK] 片%d 用时 %.0fs，%d 段，%d 个说话人"
            % (i + 1, time.time() - t0, len(j["segments"]),
               len({s["speaker"] for s in j["segments"]})), logfh)
        return i, j, None

    with ThreadPoolExecutor(max_workers=(1 if len(plan) == 1 else args.workers)) as ex:
        for i, j, err in ex.map(work, jobs):
            if j is None:
                die("片%d 转写失败：%s" % (i + 1, err))
            reqs[i] = j

    if not args.keep_audio:
        import shutil
        shutil.rmtree(adir, ignore_errors=True)
    log("[INFO] 上传完成，开始归一与对齐", logfh)

    # ---------- 编号映射 + 全局时间轴
    info = {}
    for i, req in enumerate(reqs):
        info[i] = {"dur": float(req.get("duration") or (plan[i][1] - plan[i][0])),
                   "usage": req.get("usage")}
        req["_offset"] = plan[i][0]
    for i, req in enumerate(reqs):
        for s in req["segments"]:
            s["_a"] = s["start"] + plan[i][0]
            s["_b"] = s["end"] + plan[i][0]

    canon, evid, unaligned = {}, [], []
    for i, req in enumerate(reqs):
        spks = speakers_of(req)
        cmap = {}
        if i == 0:
            for k, sp in enumerate(spks, 1):
                cmap[sp] = "G%d" % k
        else:
            ov_a, ov_b = plan[i][0], plan[i - 1][1]
            mapping, ev = align_req(reqs[i - 1], req, ov_a, ov_b, args.min_lcs)
            used = {g for m in canon.values() for g in m.values()}
            for sp in spks:
                g = canon[i - 1].get(mapping.get(sp)) if sp in mapping else None
                if g:
                    cmap[sp] = g
                    evid.append("片%d 本地编号 %s → 沿用 %s（重叠区最长公共子串 %d 字 ≥ %d，可信）"
                                % (i + 1, sp, g, ev[sp], args.min_lcs))
                else:
                    n = 1
                    while "G%d" % n in used:
                        n += 1
                    used.add("G%d" % n)
                    cmap[sp] = "G%d" % n
                    unaligned.append(("G%d" % n))
                    evid.append("片%d 本地编号 %s → 新编号 %s（重叠区对齐失败，"
                                "**可能与前面某编号是同一人**，需人工听辨）" % (i + 1, sp, cmap[sp]))
        canon[i] = cmap

    segs, seams = [], []
    prev_end = -1.0
    for i, req in enumerate(reqs):
        chunk = sorted(req["segments"], key=lambda x: x["_a"])
        ov_end = plan[i - 1][1] if i > 0 else None
        sm = None

        def new_seam():
            return {"req": i + 1,
                    "overlap": [round(plan[i][0], 2), round(ov_end, 2)],
                    "kept_from": round(prev_end, 2),
                    "dropped_to": round(prev_end, 2)}

        for s in chunk:
            a, b = s["_a"], s["_b"]
            if i > 0:
                if b <= prev_end + 0.2:
                    if sm is None:
                        sm = new_seam()          # 该片第一次发生去重 = 片间交界
                    sm["dropped_to"] = max(sm["dropped_to"], round(b, 2))
                    continue                     # 已被前片覆盖 → 去重丢弃
                if a < prev_end:
                    if sm is None:
                        sm = new_seam()
                    a = prev_end                 # 跨接段：起点裁到覆盖终点
            if b - a < 0.15:
                continue
            segs.append({"start": round(a, 2), "end": round(b, 2), "text": s["text"].strip(),
                         "speaker": "", "_spk": canon[i][s["speaker"]], "_req": i})
            prev_end = max(prev_end, b)
        if sm is not None:
            seams.append(sm)
    segs.sort(key=lambda x: (x["start"], x["end"]))

    # 单调化收口：模型自身的时间戳偶有倒挂（实测同一请求内出现 0.04s 级重叠）。
    # 完全被覆盖的段把文本并入上一段（不丢字），轻微跨界段裁齐起点；大幅重叠会报警。
    fixed, overlaps = [], []
    for s in segs:
        if not fixed:
            fixed.append(s)
            continue
        p = fixed[-1]
        if s["end"] <= p["end"] + 0.05:
            p["text"] += s["text"]
            continue
        if s["start"] < p["end"]:
            overlaps.append((round(p["end"] - s["start"], 2), round(s["start"], 2)))
            s["start"] = p["end"]
        fixed.append(s)
    segs = fixed
    big = [o for o in overlaps if o[0] > 1.0]
    if overlaps:
        log("[INFO] 时间戳收口：%d 处轻微重叠已裁齐%s"
            % (len(overlaps), "；其中 %d 处 >1s 需留意" % len(big) if big else ""), logfh)

    # ---------- 后处理链
    profiles = build_profiles(segs)
    segs, n1 = merge_noise(segs, profiles, enabled=not args.no_noise_merge)
    profiles = build_profiles(segs)
    segs, n2 = apply_alias(segs, profiles, args.alias)
    profiles = build_profiles(segs)
    order, remap = renumber(segs, profiles)
    unaligned = [k for k in unaligned if k in profiles]   # 已被后续合并吃掉的编号不再提示
    notes = n1 + n2
    # 每个编号出现在哪些片里 —— 用于把「未锚定」讲清楚：
    # 未锚定＝该编号首次出现时没能和上一片对上，因此它**可能与更早的某个编号是同一人**；
    # 跨片对齐是链式的，一处断链就会把同一个人劈成两号，且不会自愈。
    key_reqs = {}
    for s in segs:
        key_reqs.setdefault(s["_spk"], set()).add(s["_req"])
    unanchor_info = {k: {"since": min(key_reqs[k]) + 1, "reqs": sorted(x + 1 for x in key_reqs[k])}
                     for k in unaligned}
    total = max([s["end"] for s in segs] + [0.0])
    covered = sum(s["end"] - s["start"] for s in segs)

    # ---------- 逐字稿.json
    data = {
        "source": src, "model": MODEL, "duration": round(dur, 2),
        "generated": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "safe_limit": args.safe_limit, "overlap_seconds": args.overlap,
        "plan": [{"start": a, "end": b} for a, b in plan],
        "requests": [{"file": "asr/req_%02d.json" % i, "duration": info[i]["dur"],
                      "usage": info[i]["usage"]} for i in range(len(reqs))],
        "speaker_map_by_request": [{"req": i + 1, "local_to_global": canon[i]}
                                   for i in range(len(reqs))],
        "speakers": {remap[k]: {"dur": round(profiles[k]["dur"], 1), "n": profiles[k]["n"],
                                "first": round(profiles[k]["first"], 2),
                                "last": round(profiles[k]["last"], 2),
                                "samples": profiles[k]["samples"],
                                "avg_seg": round(profiles[k]["avg_seg"], 2),
                                "avg_chars": round(profiles[k]["avg_chars"], 2),
                                "unaligned": k in unaligned,
                                "reqs": sorted(x + 1 for x in key_reqs.get(k, [])),
                                "unanchored_since": unanchor_info.get(k, {}).get("since"),
                                "fragment": (profiles[k]["dur"] < NOISE_MAX_SEC
                                             or profiles[k]["n"] <= 8)} for k in order},
        "segments": [{"start": s["start"], "end": s["end"], "speaker": s["speaker"],
                      "text": s["text"]} for s in segs],
        "postprocess": notes, "alignment": evid, "seams": seams,
        "clamped_overlaps": overlaps,
        "covered_ratio": round(covered / total, 4) if total else 0,
        "_keys": {remap[k]: k for k in order},
    }
    io.open(os.path.join(out, "逐字稿.json"), "w", encoding="utf-8").write(
        json.dumps(data, ensure_ascii=False, indent=1))

    # ---------- 逐字稿.md（按轮次）
    turns, cur = [], None
    for s in segs:
        if cur and cur["speaker"] == s["speaker"] and s["start"] - cur["end"] <= 3.0:
            cur["text"] += s["text"]
            cur["end"] = s["end"]
        else:
            cur = dict(s)
            turns.append(cur)
    seam_pts = sorted(s["kept_from"] for s in seams)
    seam_txt = {s["kept_from"]: s for s in seams}
    md = ["# 逐字稿（说话人分离）\n",
          "- **来源**：`%s`" % os.path.basename(src),
          "- **时长**：%s（%.1fs）" % (hhmmss(dur), dur),
          "- **模型**：`%s`　**转写时间**：%s" % (MODEL, data["generated"]),
          "- **切片**：%d 片%s" % (len(plan), "（整片直传，未切分）" if len(plan) == 1
                                   else "（片间重叠 %.0fs，交界处按片序去重）" % args.overlap),
          "- **说话人**：%d 人；有效语音占比 %.1f%%" % (len(order), data["covered_ratio"] * 100),
          "- **编号性质**：模型给的是**匿名聚类编号**，同一请求内一致；"
          "谁是谁须按内容判定，见 [[说话人对照表]]\n",
          "> ⚠️ 音频已上传第三方云端（api.siliconflow.cn）完成识别，存在录音外传。\n",
          "## 说话人一览\n",
          "| 编号 | 说话时长 | 段数 | 首次出现 | 末次出现 | 样例句 |",
          "|---|---|---|---|---|---|"]
    for k in order:
        p = profiles[k]
        flag = (" ✳️未锚定" if k in unaligned else "") + \
               (" ⚠️碎片" if (p["dur"] < NOISE_MAX_SEC or p["n"] <= 8) else "")
        md.append("| %s%s | %.0fs | %d | %s | %s | %s |"
                  % (remap[k], flag, p["dur"], p["n"], hhmmss(p["first"]),
                     hhmmss(p["last"]), (p["samples"][0][:34] if p["samples"] else "")))
    md.append("\n---\n")
    for t in turns:
        md.append("[%s] **%s**　%s\n" % (hhmmss(t["start"]), t["speaker"], t["text"]))
        for p in seam_pts:
            if t["start"] <= p <= t["end"]:
                s_ = seam_txt[p]
                md.append("> ⏭️ **片间交界**（第 %d 片起）：重叠区 %s；交界处已取前一片版本，"
                          "本片被覆盖的语音弃至 %s，**无内容删除**。\n"
                          % (s_["req"], "–".join(hhmmss(x) for x in s_["overlap"]),
                             hhmmss(s_["dropped_to"])))
    io.open(os.path.join(out, "逐字稿.md"), "w", encoding="utf-8").write("\n".join(md))

    # ---------- 说话人对照表.md
    # 各片编号分布：跨片对齐只能救回「在重叠窗口说过话的人」，其余编号必须靠人工按内容归并。
    # 这张表把每片每个本地编号的时段与样例句摊开，是人工归并的唯一抓手。
    slice_tbl = []
    if len(plan) > 1:
        for i, req in enumerate(reqs):
            agg = {}
            for s in req["segments"]:
                a = s["start"] + plan[i][0]
                p = agg.setdefault(s["speaker"], {"dur": 0.0, "n": 0, "first": a, "last": a,
                                                  "samples": []})
                p["dur"] += s["end"] - s["start"]
                p["n"] += 1
                p["first"] = min(p["first"], a)
                p["last"] = max(p["last"], a)
                if len(s["text"]) >= 8 and len(p["samples"]) < 3:
                    p["samples"].append(s["text"])
            slice_tbl.append((i + 1, plan[i], agg, canon[i]))

    c = ["# 说话人对照表\n",
         "> 用途：把左列编号改成真实姓名／角色后，替换 `逐字稿.md` 与笔记里的编号即可。",
         "> 模型只做匿名聚类，**编号 ≠ 身份**；本表只给「判断依据」，不替你落名。\n",
         "| 编号 | 说话时长 | 段数 | 首/末出现 | 已判定为 | 模型内编号 | 样例句 |",
         "|---|---|---|---|---|---|---|"]
    for k in order:
        p = profiles[k]
        mp = ["片%d 本地%s" % (e["req"], i)
              for e in data["speaker_map_by_request"] for i, g in e["local_to_global"].items()
              if g == k]
        flags = (" ⚠️碎片" if p["dur"] < NOISE_MAX_SEC or p["n"] <= 8 else "") + \
                (" ✳️未锚定" if k in unaligned else "")
        c.append("| %s%s | %.0fs (%.0f%%) | %d | %s / %s | *（待填）* | %s | %s |"
                 % (remap[k], flags, p["dur"], 100 * p["dur"] / max(1, covered), p["n"],
                    hhmmss(p["first"]), hhmmss(p["last"]),
                    "、".join(mp) if mp else "-",
                    (p["samples"][0][:56] if p["samples"] else "")))
    c.append("\n> 标记含义：**⚠️碎片** = 又短又碎，很可能是噪声 cluster（可 `--alias N=M` 并入相邻编号）；"
             "**✳️未锚定** = 首次出现时没能与上一片对齐，**可能与更早的某个编号是同一人**。")
    c.append("\n### 样例句（每人最多 6 条）\n")
    for k in order:
        c.append("**%s**" % remap[k])
        c.extend("- " + s[:120] for s in profiles[k]["samples"])
        c.append("")
    if slice_tbl:
        c.append("---\n")
        c.append("### 各片编号分布（跨片归并的唯一依据）\n")
        if unaligned:
            c.append("> ⚠️ **跨片对齐只能救回「在片间重叠窗口里说过话的人」。**"
                     "本次重叠区只覆盖到少数编号，下列 %s 号未能对齐——"
                     "它们**各自是独立的 cluster，但可能与片 1 的某编号是同一人**。\n"
                     % "、".join(remap.get(k, k) for k in unaligned))
            c.append("> **归并方法**：拿下面同一行的「时段 + 样例句 + 常说的话题」去比对"
                     "片 1 的编号。**典型情形**：全程在提问、反复说「你叫什么名字」「介绍一下成长经历」的那个编号，"
                     "每一片都会有它——那把各片的那一号并成一个即可。\n")
        for no, pl, agg, cmap in slice_tbl:
            c.append("**片%d**（%s–%s）\n" % (no, hhmmss(pl[0]), hhmmss(pl[1])))
            c.append("| 片内编号 | → 全局编号 | 说话时长 | 段数 | 时段 | 样例句 |")
            c.append("|---|---|---|---|---|---|")
            for sk in sorted(agg, key=lambda x: -agg[x]["dur"]):
                p = agg[sk]
                c.append("| %s | %s | %.0fs | %d | %s–%s | %s |"
                         % (sk, remap.get(cmap[sk], cmap[sk] + "（已并入他人）"), p["dur"], p["n"],
                            hhmmss(p["first"]), hhmmss(p["last"]),
                            "／".join(x[:38] for x in p["samples"])))
            c.append("")
    c.extend(["---\n", "### 后处理记录\n"])
    steps = ["**编号重排**（按首次出现时间）：%s"
             % "；".join("%s → %s" % (k, remap[k]) for k in order)]
    steps += notes or ["未做 cluster 合并（无噪声 cluster，也未指定 `--alias`）"]
    steps.append("**跨片对齐**：%s" % ("单片请求，无需对齐" if len(plan) == 1
                                  else "详见 `_log.txt` 与 `逐字稿.json` 的 alignment 字段"))
    c.extend("%d. %s" % (i, s) for i, s in enumerate(steps, 1))
    if unaligned:
        c.append("\n> ⚠️ **跨片对齐是链式的：一处断链就会把同一个人劈成两号，且不会自愈。**"
                 "下列编号首次出现时没能和上一片对上，**可能与更早的某个编号是同一人**：\n")
        for k in unaligned:
            info = unanchor_info.get(k, {})
            rs = info.get("reqs", [])
            c.append("> - **%s**：首次出现于片%d（未对齐）；出现在片 %s"
                     % (remap.get(k, k), info.get("since", 0),
                        "、".join(str(x) for x in rs) or "-"))
        c.append(">\n> 归并办法：拿它们的样例句与「各片编号分布」去比对更早的编号"
                 "（典型的**主持人**编号会横跨全片，且在每场的开场/串场句里出现）；"
                 "确认后用 `--alias` 固化，**判不准就留着**。")
    c.append("\n### 已知缺陷（模型侧，非脚本问题）\n")
    c.append("1. 同一人可能被拆成两个 cluster；本脚本只自动合并「又短又碎」的噪声 cluster。")
    c.append("2. 短促插话／重叠话音会产生噪声 cluster。")
    c.append("3. **录音越长、说话人越多，过分割越明显**：实测 2.93h 录音切 2 片后，"
             "片 1 分出 6 个编号、片 2 分出 9 个，而实际只有 1 位谈话人 + 若干申请人。"
             "**片内编号数 ≠ 真实人数**。")
    c.append("4. 跨片重叠区对齐只能救回在重叠窗口内说过话的人（典型只有 2 人：谈话人 + 当时正在作答的申请人）；"
             "**其余编号必须按上面的「各片编号分布」人工归并**。")
    c.append("5. 对齐是**链式**的：片3 与片2 的对齐失败，片3 那个编号就永远拿不到片1/片2 的编号身份——"
             "即使它其实就是同一个人。**一处断链不会自愈**，只能人工并。")
    c.append("6. 本脚本只保证「同一请求内同一编号＝同一人」，**不判定真实身份**。")
    io.open(os.path.join(out, "说话人对照表.md"), "w", encoding="utf-8").write("\n".join(c))

    log("[DONE] %d 段 / %d 轮 / %d 说话人 / 有效语音 %.1f%%"
        % (len(segs), len(turns), len(order), data["covered_ratio"] * 100), logfh)
    log("[OUT] %s" % out, logfh)
    logfh.close()
    print("\n产物目录：%s" % out)
    print("  逐字稿.md / 逐字稿.json / 说话人对照表.md / asr/req_*.json / _log.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
