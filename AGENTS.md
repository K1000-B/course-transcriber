# AGENTS.md — Course Transcriber

> Repository-level instructions for Codex and other coding agents. These rules apply to the entire repository. Follow the user's current request first, then these project conventions.

## 1. Project mission

Build and maintain a **local, Docker-first, CPU-only** command-line tool that converts university lecture recordings into timestamped text transcripts. It must run on **Windows (x86-64), macOS Intel, and macOS Apple Silicon**, without requiring Python, FFmpeg, or Whisper on the host.

**Supported workflow:**

1. Accept an HTTP(S) MP4/M3U8 media URL or a local audio/video file under `input/`.
2. Use FFmpeg to extract mono PCM WAV at **16 kHz**, split into **60-second** chunks.
3. Transcribe each chunk with OpenAI Whisper **on CPU**.
4. Write timestamped UTF-8 text to `export/`, preserving progress in `.partial.txt` during processing.
5. Delete generated files in `temp/`; preserve original media, models, and exports.

The product is a **terminal application**, not a web app or GUI. Keep user-facing terminal messages and source-code identifiers in **English**.

## 2. Source of truth and repository map

Read these files before changing relevant behavior:

| Path | Responsibility |
| --- | --- |
| `README.md` | User-facing setup, use, shutdown, limitations, and troubleshooting |
| `main.py` | CLI, source selection, FFmpeg, Whisper model management, transcription, and export |
| `Dockerfile` | Containerized Python 3.11, FFmpeg, and CPU-only PyTorch runtime |
| `compose.yaml` | Interactive service `transcriber` and project bind mount |
| `requirements.txt` | Python application dependencies |
| `.devcontainer/devcontainer.json` | VS Code container-based development |
| `input/` | User-owned source media — **never delete or overwrite** |
| `models/` | Persisted Whisper checkpoints — delete **only** on explicit model-manager request |
| `export/` | Final `.txt` and recoverable `.partial.txt` transcripts — **never delete automatically** |
| `temp/` | Generated working audio — safe to clean only within this directory |

Inspect the actual repository before making assumptions; this table describes the intended structure, not proof that every file currently exists. If code and documentation disagree, identify the discrepancy and fix it within the requested scope.

## 3. Non-negotiable architecture

- **Exactly one application service:** `compose.yaml` must contain only the `transcriber` service. Keep the product terminal-only; do not add a GUI, native launcher, Electron, Node.js/npm, Tauri, IPC layer, browser container, or additional application service.
- **Docker is the default runtime.** Do not require host-side Python, pip, virtual environments, FFmpeg, or system packages.
- **CPU-only is a product decision.** Use `device="cpu"` and `fp16=False` for inference; do not add CUDA, MPS, GPU discovery, GPU Docker images, or device-dependent code unless the user explicitly changes the requirement.
- **Multi-architecture:** support Linux `amd64` and `arm64` images. Do not force `platform: linux/amd64`, introduce x86-only binaries, or assume WSL is available on macOS.
- **Keep it offline-capable after setup:** local media and already-downloaded models must work without internet. Network is expected only for initial dependency/model download or user-supplied remote URLs.
- **No external transcription API, credentials, telemetry, database, or cloud storage** without explicit authorization.
- **Headless by design:** do not use `tkinter`, native file pickers, desktop dialogs, or host-specific absolute paths. Local files are selected from the bind-mounted `input/` directory.
- Keep the CLI simple; do not introduce a web framework, queue, microservices, or unrelated dependencies without a demonstrated need.

**Known alignment issue:** older revisions of `main.py` may implement `get_device()` with `torch.cuda.is_available()`. That is legacy behavior, **not** permission to implement GPU acceleration. When touching this logic, make it unambiguously CPU-only.

## 4. Functional invariants

