# Lecture Recorder

把课堂 / 会议 / 调研录音整理成结构化 Markdown 学习笔记 —— 自带**说话人分离逐字稿**、课件整合与逐步数学推导。

Turn lecture, meeting, and research-interview recordings (MP4 / m4a / mp3) into structured Markdown study notes, with speaker-diarized verbatim transcripts, slide integration, and step-by-step mathematical derivations.

---

## What it produces

- **逐字稿**：说话人分离的逐句稿，秒级时间戳（`[HH:MM:SS] 说话人N 正文`）
- **说话人对照表**：编号 / 时长 / 段数 / 首末出现 / 样例句 / 人工确认栏 —— 只给编号，不替你落名
- **学习笔记**：讲课人的论证线索、课件文字与整页截图嵌入到语义对应的位置
- **数学推导**：逐步 LaTeX 推导 + 每个符号的强制变量说明（教授没定义的要推断并标注）
- **要点速览**：可直接用于考前复盘的浓缩小节

三种课程口径已内置：经济学 / 社科类、人文类（无公式、重文本细读）、数学类（无课件、重证明结构）。

## Requirements

- **ffmpeg / ffprobe** 在 PATH 上
- **Python 3.9+**：`requests`、`pymupdf`、`pdfplumber`、`python-pptx`
  ```bash
  pip install -r scripts/requirements.txt
  ```
- **SiliconFlow API Key**（云端 ASR，说话人分离）：到 https://siliconflow.cn 申请，然后
  ```bash
  export SILICONFLOW_API_KEY="sk-..."
  ```
  本地 Whisper 是可选兜底路径（无说话人标签，CPU 上很慢），另需 `openai-whisper`。

## Usage

```bash
PYTHON="python3"
SKILL_DIR="/path/to/lecture-recorder"

# 只看切片计划，不上传
"$PYTHON" "$SKILL_DIR/scripts/transcribe_diarize.py" "input.m4a" --dry-run

# 转写（≤8500s 整片直传；超长自动切片并用重叠区对齐说话人编号）
"$PYTHON" "$SKILL_DIR/scripts/transcribe_diarize.py" "input.mp4" --out "<输出目录>"

# 成稿自检：请求完整性 / 时间轴 / 说话人 / 去重账 / 成稿一致性
"$PYTHON" "$SKILL_DIR/references/verify_diarized.py" "<产物目录>"
```

把录音放进 `原始视频/`、课件放进 `原始课件/`，然后按 `SKILL.md` 的 Pipeline 走完 7 步即可。

## Layout

```
原始视频/          录音原件（你自己放）
原始课件/          PDF / PPTX 课件（你自己放）
_cache/            课件文字缓存
_images/           课件整页截图
_transcripts/      逐字稿与说话人对照表
<日期>-第X讲-主题.md   最终笔记
```

## Notes

- **说话人编号 ≠ 身份**。模型只保证「同一请求内同一编号＝同一人」；谁是谁必须按内容判定，判不准就标 `（说话人不确定）`，绝不硬编。
- **别按文件名切分场次**。一场会可能多机同时录，也可能一机跨两个会场，先逐段比对内容再动手。
- 长逐字稿用 `scripts/make_readable.py` 转成不截断的可读稿再通读。

## Privacy

音频会上传第三方云端（`api.siliconflow.cn`）完成识别。处理敏感录音前请自行评估。本地 Whisper 路径可完全离线，但不提供说话人标签。

## License

MIT — 见 [LICENSE](LICENSE)。
