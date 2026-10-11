---
name: lecture-recorder
displayName: Lecture Recorder
slug: lecture-recorder
version: 1.0.0
description: "Turn lecture, meeting, and research-interview recordings (MP4/m4a/mp3) into structured Markdown study notes. Produces a speaker-diarized verbatim transcript with second-level timestamps; a standalone 逐字稿 plus 说话人对照表 for meetings and talks; slide text and full-page slide screenshots embedded at semantically matching positions; step-by-step LaTeX math derivations with mandatory variable annotation; and condensed exam-review key points. Cloud ASR via SiliconFlow (XingChenASR-Diarize, speaker diarization built in), with a local Whisper fallback. When to use: 处理录播, 整理课程录音, 转录课堂视频, lecture notes, 说话人分离, 分辨谁在说, 会议录音转写, speaker diarization."
---

# Lecture Recorder — 课堂录播处理

## Overview

End-to-end pipeline that turns a raw lecture/meeting recording + course slides into a structured `.md` study note containing:
- **Speaker-diarized** verbatim transcript with second-level timestamps
- A standalone 《逐字稿》 + 《说话人对照表》 for meetings/talks (see `references/speaker-diarization.md`)
- Slide content embedded at semantically matching positions
- **Chart/diagram screenshots** extracted from PDF slides (when professor explains them)
- Mathematical model derivations in LaTeX (self-contained; optionally delegated to a derivation skill if you have one)
- **Mandatory variable annotation** for all formulas (each symbol defined; AI infers if professor omits)
- Condensed exam review key points

**ASR 主模型已换成自带说话人分离的 `XingChenAGI/XingChenASR-Diarize-V3.0`**（2026-09-28），
所有场景（含讲授型课堂笔记）统一用它。选型依据、实测数据与硬约束见
[`references/speaker-diarization.md`](references/speaker-diarization.md) —— **动手前先读那一份**。

## 依赖与环境

- **ffmpeg / ffprobe**：必须在 PATH 上（`ffmpeg -version` 能跑）。脚本也会自动探测 Windows winget 的安装目录。
- **Python 3.9+**，需要 `requests`（ASR 上传）、`pymupdf`（课件整页渲染）、`pdfplumber`（PDF 取字）、`python-pptx`（PPTX 取字）。兜底的本地 whisper 路径另需 `openai-whisper`（可选，不装不影响云端主路径）。

```bash
pip install -r scripts/requirements.txt
```

下文用 `$PYTHON` 指代你的解释器、`<SKILL_DIR>` 指代本技能目录。调用前先设好：

```bash
PYTHON="python3"                        # 或你的绝对路径
SKILL_DIR="/path/to/lecture-recorder"   # 本技能所在目录
```

**⚠️ Windows/Git-Bash 路径坑（必读）**：给 ffmpeg 这类**原生 Windows 程序**传输出路径时，**不要写 `/c/Users/...` 这种 MSYS 路径**。当 cwd 在非 C 盘时，MSYS 会把 `/c/Users/...` 改写成 `E:\c\Users\...`，ffmpeg 立刻报 `Failed to open segment` / `Permission denied`。**一律写带盘符的显式路径**：

```bash
# ❌ 会被改写成 E:\c\Users\... 而失败
ffmpeg -i in.mp3 ... "/c/Users/me/AppData/Local/Temp/x/seg_%02d.wav"
# ✅ 显式盘符
ffmpeg -i in.mp3 ... "C:/Users/me/AppData/Local/Temp/x/seg_%02d.wav"
```

`ls` / `mkdir` 等走 MSYS 的命令不受影响——只有传给原生 exe 的**参数**会被改写。

**⚠️ 抽音一步到位，但默认不切分**：用一次 ffmpeg 同时完成重采样与降声道即可，**不要 `-f segment`**（见下节：整片优先）：

```bash
ffmpeg -hide_banner -loglevel error -i "in.mp4" -vn -ac 1 -ar 16000 -b:a 32k "C:/.../temp/audio.mp3" -y
```

**所有音频中间产物放系统临时目录**，跑完即删，别留在工作目录里。跑完后连同 `asr/req_*.json` 缓存、切片计划与 `_log.txt` 一并清掉——只把最终成稿留在工作目录里。

## ASR 选型（本机实测，2026-09-28 更新）

**主模型：`XingChenAGI/XingChenASR-Diarize-V3.0`（SiliconFlow，自带说话人分离，官方价格页标免费）。**
端点与鉴权跟原来的 TeleSpeechASR 完全一样：`https://api.siliconflow.cn/v1/audio/transcriptions`，
`model` + `language=zh` + `file`。**所有场景（讲授型课堂笔记、会议、调研、谈话）一律用它。**

**不要手写 curl / requests 去拼这套流程——直接调用主脚本：**

```bash
PYTHON="python3"
SKILLS="<SKILL_DIR>"
"$PYTHON" "$SKILLS/scripts/transcribe_diarize.py" "<音频路径>" [--out <目录>] [--dry-run]

# 产出（默认 <音频目录>/_transcripts/_diar/<音频名>/）：
#   asr/req_%02d.json   原始返回（必留，供复核）
#   逐字稿.json          全局时间轴 + 说话人映射
#   逐字稿.md            按轮次对话稿：[HH:MM:SS] **说话人N**　正文
#   说话人对照表.md       编号/时长/段数/首末出现/样例句/后处理日志/人工确认栏
#   _log.txt            运行日志（追加）

# 常用开关
--dry-run                只看切片计划，不上传
--safe-limit 8500        单请求时长上限。**不要为了"保险"调小**——片越多、跨片未锚定编号越多
--cuts "5567"            人工指定切片点（秒）：把切点钉在「场次边界」上，不要机械等分
--overlap 300            切片时的片间重叠秒数
--alias 5=2              人工把第5个 cluster 并进第2个（按首次出现序）——跨片同人的固化手段
--no-noise-merge         关闭噪声 cluster 自动合并
--keep-audio             保留中间 mp3（默认删）
```

**五条硬约束（都是踩出来的，别绕）**：

1. **单请求时长上限 ≈9000 秒**，**与文件体积无关**（115MB 的 1h WAV ✅、68MB 的 2h MP3 ✅、9000s 的 36MB ❌）。
   工程安全值 **8500s（≈2h21m）**。所以：**≤8500s 一律单片直传，彻底不切分**；超过才切
   `ceil(时长/(8500−overlap))` 片（3h → **2 片**）。
2. **`speaker_count` / `response_format` 等参数一律无效**，返回结构固定自带 `segments`。别写。
3. **500 是偶发的，必须重试**（实测 5194s 的片首次 500、原样重试即 200）。脚本内已做 5 次退避。
4. **编号只保证「同一请求内同一编号＝同一人」**，跨请求必然漂移——这正是取消切割的核心收益。
   超限切片时必须用重叠区对齐；对不上的编号脚本会**显式标注「未锚定」并在对照表里写明它出现在哪几片**，不许静默合并。
5. **模型自身时间戳偶有 0.04s 级倒挂**，脚本做单调化收口；不要因为「看着不顺」就自己重排。

**上传体积与编码**：**mp3 32kbps 单声道足够**（1h→14.4MB，净字数与 16k WAV 等价）。
单请求 5–35MB 都验证过 200。

**吞吐**：≈28–35× 实时。20 分钟录音 **29 秒**；2.4 小时约 4 分钟。整片跑比切 18 片更快，且省去合并。

**成稿后必跑校验**（五项：请求完整性/时间轴/说话人/去重账/成稿一致性）：

```bash
"$PYTHON" "$SKILLS/references/verify_diarized.py" "<产物目录>"
```

**兜底：本地 whisper**（仅在 API 不可用时；**不产说话人标签**）：

```bash
"$PYTHON" -m whisper "_cache/.audio.wav" --model medium --language zh \
  --output_dir "_transcripts" --output_format txt
```

成本约 6–8 小时/3 小时音频（CPU），中文长讲课质量不如云端，唯一优点是零外传。

**明确不做：本地说话人分离。** 本机无 funasr/modelscope/torchaudio，torch 是 CPU 版；
云端免费且快一个数量级。理由详见 `references/speaker-diarization.md` 第 7 节。