- Maintain both **local files** and **direct HTTP(S) media URLs** (including MP4 and M3U8). A webpage URL is not necessarily a playable media URL.
- Invoke FFmpeg/FFprobe with an **argument list** (`subprocess.run([...])`), never `shell=True` for media URLs or filenames. Capture useful stderr and report actionable failures.
- Probe duration on a best-effort basis: unavailable metadata must not prevent a valid media stream from being processed.
- Preserve **60 s** segmentation, **16,000 Hz**, **mono**, **16-bit PCM WAV**, and consistent `[HH:MM:SS]` chunk timestamps unless the user requests changes.
- Preserve language choices: **English**, **French**, and **automatic detection**. Do not select an English-only model for explicitly French transcription.
- Download a missing Whisper model on demand to `models/`. Reuse installed weights, allow explicit deletion, and account for model-name aliases referring to the same file.
- Create a distinct export filename rather than overwriting an existing transcript. Write each completed chunk to `.partial.txt`, flush it, and rename it to `.txt` **only after successful completion**.
- Keep interrupted/failed `.partial.txt` files for recovery; **do not imply automatic resume is implemented**.
- Release inference resources and clean temporary audio on success, failure, and user interruption. **Never delete `input/`, `models/`, or `export/` as part of cleanup.**
- Never log complete signed/tokenized media URLs, cookies, authorization headers, or secrets.
- Do not implement DRM circumvention or authentication bypass. Treat remote URLs as untrusted inputs.

## 5. Development conventions

- Target **Python 3.11**. Use type hints, `pathlib.Path`, descriptive names, small functions, and standard-library features when sufficient.
- Prefer minimal, reviewable changes over broad refactors; preserve existing menu options and output format unless asked to change them.
- Separate concerns as the code grows (media handling, model management, transcription, CLI), but **do not** split files purely for aesthetic reasons.
- Avoid unnecessary memory consumption: load one Whisper model per transcription job, and do not load whole video files into Python memory.
- Avoid uncontrolled concurrency: parallel Whisper jobs on CPU can exhaust RAM and reduce throughput.
- Use deterministic behavior and clear errors. Validate menu indices, file paths, and configuration values.
- Never commit source videos, `.pt` weights, partial/final transcripts, secrets, caches, or temporary files. Verify `.gitignore` covers `input/`, `models/`, `export/`, `temp/`, `.env`, and Python caches. `.dockerignore` alone is insufficient.
- Update `README.md` whenever installation commands, menu behavior, data retention, supported input formats, or dependencies change.
- Do not edit unrelated files or reformat the whole project for a small request.

## 6. Run and validate

**From a host terminal, at the repository root:**

```bash
docker compose config
docker compose build
docker compose run --rm transcriber
docker compose down
```

**From an already-open VS Code Dev Container terminal:**

```bash
python main.py
```

Do **not** use `docker compose run` inside the Dev Container as the normal execution path. Code-only edits are bind-mounted and ordinarily do not need an image rebuild; dependency or Dockerfile edits do.

**Checks after Python changes** (inside the image; avoid host Python dependencies):

```bash
docker compose run --rm transcriber python -m py_compile main.py
```

If a `tests/` suite exists, run it inside the container:

```bash
docker compose run --rm transcriber python -m unittest discover -s tests -v
```

Test changed paths with **small generated media fixtures** rather than private course recordings. Prefer unit tests/mocks for URL failures, model downloads, model deletion, partial-export recovery, filename collisions, and cleanup; never download large Whisper models as part of routine automated tests. Reserve real Whisper/FFmpeg end-to-end tests for an explicitly requested integration run.

If Docker, network access, or hardware is unavailable, **do not claim tests passed**. Run available static checks and report what remains unverified.

## 7. How to handle Codex tasks

1. **Inspect first:** read the relevant files, README, and existing tests before editing.
2. **Scope the change:** identify which user-visible behavior is changing and preserve all invariants outside that scope.
3. **Implement:** use the smallest maintainable diff; avoid new dependencies unless justified.
4. **Verify:** execute feasible targeted checks, then inspect the diff for accidental data deletion, host-only assumptions, and regressions.
5. **Report concisely:** list files changed, relevant decisions, checks actually run (and results), and any remaining limitations.

Do not silently redesign the product, change to GPU inference, delete user data, publish files, push commits, or run destructive Docker cleanup. Ask for authorization before destructive or externally visible actions when not explicitly requested.
