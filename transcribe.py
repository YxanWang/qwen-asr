# -*- coding: utf-8 -*-
"""Qwen3-ASR 通用转写：python transcribe.py 输入文件 [--speakers N] [--format txt|srt|both]"""
import shutil
import argparse
import gc
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

WORKDIR = Path("D:/Program/qwen-asr")
# 优先用系统 PATH 里的 ffmpeg，否则用项目 bin/ 下的自带副本
FFMPEG = shutil.which("ffmpeg") or str(WORKDIR / "bin" / "ffmpeg.exe")
MODEL_DIR = "D:/Program/qwen-asr/Qwen3-ASR-1.7B"
ALIGNER_DIR = "D:/Program/qwen-asr/Qwen3-ForcedAligner-0.6B"
CHUNK_SEC = 100  # 目标长度；官方会在附近寻找低音量切点，并非严格上限。
BATCH_SIZE = 3
# 时间轴模式：对齐模型对整块音频做一次全序列前向，块长×批量的显存增长极快，
# 100s×3 批曾在 8GB 显卡上触发 CUDA 非法访问并连带崩掉驱动，故强制小块单批。
ALIGN_CHUNK_SEC = 30
ALIGN_BATCH_SIZE = 1

# torch 的 CUDA/MKL 等外部 DLL 统一放在 runtime_dlls，导入 torch 前加入搜索路径
_DLL_DIR = WORKDIR / "runtime_dlls"
if _DLL_DIR.is_dir():
    _DLL_HANDLE = os.add_dll_directory(str(_DLL_DIR))
    os.environ["PATH"] = str(_DLL_DIR) + os.pathsep + os.environ.get("PATH", "")


def join_chunk_texts(texts):
    """按顺序衔接无重叠音频块；不去重、不补标点、不改识别内容。"""
    joined = ""
    for text in texts:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        # 英文词之间保留空格，中文跨块句子直接接续。
        if joined and joined[-1].isascii() and joined[-1].isalnum() and text[0].isascii() and text[0].isalnum():
            joined += " "
        joined += text
    return joined


def make_paragraphs(text, target=220, maximum=320):
    """保护短且闭合的引语；缺失引号或超长引语不阻止后文分段。"""
    if not 0 < target <= maximum:
        raise ValueError("分段长度必须满足 0 < target <= maximum")
    sentences = []
    start = 0
    quote_stack = []
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』"}
    # 先确认引号确实闭合。模型偶尔漏掉右引号，不能因此锁住整篇正文。
    matched = {}
    openings = []
    for pos, char in enumerate(text):
        if char in pairs:
            openings.append((pos, pairs[char]))
        elif char in pairs.values():
            for k in range(len(openings) - 1, -1, -1):
                begin, expected = openings[k]
                if char == expected:
                    matched[begin] = pos
                    del openings[k:]
                    break
    pending_end = False
    for i, char in enumerate(text):
        if char in pairs and i in matched and matched[i] - i + 1 <= maximum:
            quote_stack.append(pairs[char])
        elif quote_stack and char == quote_stack[-1]:
            quote_stack.pop()
        is_end = char in "。！？!?" or (char == "." and (i + 1 == len(text) or text[i + 1].isspace()))
        pending_end = is_end or (pending_end and char in "”’」』\"）)]")
        if pending_end and not quote_stack:
            # 连续标点和闭引号附在原句后。
            if i + 1 < len(text) and text[i + 1] in "。！？!?.”’」』\"）)]":
                continue
            sentences.append(text[start:i + 1])
            start = i + 1
            pending_end = False
    if start < len(text):
        sentences.append(text[start:])
    # 缺少句末标点时先在逗号、分号或空白处断段，最后才按长度兜底。
    bounded = []
    for sentence in sentences:
        while len(sentence) > maximum:
            cuts = [m.end() for m in re.finditer(r"[，,；;：:\s]", sentence[:maximum])]
            preferred = [cut for cut in cuts if cut >= target]
            cut = min(preferred, key=lambda n: abs(n - target)) if preferred else (cuts[-1] if cuts else maximum)
            bounded.append(sentence[:cut])
            sentence = sentence[cut:]
        if sentence:
            bounded.append(sentence)
    paragraphs = []
    current = ""
    for sentence in bounded:
        if current and len(current) + len(sentence) > maximum:
            paragraphs.append(current.strip())
            current = ""
        current += sentence
        if len(current) >= target:
            paragraphs.append(current.strip())
            current = ""
    if current.strip():
        paragraphs.append(current.strip())
    return paragraphs


def format_transcript(title, texts):
    body = "\n\n".join(make_paragraphs(join_chunk_texts(texts)))
    return (
        f"标题：{title}\n\n"
        + body + "\n"
    )


