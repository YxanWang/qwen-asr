# qwen-asr 本地转写工具

全程本地的音视频转文字工具：Qwen3-ASR 转写 + 说话人分离 + SRT 词级时间轴，Tkinter 队列界面。无任何云端调用。

## 文件说明

| 文件 | 用途 |
|---|---|
| `gui.py` | 图形界面（纯标准库 Tkinter），队列转写、说话人数、输出格式 |
| `transcribe.py` | 转写引擎，CLI：`.venv\Scripts\python.exe transcribe.py 文件 --speakers N --format txt\|srt\|both` |
| `pyproject.toml` + `uv.lock` | uv 环境定义（121 个包逐版本钉死，torch 走 cu128 显式源） |
| `ASR转写.lnk` | 启动快捷方式 → `.venv\Scripts\pythonw.exe gui.py`（绝对路径，仅本机布局有效） |

**不入库的大文件**（`.gitignore` 已排除，恢复方法见下）：`.venv\`、两个模型文件夹、`bin\ffmpeg.exe`。

## 新机器恢复步骤

1. `git clone` 到 `D:\Program\qwen-asr`（`transcribe.py` 顶部常量硬编码了此路径，换路径需同步修改 `MODEL_DIR` / `ALIGNER_DIR`）
2. 安装 [uv](https://docs.astral.sh/uv/)，项目目录下执行 `uv sync` —— 完整复原 Python 3.12 + 全部依赖（含 torch 2.11.0+cu128）
3. 从 ModelScope 下载两个模型，解压/放入项目根目录：
   - `Qwen3-ASR-1.7B`（转写主模型，约 4.4GB）
   - `Qwen3-ForcedAligner-0.6B`（SRT 时间轴对齐器，约 1.8GB，只用 TXT 可不放）
   - 说话人分离的声纹模型 `iic/speech_eres2netv2_sv_zh-cn_16k-common` 无需手动下载，首次用 `--speakers ≥2` 时自动获取（~100MB，落 ModelScope 缓存）
4. `ffmpeg.exe` 放入 `bin\`（约 83MB），或保证系统 PATH 里已有 ffmpeg（代码逻辑：PATH 优先、bin 兜底）
5. （可选）新建快捷方式指向 `.venv\Scripts\pythonw.exe gui.py`，起始位置设为项目目录

## 显存红线（RTX 4060 Laptop 8GB 实测）

- 对齐器必须 `ALIGN_CHUNK_SEC=30` + 单批：整段全序列前向会 CUDA 非法访问，曾把机器搞黑屏强制重启；30s 单批实测峰值 ~7.7GB 稳定
- 转写 + 说话人分离同时开启时显存峰值较高，不要与其他 GPU 任务并行