### 切片策略：**片数越少越好**，切点要落在场次边界

**核心结论（2.93h 多对象依次访谈实测，1 位访谈人 + 6 位受访者依次）：**

| 切法 | 各片簇数 | 合并后全局编号 | 未锚定 | 判定 |
|---|---|---|---|---|
| 整片 | — | — | — | 不可能（>9000s 直接 500） |
| **等分 2 片**（5567s / 5267s） | 6 / 9 | **13** | **7（54%）** | ✅ 相对更好 |
| 等分 7 片（各 ~1505s） | 3,3,3,4,4,4,5 | **15** | **13（87%）** | ❌ 更差 |

**反直觉但已复现的结论**：把片切小，**局部**聚类确实更干净（7 片时每片只有 3–4 人，符合实际），
但**全局**结果更糟——因为①切得越多，锚定链越长，断链机会越多；②重叠区里能对上的人始终只有
「当时正在说话的那 2 个」，多切几刀就多出几处对不上的编号。
**衡量质量看「全局编号数 + 未锚定比例」，不要看「每片几个人」。**

**所以：**

1. **能一片就一片**（≤8500s）。片越少 → 锚定链越短 → 编号越干净。
2. 必须切时，**片数取最小值**（`ceil(时长/(8500−overlap))`，3h → 2 片）。
3. `--cuts` 的正确定位**不是「切更多」，而是「让切点落在场次边界上」**——找一次谈话结束、
   下一位还没开口的间隙（通常有 20–60 秒静音，或出现「谢谢」「下一个」这类串场语）。
   这样同一位谈话人的编号链不会在最密集的对话中途断开，且重叠区里他一定在说话（他在串场），
   对齐成功率最高。

```bash
"$PYTHON" "$SKILLS/scripts/transcribe_diarize.py" "in.m4a" --dry-run          # 先看时间轴与默认切点
"$PYTHON" "$SKILLS/scripts/transcribe_diarize.py" "in.m4a" --cuts "5567"      # 把切点钉在场次边界
```

> ⚠️ **片内编号数 ≠ 真实人数**。落名前必须按《说话人对照表》的「各片编号分布」逐行比对
> （时段 + 样例句 + 常说的话题），把其实是同一个人的编号手工并到一起，再用 `--alias` 固化。
> 判不准就留着并标 `（说话人待确认）`，**不要硬凑成「看起来合理」的人数**。

### 结果怎么用：说话人编号不是身份

模型给的是**匿名聚类编号**，脚本按首次出现时间重排为 `说话人1/2/…`。谁是谁必须按内容判定：

- 成稿里**必须附《说话人对照表》**（脚本已自动生成），列出每人的说话时长、段数、首末出现时间、
  最多 6 条样例句，以及「模型内编号 ↔ 全局编号」的映射。这是用户自行校验的唯一抓手。
- 表里给「判断依据」，**不替你落名**；人名/角色一栏留空，由用户填。
- 判断时优先看**称呼与应答关系**（谁在被点名、谁在回答、谁说“我这边”“下一个”），
  其次看**席位与时序**。判不准的写 `（说话人不确定）`，**绝不硬编**。

## Folder Structure

The skill operates in a workspace folder (e.g., `课件与录音/`). Files are organized into a clean separation of original materials, intermediate cache, transcripts, and final output:

```
课件与录音/                          # Workspace root
├── 原始视频/                        # [User places here] Original MP4 recordings
│   └── 课程名 - YYYY-MM-DD第X-Y节 - 教师名.mp4
├── 原始课件/                        # [User places here] Original PDF/PPTX slides
│   ├── 课程名第一讲.pptx
│   ├── 课程名2.1讲.pdf
│   └── ...
├── _cache/                          # [Auto] Slide text caches & audio temps (cleaned)
│   └── .slides_cache_*.md
├── _images/                         # [Auto] Extracted chart/diagram screenshots
│   └── chart_*.png
├── _transcripts/                    # [Auto] Transcripts
│   ├── YYYY-MM-DD-第X节-逐字稿.md
│   └── _diar/<录音名>/              # 说话人分离产物（逐字稿.md/.json、说话人对照表.md、asr/）
└── YYYY-MM-DD-第X讲-主题.md         # [Final output] Study note at workspace root
```