def extract_wav(src: Path, temp_dir: Path) -> Path:
    """每次任务使用独立 ASCII 临时目录，避免并行任务相互覆盖。"""
    tmp_in = temp_dir / "input.media"
    tmp_wav = temp_dir / "audio.wav"
    shutil.copy2(src, tmp_in)
    try:
        subprocess.run(
            [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(tmp_in), "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-vn", str(tmp_wav)],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"音轨提取失败，请确认输入含有音轨。\n{detail}") from exc
    finally:
        tmp_in.unlink(missing_ok=True)
    return tmp_wav


def write_text_atomic(out: Path, text: str) -> None:
    """先写同目录临时文件，成功后替换，防止写入失败破坏旧结果。"""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=out.parent, prefix=".qwen-", suffix=".tmp",
                                         delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, out)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def transcribe_chunks(model, chunks, sr, torch, want_ts=False, batch_size=BATCH_SIZE):
    """显存不足时降为单块重试；其他错误直接报告，不静默丢块。
    返回 (每块正文, 每块词级时间戳)；未开时间轴时后者为空列表。"""
    texts = []
    all_words = []
    index = 0
    while index < len(chunks):
        batch = chunks[index:index + batch_size]
        oom = False
        try:
            with torch.inference_mode():
                results = model.transcribe(audio=[(c[0], sr) for c in batch],
                                           return_time_stamps=want_ts)
        except torch.cuda.OutOfMemoryError:
            if len(batch) == 1:
                raise RuntimeError(
                    f"第 {index + 1} 块单独识别仍显存不足。请关闭其他占用显卡的程序后重试。"
                    "本次未覆盖已有 TXT。"
                ) from None
            oom = True
        # 离开异常处理后，失败调用的 traceback 才能释放 GPU 张量引用。
        if oom:
            gc.collect()
            torch.cuda.empty_cache()
            batch_size = max(1, (len(batch) + 1) // 2)
            model.max_inference_batch_size = batch_size
            print(f"显存不足，改为每次识别 {batch_size} 块并重试当前批次。", flush=True)
            continue
        if len(results) != len(batch):
            raise RuntimeError(f"第 {index + 1} 块起返回结果数量不符，停止以避免漏掉正文。")
        for offset, result in enumerate(results):
            if not isinstance(result.text, str):
                raise RuntimeError("模型返回了无效正文，停止保存。")
            content = result.text.strip()
            if not content:
                print(f"提示：第 {index + offset + 1} 块未识别出文字，可能为静音。", flush=True)
            texts.append(content)
            words = []
            if want_ts and result.time_stamps is not None:
                words = list(result.time_stamps.items)
            all_words.append(words)
        index += len(batch)
        del results
        print(f"  {index}/{len(chunks)}", flush=True)
    return texts, all_words


def diarize(wav, sr, speakers):
    """按音色区分说话人，返回 [(说话人序号, 起秒, 止秒)] 按时间排序。
    首次运行会从 ModelScope 自动下载声纹模型（约几十 MB）。"""
    import numpy as np
    from funasr import AutoModel
    from sklearn.cluster import KMeans

    print("说话人分离中（首次运行需下载声纹模型，约几十 MB）...", flush=True)
    spk_model = AutoModel(model="iic/speech_eres2netv2_sv_zh-cn_16k-common",
                          disable_update=True, log_level="ERROR")

    # 1) 1.5s 滑窗（步长 0.75s）铺满全文件，剔除近乎静音的窗口：
    #    静音窗口的声纹没有意义，混进聚类只会捣乱。
    win, hop = int(sr * 1.5), int(sr * 0.75)
    starts = sorted(set(list(range(0, max(1, len(wav) - win), hop)) + [max(0, len(wav) - win)]))
    loud = np.array([float(np.sqrt((wav[s:s + win] ** 2).mean())) for s in starts])
    floor = max(1e-3, 0.1 * float(np.percentile(loud, 90)))
    starts = [s for s, r in zip(starts, loud) if r >= floor]
    if len(starts) < speakers * 2:
        raise RuntimeError("有效人声窗口太少，无法区分说话人。")
    clips, spans = [], []
    for st in starts:
        clip = wav[st:st + win]
        if len(clip) < win:
            clip = np.pad(clip, (0, win - len(clip)))
        clips.append(clip)
        spans.append((st / sr, min(st + win, len(wav)) / sr))

    embs = []
    for i in range(0, len(clips), 32):
        out = spk_model.generate(input=clips[i:i + 32], batch_size=32, cache={})
        # 声纹模型把整批打包成一个 (N, 192) 张量返回，而非每段一个结果。
        emb = out[0]["spk_embedding"]
        emb = emb.detach().cpu().numpy() if hasattr(emb, "detach") else np.asarray(emb)
        emb = emb.reshape(len(emb), -1)
        if len(emb) != len(clips[i:i + 32]):
            raise RuntimeError("声纹模型返回数量与输入窗口数不符。")
        embs.extend(emb)

    # 3) 已知人数聚类。声纹先单位化，欧氏距离与余弦等价；KMeans 质心
    #    落在音色团中心，而凝聚聚类的链式合并会被"跨说话人窗口"带偏。
    X = np.stack(embs)
    X = X / np.linalg.norm(X, axis=1, keepdims=True)
    labels = list(KMeans(n_clusters=speakers, n_init=10,
                         random_state=0).fit_predict(X))

    # 4) 标签平滑：孤立的翻转窗口跟随邻居，避免一次发言被切碎。
    smoothed = labels[:]
    for i in range(len(labels)):
        neigh = labels[max(0, i - 1):i + 2]
        smoothed[i] = max(set(neigh), key=neigh.count)
    labels = smoothed

    # 5) 相邻同标签窗口合并为发言段；丢弃不足 0.5s 的碎段。
    turns = []
    for (t0, t1), who in zip(spans, labels):
        if turns and turns[-1][0] == who and t0 - turns[-1][2] <= 1.0:
            turns[-1][2] = t1
        else:
            turns.append([who, t0, t1])
    turns = [t for t in turns if t[2] - t[1] >= 0.5]
    order = {}
    for who, _, _ in turns:
        order.setdefault(who, len(order) + 1)
    print(f"分离完成：{len(order)} 位说话人，共 {len(turns)} 次发言", flush=True)
    return [(order[who], t0, t1) for who, t0, t1 in turns]


def format_dialogue(title, turn_texts):
    """多人版输出：每次发言带说话人标签，段落仍按完整句子切分。"""
    blocks = []
    for who, text in turn_texts:
        if not text:
            continue
        paragraphs = make_paragraphs(text)
        blocks.append(f"说话人{who}：{paragraphs[0]}")
        blocks.extend(paragraphs[1:])
    return (
        f"标题：{title}\n\n"
        + "\n\n".join(blocks) + "\n"
    )


def fmt_srt_time(sec):
    """秒 → SRT 时间码 HH:MM:SS,mmm。"""
    ms = max(0, round(sec * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_cues(words, prefix=""):
    """词级时间戳聚成字幕条：句末标点、长度或时长超限、长停顿处断条。
    words 为 [(文本, 起秒, 止秒)]，须已换算为全局时间。"""
    cues = []
    buf = []
    start = last_end = None

    def flush():
        nonlocal buf, start
        if buf:
            text = ""
            for w in buf:
                if (text and text[-1].isascii() and text[-1].isalnum()
                        and w[:1].isascii() and w[:1].isalnum()):
                    text += " "
                text += w
            if text:
                # 过短的条撑到 0.6 秒，保证观众能看清。
                cues.append([start, max(last_end, start + 0.6), prefix + text])
        buf.clear()
        start = None

    for word, t0, t1 in words:
        if start is not None and t0 - last_end > 1.2:
            flush()  # 句间长停顿，另起一条
        if not buf:
            start = t0
        buf.append(word)
        last_end = t1
        joined = "".join(buf)
        if (len(joined) >= 10 and joined[-1:] in "。！？!?…；;，,.") \
                or len(joined) >= 42 or t1 - start >= 7.0:
            flush()
    flush()
    # 时间轴保持单调：上一条结束晚于本条起点时，把本条往后推。
    for i in range(1, len(cues)):
        if cues[i][0] < cues[i - 1][1]:
            cues[i][1] = max(cues[i][1], cues[i - 1][1] + 0.2)
            cues[i][0] = cues[i - 1][1]
    return cues


def format_srt(cues):
    """字幕条列表 → SRT 文本。"""
    blocks = [f"{i}\n{fmt_srt_time(a)} --> {fmt_srt_time(b)}\n{t}"
              for i, (a, b, t) in enumerate(cues, 1)]
    return "\n\n".join(blocks) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="本地 Qwen 转写，只输出同名 .txt")
    parser.add_argument("input", type=Path, help="视频或音频文件路径")
    parser.add_argument("--speakers", type=int, default=1,
                        help="说话人数：1 保持单人格式（默认），≥2 输出带说话人标注")
    parser.add_argument("--format", choices=("txt", "srt", "both"), default="txt",
                        help="输出格式：txt 纯文本（默认）、srt 字幕、both 两者（srt 需对齐模型）")
    args = parser.parse_args()
    if not 1 <= args.speakers <= 8:
        raise ValueError("说话人数必须在 1 到 8 之间。")
    src = args.input.resolve()
    if not src.is_file():
        raise FileNotFoundError(f"输入文件不存在：{src}")
    if not Path(FFMPEG).is_file():
        raise FileNotFoundError(f"找不到 FFmpeg：{FFMPEG}")
    if not Path(MODEL_DIR).is_dir():
        raise FileNotFoundError(f"找不到模型目录：{MODEL_DIR}")
    if args.format in ("srt", "both") and not Path(ALIGNER_DIR).is_dir():
        raise FileNotFoundError(f"找不到对齐模型目录：{ALIGNER_DIR}")
    out = src.with_suffix(".txt")
    if out == src:
        raise ValueError("请传入音视频文件，不能将输出 TXT 当作输入。")

    import soundfile as sf
    import torch
    from qwen_asr import Qwen3ASRModel
    from qwen_asr.inference.utils import split_audio_into_chunks

    if not torch.cuda.is_available():
        raise RuntimeError("未检测到可用的 CUDA 显卡，请检查驱动和运行环境。")

    with tempfile.TemporaryDirectory(prefix="qwen-", dir=WORKDIR) as temp_dir:
        wav_path = extract_wav(src, Path(temp_dir))
        wav, sr = sf.read(wav_path, dtype="float32", always_2d=False)
    if len(wav) == 0:
        raise ValueError("输入音轨为空。")
    print(f"音频 {len(wav)/sr/60:.1f} 分钟 @ {sr}Hz", flush=True)

    turns = None
    if args.speakers >= 2:
        turns = diarize(wav, sr, args.speakers)
        gc.collect()                      # 先释放声纹模型显存，再加载转写模型
        torch.cuda.empty_cache()

    want_ts = args.format in ("srt", "both")
    chunk_sec = ALIGN_CHUNK_SEC if want_ts else CHUNK_SEC
    batch = ALIGN_BATCH_SIZE if want_ts else BATCH_SIZE
    print("加载模型...", flush=True)
    load_kwargs = dict(dtype=torch.bfloat16, device_map="cuda:0",
                       max_new_tokens=4096, max_inference_batch_size=batch)
    if want_ts:
        # 开时间轴时同时加载官方 0.6B 对齐模型，与主模型共用显卡。
        load_kwargs["forced_aligner"] = ALIGNER_DIR
        load_kwargs["forced_aligner_kwargs"] = dict(dtype=torch.bfloat16, device_map="cuda:0")
    model = Qwen3ASRModel.from_pretrained(MODEL_DIR, **load_kwargs)

    cues = []
    if turns is None:
        chunks = split_audio_into_chunks(wav=wav, sr=sr, max_chunk_sec=chunk_sec)
        print(f"转写中（{len(chunks)} 块，每块目标约 {chunk_sec}s）...", flush=True)
        texts, words = transcribe_chunks(model, chunks, sr, torch, want_ts=want_ts, batch_size=batch)
        if not any(texts):
            raise RuntimeError("整段音频未识别出文字，本次不覆盖已有 TXT。")
        text = format_transcript(src.stem, texts)
        char_count = len(join_chunk_texts(texts))
        # 块内时间戳相对块起点，加上块偏移（split 返回的第二项）才是全局时间。
        cues = build_cues([(w.text, w.start_time + off, w.end_time + off)
                           for (_, off), ws in zip(chunks, words) for w in ws])
    else:
        print(f"转写中（{len(turns)} 次发言）...", flush=True)
        turn_texts = []
        for index, (who, t0, t1) in enumerate(turns, 1):
            piece = wav[int(t0 * sr):int(t1 * sr)]
            chunks = split_audio_into_chunks(wav=piece, sr=sr, max_chunk_sec=chunk_sec)
            texts, words = transcribe_chunks(model, chunks, sr, torch, want_ts=want_ts, batch_size=batch)
            turn_texts.append((who, join_chunk_texts(texts)))
            cues.extend(build_cues(
                [(w.text, w.start_time + off + t0, w.end_time + off + t0)
                 for (_, off), ws in zip(chunks, words) for w in ws],
                prefix=f"说话人{who}："))
            print(f"  发言 {index}/{len(turns)} 完成", flush=True)
        if not any(t for _, t in turn_texts):
            raise RuntimeError("整段音频未识别出文字，本次不覆盖已有 TXT。")
        text = format_dialogue(src.stem, turn_texts)
        char_count = sum(len(t) for _, t in turn_texts)
    if args.format in ("txt", "both"):
        write_text_atomic(out, text)
        print(f"正文约 {char_count} 字，已按完整句子分段")
    if args.format in ("srt", "both"):
        if cues:
            srt_path = src.with_suffix(".srt")
            write_text_atomic(srt_path, format_srt(cues))
            print(f"字幕: {srt_path}")
        elif args.format == "srt":
            raise RuntimeError("识别出文字但未获得时间轴，本次未生成字幕。")
        else:
            print("提示：未生成字幕（没有带时间戳的识别结果）。")
    done = src.with_suffix(".srt") if args.format == "srt" else out
    print(f"完成: {done}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        sys.exit(1)