**Rules**:
1. **原始视频/** and **原始课件/** — user places originals here before invoking the skill. Never modified.
2. **\_cache/** — slide text caches, auto-generated and kept for reuse across runs.
3. **\_transcripts/** — 主脚本默认输出到 `<音频目录>/_transcripts/_diar/<录音名>/`；笔记定稿后再按需
   把 `逐字稿.md` 复制/改名为 `_transcripts/YYYY-MM-DD-第X节-逐字稿.md`。**一个讲次一份稿**。
4. **Final note** — placed at workspace root. Contains a wikilink to the transcript at the top: `> 📝 原始逐字稿：[[_transcripts/YYYY-MM-DD-第X节-逐字稿|查看完整转写稿]]`.
5. **`asr/req_*.json` 必须保留** —— 那是模型原始返回，用户复核「你改了什么」的唯一依据。
6. **Audio intermediates**（中间 mp3）— 主脚本默认跑完即删（`--keep-audio` 可留）。

## API Configuration

**SiliconFlow API** (for cloud ASR transcription):
- **API Key**: 通过环境变量 `SILICONFLOW_API_KEY` 提供，**不要写进任何文件**：
  ```bash
  export SILICONFLOW_API_KEY="sk-..."     # 到 https://siliconflow.cn 申请
  ```
  主脚本在缺这个变量时会直接报错退出。
- **Base URL**: `https://api.siliconflow.cn/v1`
- **Model**: `XingChenAGI/XingChenASR-Diarize-V3.0`（自带说话人分离；**官方价格页标注免费**）
- ⚠️ 旧模型 `TeleAI/TeleSpeechASR` **已从 `/v1/models` 下架**（仍可调用但随时可能失效），**不要再用**。
- **计费无法从 API 自证**（余额接口已废弃）：对外只说「官方价格页标免费」，**不要承诺「绝对不扣费」**。
- ⚠️ **隐私**：音频会上传第三方云端（`api.siliconflow.cn`）。处理敏感录音前先告知使用者。

## Prerequisites Check

Before starting, verify tools exist. **本机已装齐（见上节「环境与解释器」），无需安装。** 仅当确实缺失时才装：

```bash
# ffmpeg — 已装，PATH 可用
ffmpeg -version

# python 包已在系统 Python 3.9，无需再装；如确需安装，务必指定该解释器：
PYTHON="python3"
"$PYTHON" -c "import whisper, markitdown, pymupdf, pdfplumber, pptx, requests; print('deps OK')"
```

**⚠️ Windows winget users**: ffmpeg installs to a deep path under `AppData\Local\Microsoft\WinGet\Packages\` and may NOT be auto-added to PATH. Before running whisper, add it manually:

```bash
# Find ffmpeg location
find /c/Users/$USER/AppData/Local/Microsoft/WinGet/Packages -name ffmpeg.exe -type f

# Add to PATH (adjust path to match your version)
export PATH="/c/Users/$USER/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_*/ffmpeg-*/bin:$PATH"
```

Verify with: `ffmpeg -version` and `"$PYTHON" -c "import whisper; print('OK')"`

## Pipeline

Execute these steps sequentially. Use cached intermediate files when they exist.

### Step 1: Discover Files

Scan the workspace and subdirectories:
- `原始视频/*.mp4` — recordings
- `原始课件/*.pdf`, `原始课件/*.pptx` — slides

Match by filename hints (date, lecture number, topic keywords). If files are at root level (not yet organized), detect and offer to move them into `原始视频/` and `原始课件/`. If ambiguous, present candidates and ask the user to confirm.

### Step 2: Extract Audio

**不必单独抽音**——主脚本内部会按需抽片。只有当你需要自己先看一眼音频时：

```bash
ffmpeg -hide_banner -loglevel error -i "原始视频/input.mp4" -vn -ac 1 -ar 16000 -b:a 32k "C:/.../temp/audio.mp3" -y
```

输出路径**必须带盘符**（见上文「Windows/Git-Bash 路径坑」）。

### Step 3: Transcribe Audio（说话人分离）

**一条命令搞定整片优先切片、上传、对齐、后处理：**

```bash
PYTHON="python3"
SKILLS="<SKILL_DIR>"

"$PYTHON" "$SKILLS/scripts/transcribe_diarize.py" "原始视频/input.mp4" \
  --out "课件与录音/_transcripts/_diar/<录音名>"
```

脚本会自己：`ffprobe` 探长 → 判定整片/切片 → ffmpeg 切 mp3 → 并发上传（片级缓存 + 5 次退避重试）
→ 重叠区对齐编号 → 噪声 cluster 合并 → 按首次出现时间重排 → 落四份产物。

**不要手写 requests 拼接口**（老版本 SKILL 里的并发示例已废弃，脚本内实现更完整、且带缓存与对齐）。
需要改行为时改脚本参数，不要另起一份代码。

**先看计划再上传**（大文件建议先跑一次）：

```bash
"$PYTHON" "$SKILLS/scripts/transcribe_diarize.py" "in.m4a" --dry-run
```

**成稿后必跑校验**（五项，任一 ❌ 都要回头查）：

```bash
"$PYTHON" "$SKILLS/references/verify_diarized.py" "课件与录音/_transcripts/_diar/<录音名>"
```

**Model comparison**:
- `XingChenAGI/XingChenASR-Diarize-V3.0` — **默认首选**，自带说话人分离 + 秒级时间戳，≈30× 实时
- `XingChenASR-V3.2 / V3.2-Ultra` — 同表免费，**无说话人标签**；仅在明确不需要区分说话人时考虑
- `FunAudioLLM/SenseVoiceSmall` — 更快但质量更差，长音频会乱
- ⚠️ `TeleAI/TeleSpeechASR` — **已下架**，勿用
- Local `whisper` (medium) — 零外传但 CPU 上 3h 音频要 6–8 小时，**且无说话人标签**

**Fallback — Local whisper** (only if API unavailable):
```bash
PYTHON="python3"
"$PYTHON" -m whisper "_cache/.audio.wav" --model medium --language zh \
  --output_dir "_transcripts" --output_format txt
```

**Cache check**: 主脚本自带片级缓存（`asr/req_%02d.json` 存在即跳过），重跑秒级完成。

**Audio intermediates**: 主脚本跑完自动删除中间 mp3（`--keep-audio` 可保留）。

### Step 4: Extract Slide Content

**PPTX**: Use `python-pptx` or `python -m markitdown`:

```python
from pptx import Presentation
prs = Presentation("slides.pptx")
for slide in prs.slides:
    for shape in slide.shapes:
        if shape.has_text_frame:
            # extract text
```

**PDF**: Use `pdfplumber`:

```python
import pdfplumber
with pdfplumber.open("slides.pdf") as pdf:
    for page in pdf.pages:
        text = page.extract_text()
```

Save extracted content to `.slides_cache_*.md` for reuse.

#### 4b: Extract Slide Screenshots & Embed in Notes

**For every lecture, extract screenshots of ALL key content slides as full-page renders** and embed them into the final note at the corresponding sections. Screenshots dramatically improve study notes by preserving the exact visual layout, formulas, and diagrams that the professor presented. This should be done for every slide deck used in the lecture — not just the ones the professor explicitly discusses.

##### Page Selection Strategy

Extract a curated subset of slides from each PDF, prioritizing:

| Priority | When to extract | Examples |
|----------|-----------------|---------|
| **Always** | Title slide with course structure/outline | Content outline, teaching plan |
| **Always** | Definitions and key concept slides | Model setup, market structure assumptions, theorem statements |
| **Always** | Formula/derivation slides with LaTeX equations | FOC conditions, MRS conditions, equilibrium conditions |
| **Always** | Theorem/result summary slides | Welfare theorems, key conclusions |
| **When discussed** | Charts, graphs, diagrams explained by professor | Supply-demand curves, utility possibility frontiers, DWL triangles |
| **When discussed** | Data tables, empirical results | Regression tables, distribution graphs |

Extract **8-15 pages per slide deck** (dense formula-heavy decks need more). A good rule: if the slide text cache entry for a page contains formulas or structured content, that page is a candidate.

##### Extraction Method — PyMuPDF Full-Page Rendering

**Always use full-page rendering** (`get_pixmap`). Do NOT attempt to extract embedded raster images (they are often low-res or non-existent for LaTeX-generated formula slides). Full-page renders at 200 DPI capture everything perfectly.

```python
import pymupdf  # NOTE: use 'import pymupdf', NOT 'import fitz'

doc = pymupdf.open("原始课件/slides.pdf")

# Map page numbers to short English topic keywords (ASCII-safe!)
# Page numbers are 1-indexed in the PDF
key_pages = {
    4:  'model_setup',           # Page 4: 经济结构与完美市场假定
    5:  'consumer_choice_MRS',   # Page 5: 消费选择与MRS条件
    7:  'equilibrium_walras',    # Page 7: 市场均衡与瓦尔拉斯法则
    8:  'competitive_eq',        # Page 8: 竞争均衡定义
    10: 'pareto_definition',     # Page 10: 帕累托有效定义
    11: 'pareto_optimization',   # Page 11: 社会福利最大化
    14: 'welfare_thm_1',         # Page 14: 福利经济学第一定理
}

for pnum, keyword in key_pages.items():
    page = doc[pnum - 1]  # 0-indexed
    pix = page.get_pixmap(dpi=200)
    # Naming: s{lecture_num}_p{page:02d}_{keyword}.png
    fname = f"s2_1_p{pnum:02d}_{keyword}.png"
    pix.save(f"_images/{fname}")

doc.close()
```

**CRITICAL — Filename Encoding Rule**:
- **Always use ASCII-only filenames** (English letters, digits, underscores). Example: `s2_1_p04_model_setup.png`
- **NEVER use Chinese characters in image filenames**. Chinese filenames WILL get garbled (mojibake) when passing through shell commands, Python subprocess, or the Obsidian Glob tool on Windows, causing read failures and broken embeds.
- Use short English keywords that describe the slide content (e.g., `model_setup`, `consumer_choice_MRS`, `welfare_thm_1`)
- Keep a comment mapping each keyword to its Chinese meaning in the extraction script (see example above)

**Naming Convention**: `s{deck_num}_p{page:02d}_{topic_keyword}.png`
- `s2_1` = 第2.1讲 slides, `s2_2` = 第2.2讲 slides
- `p04` = page 4 (zero-padded to 2 digits)
- `model_setup` = short descriptive keyword in English

**CRITICAL — Obsidian 嵌入路径必须是「仓库根相对路径」**：
`![[_images/foo.png]]` 这种写法**只有在课程文件夹本身就是 Obsidian 仓库根时**才有效。当课程文件夹是仓库的子目录（例如本机仓库根 = `<vault-root>`，课程在 `<vault-root>\2-7-大四上\课程名\`）时，Obsidian 不会把 `_images/foo.png` 匹配到 `课程目录/课程名/_images/foo.png`，图片直接裂开。

**正确写法：从仓库根写全路径。** 先确认仓库根（`ls -d <path>/.obsidian`），再写：

```markdown
> ![[课程目录/课程名/_images/s1_p04_clash_thesis.png]]
```

同理，笔记顶部的逐字稿 wikilink 也要写全：

```markdown
> 📝 原始逐字稿：[[课程目录/课程名/_transcripts/2026-09-08-第1节-逐字稿|查看完整转写稿]]
```

写完笔记后务必自检一遍引用完整性：

```bash
grep -o '_images/[a-zA-Z0-9_]*\.png' note.md | sed 's|_images/||' | sort -u > /tmp/ref.txt
ls _images > /tmp/have.txt
comm -23 /tmp/ref.txt /tmp/have.txt   # 输出为空 = 无缺失
```

##### Embedding in Notes — Three-Part Pattern

After the corresponding `📄 课件` block quote and **before** the next section heading, insert:

```markdown
> **📊 课件截图（Page X）**
> ![[_images/s2_1_p04_model_setup.png]]
> *图：经济结构模型设定——两种商品、两个消费者、禀赋、竞争性市场价格向量*
```

This three-part pattern consists of:
1. **Header line**: `> **📊 课件截图（Page X）**` — identifies the source slide
2. **Wiki-link embed**: `> ![[_images/filename.png]]` — Obsidian-native image embed (must be inside a blockquote with `>` to keep visual consistency, though the `>` before `![[` is optional and may be omitted if the image should render at full width)
3. **Caption line**: `> *图：Chinese description of what this slide shows*` — always provide a descriptive caption in Chinese so readers understand the slide content without opening the image

##### Insertion Workflow

After generating the final note body (Step 7), do a second pass to insert screenshots:

1. **Pre-extract all images** into `_images/` before writing the note (or during Step 4)
2. **Write the note body** first (Steps 5-7)
3. **Read back the note** to find insertion points — locate each `📄 课件（Page X）` reference
4. **Insert the three-part embed block** immediately after the corresponding `📄 课件` block quote, before the next heading or text
5. For pages that are referenced as a range (e.g., “课件 Page 10-12”), insert the screenshot at the first page reference

**Multiple images for one section**: When showing complementary slides (e.g., perfect competition pricing AND markup/markdown definitions), stack them:

```markdown
> **📊 课件截图（2.2讲 Page 8-9）**
> ![[_images/s2_2_p08_perfect_comp_pricing.png]]
> *图（上）：完全竞争企业定价——$p = c$，价格等于边际成本*
> ![[_images/s2_2_p09_markup_markdown.png]]
> *图（下）：市场势力的两个核心指标——产品市场加成（Markup）与要素市场减价（Markdown）*
```

##### DPI and Quality

- **200 DPI** is the recommended resolution — sufficient for reading all text and formulas clearly, while keeping file sizes reasonable (~50-120KB per slide)
- 150 DPI is acceptable for text-heavy slides; 300 DPI for diagram-heavy slides
- Full-page PNG renders at 200 DPI typically produce files of 50-120KB each

##### Cleanup and Cache

- **Delete `_images/`** at the start of a new pipeline run to avoid stale images from previous lectures mixing with new ones. Re-extract on each run.
- Do NOT cache individual slide images — extraction is fast (~1 second per page) and re-extracting guarantees fresh renders.

### Step 5: Merge Transcript with Slides

Read the full transcript and slide texts. For each section of the transcript:
1. Identify the topic being discussed
2. Find matching slide pages (by topic/keyword overlap)
3. Insert slide content as `> 📄 课件` block quotes

Use this format:
```
### [00:15:30] 公共品的Samuelson条件
> 老师讲解的逐字稿内容...

> **📄 课件（第X页）**
> 课件中的对应文字或公式描述
```

**时间戳直接取 `逐字稿.json` 里 segments 的真实 `start`/`end`（秒级）**，不要再按片长估算
（老版本「±10 分钟、内容插值到 ±5 分钟」的口径已作废）。小节边界可选在说话人切换处，
这比按固定时长切干净得多。

**笔记正文不留说话人标签**（2026-09-28 用户决定）：课堂笔记的主体是老师讲授，逐句标
「**说话人1**：」只会淹没内容。规则：

- 老师与学生的对话若对内容有意义（提问引出概念、课堂讨论、点名回答），**写成角色而非编号**：
  > **学生提问**：…… / > **老师**：……，或者在叙述里写“有同学问……老师答……”。
- 其余一律合并为连贯叙述，**丢掉编号**。
- **编号只活在《逐字稿》与《说话人对照表》里**——需要回溯“这句到底谁说的”，去查那两份。

### Step 6: Extract Mathematical Derivations

Scan the merged transcript for derivation signals:
- Keywords: “假设”, “设”, “则”, “可得”, “FOC”, “一阶条件”, “最优化”, “均衡”, “边际”
- Slides with formulas (LaTeX or equation descriptions)

For each identified model, produce the derivation yourself in LaTeX. If you happen to have a dedicated derivation or LaTeX-typesetting skill installed, delegate to it — but **this skill has no hard dependency on any other skill**.

For each model, produce:
- **Environment**: Agents, goods, time horizon
- **Assumptions**: Explicitly listed
- **Derivation**: Step-by-step with LaTeX equations
- **Conclusion**: Boxed key result $\boxed{...}$

### Variable Annotation Rule (MANDATORY)

**Every formula or equation in the final note MUST include a variable annotation block** — either inline or as a compact table — that defines each symbol immediately after it first appears. This applies to formulas in BOTH the “逐字稿与课件” section AND the “数学模型推导” section.

**Rule**: No formula may appear without its variables being explained. If the professor did not explicitly define a variable, the AI must infer its meaning from context and note it.

**Format — Inline annotation** (for short formulas with few variables, use in the transcript section):

```markdown
> **📄 课件（第X页）**
> Lucas 生产函数：
> $$Y_t = A_t \cdot K_t^\alpha \cdot H_t^{1-\alpha} \cdot \bar{h}_t^\gamma$$
> 
> 其中：
> - $Y_t$：$t$ 期国家总产出
> - $A_t$：技术水平（**外生给定**）
> - $K_t$：实物资本存量
> - $H_t$：个人人力资本水平（教育产生）
> - $\bar{h}_t$：**社会平均人力资本**——外部性的来源
> - $\alpha \in (0,1)$：资本产出弹性
> - $\gamma > 0$：人力资本外部性参数
```

**Format — Compact table** (for derivation sections with many variables):

```markdown
**变量说明**：

| 符号 | 含义 | 性质/单位 |
|------|------|-----------|
| $u_i(x_i, G)$ | 消费者 $i$ 的效用函数 | 严格递增、凹 |
| $x_i$ | 私人品消费量 | 实数 |
| $G$ | 公共品数量 | 实数（所有人消费相同） |
| $\omega_i$ | 消费者 $i$ 的初始禀赋 | 私人品单位 |
| $c$ | 公共品边际成本 | 以私人品计价（$MRT$） |
| $MRS_i$ | 消费者 $i$ 的边际替代率 | $\frac{\partial u_i/\partial G}{\partial u_i/\partial x_i}$ |
```

**Key principles**:
1. **First occurrence**: Annotate variables at their first appearance in each section. Don't re-annotate in subsequent uses unless adding new variables.
2. **AI inference**: If a symbol appears without definition in transcript or slides, infer its meaning from standard economics notation and add `（推断）` tag. Example: `$\lambda$：拉格朗日乘子（推断）`.
3. **Units matter**: When applicable, note the unit (e.g., “以私人品计价”, “百分比”, “元”).
4. **Exogeneity/endogeneity**: Clearly mark which variables are exogenous vs. endogenous in the model.

### Step 7: Generate Output

Write a single `.md` file following the output template (see `references/output-template.md`).

Naming convention: `YYYY-MM-DD-第X讲-主题.md`

**Transcript link** — include a wikilink to the transcript at the top of the note, right after the `#` title:

```markdown
# 课程名 第X讲：主题

> 📝 原始逐字稿：[[_transcripts/YYYY-MM-DD-第X节-逐字稿|查看完整转写稿]]
```

---

## Note Writing Quality Standards

These standards apply to ALL lecture notes generated by this skill. The goal is to produce notes that a student can **understand independently without re-watching the lecture**.

### Principle 1: Follow the Professor's Narrative Arc, Not Just Slide Order

Slides are reference points, not an outline. The professor often spends 20 minutes on one slide and skips three others. **Read the full transcript first**, understand the teaching flow, then structure the note around the professor's **logical progression**, inserting slide references where they naturally belong.

**Good structure reveals the chain of reasoning**:
```
Problem → Why obvious approach fails → How scholars circumvented it → What caveats remain
```

**Bad structure** is a slide-by-slide dump with no narrative thread.

Always include a brief "logic chain" at the top of complex lectures (like a 3-line ASCII flow diagram) so readers know where the lecture is headed before diving into details.

### Principle 2: Explain Every Formula Step-by-Step — Never Jump to the Result

For every formula or derivation, include:
1. **Setup**: What problem are we solving? What do the symbols mean?
2. **Intuition before algebra**: What is the economic logic behind this math?
3. **Step-by-step derivation**: Show intermediate steps with explanations. Don't just write “整理得” and skip three lines.
4. **The punchline**: What does the final equation tell us? Why is it important?

**Anti-pattern** (too terse):
```markdown
$$\max_P \pi = P \cdot Q(P) - c(Q(P))$$
FOC: 整理得 $P = \frac{\varepsilon}{\varepsilon+1} \cdot mc$
```

**Correct pattern** (explains every step):
```markdown
企业面临向下倾斜的需求曲线 $Q(P)$，选择价格 $P$ 最大化利润：

$$\max_P \quad \pi = P \cdot Q(P) - c(Q(P))$$

对 $P$ 求导（使用链式法则，注意 $c$ 通过 $Q$ 依赖 $P$）：

$$\frac{d\pi}{dP} = Q + P \cdot Q' - c'(Q) \cdot Q' = 0$$

把所有含 $Q'$ 的项放一边...

这个式子的直觉：等号左边是"涨价一单位多赚的钱（$Q$）加上所有已售商品因涨价而减少的收入"，等号右边是"因销量减少而省下的成本"。
```

### Principle 3: Don't Assume Prior Knowledge — Explain Basic Concepts

Students reading the note may be encountering these concepts for the first time, or have forgotten them. **When a concept first appears, define it in plain language**, even if it seems basic:

- “拉格朗日乘子度量的是'约束条件放宽一个单位时，目标函数的增加量'”
- “需求弹性 $\varepsilon = d\ln Q/d\ln P$ 说的是'价格变化1%时需求变化百分之几'”
- “调和平均给小的数值更大权重——这就是为什么分散度大会拉低宏观加成”

Never write something like “由一阶条件可得” without explaining what the first-order condition is and why we set derivatives to zero.

### Principle 4: Capture the Professor's Verbal Explanations and Emphasis

The transcript contains rich verbal content that slides don't capture. **Incorporate the professor's**:

- **Analogies and metaphors**: “来了个橙子” (乘子), “最严厉的批评”
- **Emphasis markers**: “这个非常重要”, “这是核心”, “大家一定要理解”
- **Asides and commentary**: “这个工作发表在最好的期刊上”, “数据闲鱼上一块钱一G”
- **Direct quotes**: Use blockquotes for particularly insightful or memorable statements

These make the note feel alive and capture what made the lecture engaging.

### Principle 5: Show "Why Not" Before "How To"

For method-focused lectures (like DLW), the most valuable insight is often **why the naive approach fails**. Before presenting the solution, explain:

1. The most obvious method (e.g., "just estimate demand elasticity")
2. Why it doesn't work in practice (e.g., "you only see one equilibrium point, not the curve")
3. What constraint forced scholars to find another way

This makes the actual method feel inevitable rather than arbitrary.

### Principle 6: Elaborate on Challenges and Limitations

The professor often spends equal time on **problems with the method** as on the method itself. These are NOT footnotes — they are core content. For each challenge:

- What is the problem?
- Who discovered it? (Cite specific authors and years)
- What evidence supports it? (Mention specific data findings)
- Why does it matter? (What would go wrong if we ignored it?)

### Principle 7: Use Visual Hierarchy to Guide Reading

Structure sections with clear heading levels:
- ## for major lecture parts (e.g., “第一节：医疗保险理论基础”)
- ### for topic blocks within parts (e.g., “事后道德风险的两种情形”)
- `####` for sub-points within topics

Use **bold** for key terms at first mention. Use `>` blockquotes for slide content and the professor's notable quotes. Use tables for comparisons and fact sheets.

### Principle 8: Length Guidelines

A lecture note should be **substantial** — a 3-hour lecture typically produces:
- **15,000–25,000 characters** of polished Chinese text
- **10–15 major sections**
- **5–10 embedded slide screenshots**
- **2–4 mathematical derivation boxes**

If a note is under 10,000 characters for a 3-hour lecture, it is almost certainly too terse and skipping key explanations. Err on the side of **over-explaining** rather than under-explaining.

---

## Humanities Course Adaptation（人文/社科类课程）

当课程是宗教学、哲学、历史、政治思想、文学理论等**没有数学模型、但有密集文本与概念**的课时，套用「数学课规则」和「经济学推导」都不对。适配要点：

1. **`## 🧮 数学模型推导` 整节替换为 `## 🔍 关键概念辨析`** —— 用一张表把本讲出现的关键术语、专名、外来语（含原文转写，如 *dār al-ḥarb*、kalām、sharīʿa）逐个定义，并标注它出现在哪一段讨论中。
2. **### [时间] 分段按“论证推进”而不是“课件页序”** —— 人文课的关键是**论辩链条**：谁提出什么、对手如何反驳、反驳分几个方向。逻辑线索（ASCII 流程图）在这一类课上比在理科课上更重要，务必画。
3. **长引文是核心内容，不是装饰** —— 讲课人引用的经典段落（课件上的长 blockquote）要**完整保留**并附出处；不要压缩成一句话。这类课的价值恰恰在文本细读。
4. **区分“讲课人的转述”与“讲课人的判断”** —— 人文课大量内容是“某学者认为……”，笔记必须写清**谁在说**，不能把转述写成事实。对讲课人自己的评价性意见（“这是一句废话文学”“这个立场极具干预性”），用 blockquote 保留原话并标明是讲课人的判断。**现在逐字稿自带说话人编号，遇到分不清「是老师在评价还是学生在发言」的段落，回 逐字稿.md 核对该句的说话人，再决定归属**——不要靠语气猜。
5. **保留反例与“挑错”细节** —— 人文课常花大量时间拆解对手的三个具体错误。这些细节（“基督教不需要被带回圣城，它一直在圣城”）是评分点，不能省略为“存在若干事实性错误”。
6. **人物、著作、年份务必核对** —— ASR 对西方人名、书名、术语的转写极不可靠。凡不确定的（例如讲课老师本人姓名、系里其他老师姓名），**宁可不写全名或加“（转写不确定）”，也不要编造**。
7. **课程信息（考核、文献、时间节点）单独成节** —— 人文课的导论讲常有大段课程事务，属于学生真正需要反复回查的信息，集中放在开头一节，不要散落在逐字稿里。

## Math Course Special Rules

These rules apply **only** when processing lectures for math courses: 线性代数 (Linear Algebra), 高等数学 (Advanced Mathematics), 数学分析 (Mathematical Analysis), 概率论 (Probability Theory), 抽象代数 (Abstract Algebra), and similar proof-based mathematics courses. For economics, social science, or other courses, use the general Note Writing Quality Standards above.

### M0: Recognize a Math Course

A lecture is a math course if:
- The course name contains: 线性代数, 高等数学, 数学分析, 概率论, 数理统计, 抽象代数, 拓扑, 微分方程, 数值分析, 离散数学
- The transcript is dominated by proofs, definitions, theorems, and symbolic reasoning (not empirical discussions, policy debates, or case studies)
- There are no PDF slides in the workspace (math lectures typically use blackboard writing)

### M1: No Slides — Transcript Is the Only Source

Math lectures rarely have slide PDFs. The professor writes on a blackboard. **The transcript is your primary (and often only) content source.** Steps 4-4b (slide extraction, screenshot embedding) may be skipped entirely.

**What to do instead**: Read the full transcript end-to-end. Identify the logical structure: which definitions were given, which theorems were stated, which proofs were demonstrated, which examples were worked through. Reconstruct the mathematical content from this structure + your knowledge of the subject.

If student notes PDFs exist in the workspace (e.g., `学生笔记.pdf`), read them as supplementary reference to verify formulas and capture blackboard diagrams you can't infer from audio alone.

### M2: ASR Garbles Math — Reconstruct from Context

Cloud ASR (`XingChenASR-Diarize-V3.0`) is trained on general Chinese speech and performs **poorly on mathematical terminology**. Expect severe garbling of:
- Greek letters and mathematical symbols
- Formula recitations (“阿尔法一到阿尔法m” may be transcribed as random characters)
- Technical terms spoken at speed

**Counter-strategy**: Do NOT trust the transcript's rendering of formulas. Instead:
1. Identify the **mathematical topic** from context clues (recognizable concept names: “子空间”, “直和”, “维数”, “基”, “线性无关”)
2. Reconstruct the **correct mathematical statement** from your knowledge of the subject
3. Cross-reference with any student notes PDFs in the workspace
4. If a formula is garbled beyond recognition, state what the professor was **likely** proving, using standard notation

### M3: Content Prioritization — Three-Tier System

Math lecture content falls naturally into three tiers. **Include tiers 2 and 3; exclude tier 1.**

| Tier | Content Type | Examples | Action |
|:---:|------|------|:---:|
| ⭐⭐⭐ | **Structural content** | Definitions, theorem statements, complete proofs, counterexamples, key formula derivations | **Always include in full detail** |
| ⭐⭐ | **Illustrative content** | Worked examples, geometric interpretations, alternative formulations, special cases of theorems | **Include with moderate detail** |
| ⭐ | **Atmospheric content** | Teacher-student banter, meta-commentary about studying (“你们学课上题就够”), off-topic asides, self-deprecating remarks, textbook recommendations | **Exclude entirely** |

**Tier 3 (⭐⭐⭐) — Structural**: This is the skeleton of the lecture. Without it, the note is worthless. Every definition must be stated precisely. Every theorem must include its full statement and complete proof (with all steps). Every counterexample must explain *why* it's a counterexample. The final note's “🧮 关键定理与证明” section is a condensed reference, but the full proofs must live in the lecture body.

**Tier 2 (⭐⭐) — Illustrative**: These make the abstract concrete. A lecture on “子空间” without examples like polynomial spaces, matrix spaces, and ℓ² is hollow. Geometric examples (ℝ², ℝ³) are especially important because they provide visual intuition. Include these with enough detail that a reader can follow the example from start to finish.

**Tier 1 (⭐) — Atmospheric**: These make the lecture *feel* like a lecture but don't contribute to mathematical understanding. “老师顺带提到泛函分析课程” is atmospheric. “一位同学给出了非常到位的分析” is atmospheric. Strip them out — a math note should be lean and focused on the mathematics.

**Grey area**: If the professor uses a particularly evocative analogy to explain a mathematical concept (e.g., “基的扩张就像是把向量一个接一个地拉进来”), keep it — it's a teaching device for a ⭐⭐⭐ concept, not atmospheric chatter.

### M4: Proof Structure — Every Step with Its "Why"

Math proofs are the core content of tier 3. Present each proof with **explicit reasoning at every step**:

```markdown
**情况一**：$m = n$（$\dim U = \dim V$）。

取 $U$ 的基 $\{\delta_1, \ldots, \delta_n\}$。需要证明它也是 $V$ 的基。

对任意 $\alpha \in V$，考虑 $\{\alpha, \delta_1, \ldots, \delta_n\}$——这是 $n$ 维空间中 $n+1$ 个向量 → **必然线性相关**。
因此存在不全为零的系数 $b_0, b_1, \ldots, b_n$ 使得：

$$b_0 \alpha + b_1 \delta_1 + \cdots + b_n \delta_n = 0$$

若 $b_0 = 0$，则 $\{\delta_i\}$ 线性相关——**矛盾**（它们是基）。所以 $b_0 \neq 0$。于是：

$$\alpha = -\frac{1}{b_0}(b_1\delta_1 + \cdots + b_n\delta_n)$$

$\alpha$ 可由 $\{\delta_i\}$ 线性表出。因为 $\alpha$ 任意 → $\{\delta_i\}$ 张成 $V$ → 是 $V$ 的基。
```

Key features of this pattern:
- **States the case/setup** before diving in
- **Uses bold for logical pivots** (→, **矛盾**, **必然**)
- **Explains *why* each deduction follows** (“若 $b_0 = 0$，则...”). Never write “显然可得” without justification
- **Labels the proof technique** when notable (反证法, 数学归纳法, 递归构造)
- **Concludes each case** before moving to the next

### M5: Note Structure for Math Lectures

Math notes follow a different structure than economics notes:

```
# 课程名：主题1——主题2——主题3

## 📝 逐字稿与课件

### 本节课的逻辑线索
(3-5 line ASCII flow showing the lecture's logical arc)

### [timestamp] Section 1: ...
### [timestamp] Section 2: ...
...

---
## 🧮 关键定理与证明
(Condensed reference: theorem statements + proof sketches)

---
## 📋 本节核心要点
(Definition table, key formulas, key conclusions)
```

**No “📹 课程录播” metadata block is needed** beyond a brief source line. **No slide screenshots** (unless a student notes PDF provides useful diagrams). **No “📄 课件” blockquotes** (there are no slides).

### M6: Example-Rich, Not Example-Bloated

Math lectures thrive on examples. Include all **substantive** examples (tier 2) that illustrate a concept from a different angle. But:

- **Consolidate similar examples** into a unified framework when the professor explicitly says they're “本质上是一回事” (e.g., polynomial奇偶 = matrix对称/反对称 = function奇偶 → present as one “奇偶分解的统一原理”)
- **Don't narrate the professor's process** of discovering an example (“老师想了几秒钟，画了个图，发现...” ). Just present the example
- **Geometric examples in ℝ² and ℝ³** deserve special attention — they're the student's bridge from abstract to concrete

### M7: Cross-Reference Awareness

Math courses are cumulative. When the professor references a concept from a previous lecture (“我们上节课讲过...”), include a brief inline reminder of that concept. Students reading the note in isolation should not need to flip back to earlier notes to understand a reference.

### M8: Homework and Exercise References

Search the transcript for exercise/problem references (思考题, 习题, 作业, 第X题). If the professor explicitly assigns or recommends specific problems, note them at the end of the note. If none are mentioned, do NOT fabricate or guess at homework assignments.

---

## Output Template

See [`references/output-template.md`](references/output-template.md) for the full template.

## Caching Strategy

| Intermediate File | Location | Produced By | Skip If |
|-------------------|----------|-------------|---------|
| `asr/req_%02d.json` | `_transcripts/_diar/<录音名>/` | Step 3（主脚本） | **Exists** → 秒级重跑，且**不要删** |
| `_audio/seg_*.mp3` | 同上 | Step 3（ffmpeg） | 跑完自动删（`--keep-audio` 才留） |
| `逐字稿.json` / `逐字稿.md` / `说话人对照表.md` | 同上 | Step 3（主脚本） | 每次都重算（秒级） |
| `.slides_cache_*.md` | `_cache/` | Step 4 (python) | Exists |

**Cleanup**: 只删 `_audio/`（脚本自动）。`asr/req_*.json`、`逐字稿.json`、`_log.txt`、`说话人对照表.md`
**一律保留**——它们是用户复核的依据。

## Error Handling

- **ffmpeg not found**: Print install command for the user's OS（主脚本会自己找 PATH 与 winget 目录）
- **ASR 返回 500**: **偶发，原样重试即可**（实测 5194s 的片首次 500、重试即 200）。脚本已做 5 次退避
- **ASR 返回 200 但 `segments` 为空**: 该片无有效语音。主脚本视为失败重试；若确为纯静音，
  交叉验证用 `ffmpeg -i x.wav -af volumedetect -f null -`（`mean_volume < −35 dB`），
  在成稿里**显式标注“本段无有效语音”**，不要静默丢弃
- **时长 > 9000s 直接 500**: 切片不足或 `--overlap` 设得过大导致单片仍超限；用 `--dry-run` 核对计划
- **跨片编号对不上**: 脚本标「✳️未锚定」并写明首次出现在哪一片；人工听辨后用 `--alias N=M` 合并，**不要盲合**
- **`verify_diarized.py` 报 B 时间轴不单调**: 先确认跑的是最新脚本（内含单调化收口）
- **whisper not found**: `pip install openai-whisper`（仅兜底路径需要）
- **No MP4 in folder**: Prompt user to download recording first
- **PDF text extraction garbled**: Fall back to OCR via `pdf` skill if `pdfplumber` produces no readable text
- **No slides found**: Proceed with transcript-only mode; note this in output

## 调研/会议录音·逐字稿模式（只出稿、不出笔记）

用户丢几段会议／调研 m4a 并要求“逐个转写、产出逐字稿”时走这条支线（**不生成学习笔记**，跳过课件、公式、笔记模板全部步骤）。实测于 2026-09-17 两地调研（4 段、5.2 小时）。

**1. 先摸清“文件 vs 会场”，不要按文件名切分**

一场会可能由多台设备同时录，也可能一台设备跨两个会场。**必须先逐片比对内容再动手**，否则会把同一段话整理两遍。

- 检出方法：把各段 merged.txt 按切片编号对齐读，找内容重复的区间（同一个人说同一件事）。重合区间长度通常严格相等 → 可用“重合段长度 = 时间偏移”反推设备间的起录时差。
- 处理原则：**重合段只在录音质量更好的那一稿里收录**，另一稿写一张对照表（本稿切片 ↔ 他稿切片 ↔ 内容）＋指针，不重复正文。
- 同一场内**并行分会场**的判断依据：某稿里出现“要不你们工作组另找个会议室”这类安排、且 A 稿出现 B 稿从未出现的人名／称呼 → 两稿是两个房间。

**2. 说话人编号由模型给出，但身份仍要人工判定**（2026-09-28 起口径更新）

主模型自带说话人分离，返回逐句 `speaker` 编号 + 秒级时间戳。这解决了「谁在第几分钟说什么」，
但**不解决「编号是谁」**：

- **编号只保证「同一请求内同一编号＝同一人」**。跨文件、跨片**不保证**同一编号是同一个人。
- 能可靠区分的是**人数与轮次结构**；身份要看称呼与应答（谁在被点名、谁在答复、谁在汇报）。
  **落不到具体个人时标 `（说话人不确定）`，绝不硬编。**
- 每份稿首**必须**给《说话人对照表》（主脚本已自动生成）：编号 ＋ 说话时长 ＋ 段数 ＋ 首末出现
  ＋ 最多 6 条样例句 ＋ 「模型内编号 ↔ 全局编号」映射。这是用户自行校验的唯一抓手。
- 用户若给了人名清单，就按清单 ＋ 录音中的称呼交叉确认。
- 用户若要求“用 XX 区工作人员 1/2/3 依次排列”：编号在本项目内**跨文件保持一致**，
  并在对照表里写明依据；能判断科室岗位于括号内标注（如“区财政局公用事业科”）。

> ⚠️ **不要再用“谁在说只能靠内容反推”的老口径做免责声明**——现在有编号了，
> 要写的是「编号已给，身份待落名」，并给出判断依据。

**3. 整理原则：逐字稿 ≠ 转写稿**

- **保留第一人称原话、口语、语气词**，不改写成叙述体，不改原意。
- 只订正**高置信度同音错字**（本项目实测高频：签→钱、养活→养护、打毛病→打包、方讯→汛、美国白俄→美国白蛾）。**人名、地名、金额、文号一律不猜**，保留原文并标“（待核对）”。
- 每份稿末尾必须有《疑似转写偏差对照》表：转写原文 | 订正 | 依据，未订正的收进“待核对”栏。
- 智能标点与断句：口语长句按语义断开，**不要合并成一段**，否则轮次无法辨认。
- 段落标题用 ### [HH:MM:SS–HH:MM:SS] 主题（**秒级时间戳，直接取 segments 的真实 start/end**，不要按片长估算），主题用“内容摘要”而非“第 N 段”。

**4. 多稿合并成一份「会议逐字稿」（用户要求“合并成完整会议稿”时）**

合并时间轴**不要逐句对齐**——用重合段反推设备间起录时差，整体平移即可（重合段起点差 = 偏移量，且重合段长度必然相等）。

- **基准**：取覆盖最完整／质量最好的那一稿做基准（原时间 = 合并时间），其余稿按偏移整体加减。
- **每节标注双重坐标**：`### [合并 21:00–24:00 ｜ 源：录音A 33:00–36:00] 主题`。缺任何一半都不好用——只有合并时间无法回溯，只有原始时间无法通读。
- **并行会场必须显式声明**：两个房间同时录音时，把两条并行线拉成一条直线，时间戳会在物理上重叠。正文里必须点明“读第三部分时，另一会场正在谈 X”，否则读者会以为时间线算错了。
- **剔除段落留分隔标记**：`> ⏭️ 此处略去：XX 议题，至 HH:MM 结束（约 N 分钟），已整体剔除。原文见……`。**绝不静默删除**，否则时间轴出现无法解释的空洞。
- **改标注要在正文里声明**：把不确定的具名标注降级为 （说话人不确定） 时，在开头“说话人对照”里写清降级理由，并注明“原单稿未同步改动，回溯以合并版为准”。
- **⚠️ 验收标准（必做）**：合并完写脚本，把**每一节正文拿去源稿做子串命中校验**（注意源稿若被脚本批量改过标注，比较时要用同样规则处理源文本）。未命中的节必须能精确定位到“删了哪一句／改了哪个标注”，并在成稿中显式列出。**别靠肉眼**。

**5. 有名单的谈话录音：分段边界由内容锚定，绝不能按固定时长硬切**

场景：一批录音 ＋ 一份名单（谈话时间表／排班表／签到表），N 段内容要落到 N 个人名上。实测于一次 16 人、4 场的多对象依次访谈（2 段录音）。

> 2026-09-28 更新：主模型现在**自带说话人编号 + 秒级时间戳**，所以「边界找不到」这个根因已经大大缓解
> ——**边界优先取「说话人切换点」**，再用内容锚点确认。但**「第 k 段 = 名单第 k 人」这种顺序硬套依然禁止**。

**❌ 死法**：把固定时长切片的序号直接当时间轴，再把“第 k 个时间段”当成“名单第 k 个人”。这样切出来的节标题会**系统性整体错位**——上一节的收尾语和下一节的开场语被切在同一节里，正文里出现的姓名与标题对不上；严重时同一段内容被写两遍、另一段整段丢失而无人察觉（2026-09-21 实际发生：第一段被第二段内容覆盖，真正的第一段全程缺失）。

**✅ 做法：先用内容锚点找出真实边界，再逐段读**

- **边界首先取「说话人结构变化」**：新任谈话人进场时，编号会切换或出现新的连续编号段。
  在 `逐字稿.md` 里按 `[时间] **说话人N**` 扫一遍，切点通常一眼可见。
- **新对象入场锚**（用来确认边界）：`你叫什么名字？` + `我叫 XX`、`坐/请坐`、`介绍一下你的成长经历`。这是最硬的锚——**书记点名/自报姓名的位置就是段边界**。
- **上一段收尾锚**：`谢谢`、`拍一下照片`、`（材料）拍一下照`、`你们还有什么想问我的吗`。
- **主持人交接锚**：`下一块你来吧`、`辛苦了`、`学长好走`（第二位谈话人进场）。
- **名单自带互相校验**：录音里被点名的人名应与名单某一位吻合（实测：录音中被点名的姓名 ↔ 时间表第 10 位同一姓名）。这是除顺序之外唯一的独立证据，务必用上——它同时能证明「段数与人数相等」。
- **段数 ≠ 人数时先怀疑分段，不要怀疑名单**。先回头检查是不是漏切或重切。

**成稿后必做校验**：

1. **首选**：`references/verify_diarized.py`（五项：请求完整性/时间轴/说话人/去重账/成稿一致性）——
   只要转写走的是 `transcribe_diarize.py`，跑这个就够。
2. **补充**（把成稿切成了「按人分节」时）：`references/verify_segments.py` 的三项（正向命中/重复/遗漏），
   用来证明「没有一段内容被两节引用、没有原始分片被漏掉」。**只在成稿按节重排过时才需要**。

> ⚠️ 别把「逐字稿的说话人还原」和「记录的姓名登记」混为一谈。用户给了名单，要的是**按名单顺序落名的会议记录式材料**，不是考据式转写。默认按名单顺序落名，把录音自称差异放进附录对照表，**不要用它污染正文标题**，更不要写出“未自报姓名者”“时间表未登记”“存疑”这类规避型标注——用户要的是能用的一稿，不是一份免责声明。

**6. 目录与留存**

```
调研录音与笔记/逐字稿/
├── 00-索引与说明.md            # 阅读顺序、重合段比对表、转写方式、已知风险、待办
├── {日期}-{区}.逐字稿.md       # 一份稿件一个会场
├── {日期}-{区}.说话人对照表.md  # 编号 ↔ 判断依据（主脚本自动生成）
└── _原始ASR/                   # **必须留**，否则用户无从复核你改了什么
    ├── req_*.json              # 模型原始返回（含 segments 与说话人编号）
    └── *.原始ASR.txt           # 未整理的机转纯文本（含全部误识）
```

`00-索引与说明.md` 里必须写清：**音频已上传第三方云端**（api.siliconflow.cn）；时间轴精度（秒级）；
说话人编号的可靠性边界（**模型侧同请求内一致、跨文件不保证**；谁是身份的判定依据）；
哪些段是无效音频；哪些人名/数字“待核对”。

**7. 收尾清理**：主脚本默认已删中间 mp3（`--keep-audio` 才留）；保留 `逐字稿.json`、`asr/req_*.json`
与 `_log.txt`，并把模型原文拷进 `_原始ASR/`。

---

## Notes

- **会议/讨论录音（m4a 等）→ 排版规范的《录音转写稿》** 是另一条线：同样走 SiliconFlow ASR，但产出是对齐既有参照稿排版的 docx 转写稿，而不是学习笔记。若你有专门的转写稿技能可交给它，本技能不含这一环。
- **脚本目录里不要放 `inspect.py`**：会遮蔽 stdlib `inspect`，lxml 初始化即崩，`import docx` / `import pymupdf` 全线报错且报错指向 lxml 源码。取名为 `insp_xx.py`。
- **本 skill 的 ASR 主模型 = `XingChenAGI/XingChenASR-Diarize-V3.0`**（免费、自带说话人分离、秒级时间戳）。
  旧模型 `TeleAI/TeleSpeechASR` **已下架**（仍可调用但随时失效），不要再写进任何新脚本。
  完整实测数据、硬约束与已知缺陷见 [`references/speaker-diarization.md`](references/speaker-diarization.md)。
- **不是所有场景都需要说话人标签**：「只要文字、不要谁在说」时，`逐字稿.md` 里去掉 `**说话人N**` 即可，
  不必换模型（同表其它模型不一定更好，换模型反而丢掉时间戳先验）。
- The skill is **course-agnostic** — works for any lecture with MP4 + slides
- Whisper model downloads once (~1.5GB for `medium`), cached globally（**仅兜底路径需要**）
- For non-Chinese lectures, change the `language` parameter in `scripts/transcribe_diarize.py`
- Mathematical derivation extraction is AI-driven and may miss implicit derivations — always review

## 读转写稿的三个 Windows 坑（2026-10-01 实测，读长逐字稿必踩）

《逐字稿.md》是**一「轮次」一行**，一轮次就是同一个说话人连续说完的整段——**一轮常有两三千字**。直接 Read 会被**每行 2000 字符**的上限截断，且截断处**不带任何提示**（看似完整、实则丢尾巴）。处理前先把它**硬切片**成短行再读：

1. **不要用 `textwrap` 切中文**。中文没有空格，`textwrap.wrap()` 会把整段当成**一个超长单词**而完全不切（`break_long_words=False` 时原样返回）。**必须按固定宽度手动切片**：`txt[i:i+280]`。
2. **Git Bash 会吃掉 `python -c` / heredoc 里的反斜杠**：`\d` 变成 `/d`、`\s` 变成 `/s`，
   正则**静默失配**（`re.match` 返回 None，不报错）。**凡是带正则或反斜杠的 python 代码，
   一律先 Write 成 .py 文件再执行**，不要用 python -c "..." 或 heredoc。同理，Windows 下
   Python 3.9 的 `open()` **默认编码是 GBK**，读 skill 自己产出的 UTF-8 文件必须显式
   `open(path, encoding='utf-8')`，否则中文全部匹配不上。
3. **切片脚本与用途**：轮次标头格式为 `[HH:MM:SS] **说话人N**\u3000正文`，用
   `^\[([0-9]{2}:[0-9]{2}:[0-9]{2})\]\s+\*\*(说话人[0-9]+)\*\*\s*(.*)$` 匹配，输出到
   系统临时目录（**不要写进仓库**），读完即删。

**成稿时还要核对**：笔记写完后拿 `grep -o '_images/[a-zA-Z0-9_]*\.png' 笔记.md` 与 `ls _images`
做 comm -23 差集，差集非空即有断图；**Obsidian 嵌入必须写“仓库根相对全路径”**
（本机仓库根 `<vault-root>`，故形如 `![[课程目录/课程名/_images/s2_p07_xxx.png]]`）。
再加一条：**每行 * 个数必须是偶数**（awk 查 gsub(/\*/,"*") % 2），星号不平衡会原样渲染成字面星号。

### 别重写切片脚本——直接用 `scripts/make_readable.py`

上面第 1–3 条已固化成脚本，**不要再临时手写**：

```bash
PYTHON="python3"
"$PYTHON" "$SKILLS/scripts/make_readable.py" "<_diar/录音名 目录或逐字稿.md>" "<输出路径>"
```

- 把每个轮次改成 `### [HH:MM:SS] 说话人N` 标题 + 正文按 **78 字硬切**（遇中文标点优先断行），
  彻底避开 Read 的 2000 字符截断；显式 `encoding='utf-8'`，不会被 GBK 坑。
- 输出**默认写到系统临时目录**（`$SKILLS` 外），读完即删，不污染仓库。
- 本次两讲（0922 / 0929，各约 2h、3 万字）实测：输出 692 / 320 行，全程读得干净。

## 转写后处理规则（2026-10-01 第 3、4 讲实测补充；2026-10-04 增第 4 条）

1. **某片只识别出 1 个说话人时，不要落名**。0929 全片模型只给出「说话人1（100%）」，
   但课上明显有学生提问——**编号数 ≠ 真实人数，也可能低于真实人数**。此时笔记中
   **学生发言一律“仅记要点、不逐字引用、不落名”**，并在顶部说明里写清。
2. **缺课件的处理**：若某讲没拿到课件 PDF（本次 0929 即如此），
   **绝不用别的课的截图凑数**；在笔记开头单列“关于本讲课件的说明”，内容全据录音，
   在应当有图处留补入提示；课后阅读文献可用**其首页截图**替代（标为“文献截图”而非“课件截图”）。
3. **多片可并行**：同一批录音（各自 < 8500s）可以**同时起多个后台转写任务**，
   上传是主要瓶颈，并行比串行快近一倍。转写期间照常做课件抽取、渲染、图片识别等不需要逐字稿的活。
4. **本轮新踩的坑（某次调研调研系列，0924／0929 五场）**：
   - **超长整片请求偶发长时间无返回（2026-10-04 现象，原因未定论）**：4908s／7765s／8399s 三个整片请求
     均 40+ 分钟零响应（`_log.txt` 停在 `[UPLOAD] 片N 上传中`、`asr/` 为空），而 3785s 及以下正常。
     ⚠️ **不要据此下调 `--safe-limit`**——用户此前已实证 **8500s 整片可行**，本次异常更可能与服务端瞬时
     状态／网络／同时起多个任务有关，未能定位。遇到挂起：先按上述信号确认，果断停掉重跑即可。
   - **碎片段要合并再读**：ASR 输出 800+ 段（如场次C 841 段、场次D 813 段），逐段读既读不完也读不懂。
     先按「同一说话人 + 相邻间隔 ≤4 秒」合并成轮次（约 300—400 轮），再通读；否则 `逐字稿.md`
     一行一段、信息密度极低，而且**轮次边界本身也是推断的**。
   - **一个编号常常 ≠ 一个人**：0924 场次E编号 3 至少含 3 位发言人，0924 场次D编号 3 至少含 2 位
     （园林口汇报人 + 市方提问人）。**必须按内容拆分，并在「说话人对照」里写明拆分依据**，
     不能整篇按编号归属。
   - **片 2 之后普遍「未锚定」**：0929 场次C片 3 的 8 个编号全部未锚定；0929 场次F片 2 全部未锚定。
     同一人可能同时存在于两个编号下 —— 按「发言内容 + 被称呼关系」人工归并，**不做 `--alias` 硬合并**，
     保留原编号痕迹以便回溯。
   - **纯 ASCII 引号是默认产物**：新写的稿件极易混入 ASCII "，交付前必须后处理成中文引号
     （成对翻转），并校验左右数量相等；同时校验 `**` 星号配对数。
