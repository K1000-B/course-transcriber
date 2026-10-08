"""Docker-first command-line course transcriber (Whisper + FFmpeg)."""

from __future__ import annotations

import gc
import math
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import torch
import whisper


# -----------------------------------------------------------------------------
# Configuration: the project directory is mounted at /workspace in Docker.
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
EXPORT_DIR = BASE_DIR / "export"
MODELS_DIR = BASE_DIR / "models"
TEMP_DIR = BASE_DIR / "temp"

SEGMENT_DURATION = 60  # seconds
AUDIO_SAMPLE_RATE = 16_000  # Hz
MEDIA_EXTENSIONS = {
    ".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts",
    ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus",
}
USER_AGENT = "Mozilla/5.0 (compatible; CourseTranscriber/1.0)"


def ensure_directories() -> None:
    for directory in (INPUT_DIR, EXPORT_DIR, MODELS_DIR, TEMP_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def clean_temp() -> None:
    """Remove only generated working files, never original input videos."""
    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)


def clear_screen() -> None:
    # ANSI escape codes work in VS Code, macOS Terminal and modern Windows terminals.
    print("\033[2J\033[H", end="", flush=True)


def pause() -> None:
    input("\nPress Enter to continue...")


def header(title: str) -> None:
    print("=" * 60)
    print(title.center(60))
    print("=" * 60)


def ask_index(count: int, prompt: str) -> int | None:
    """Return a zero-based selection or None (including 0 / Enter = cancel)."""
    response = input(prompt).strip()
    if not response or response == "0":
        return None
    if not response.isdecimal():
        print("Invalid selection.")
        return None
    index = int(response) - 1
    if not 0 <= index < count:
        print("Invalid selection.")
        return None
    return index


def format_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "unknown"
    total = int(seconds)
    return f"{total // 3600:02}:{(total % 3600) // 60:02}:{total % 60:02}"


def sanitize_filename(name: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name.strip())
    value = re.sub(r"\s+", "_", value)
    value = re.sub(r"_+", "_", value).strip(" ._")
    return value[:100] or "transcript"


def check_dependencies() -> None:
    missing = [executable for executable in ("ffmpeg", "ffprobe") if not shutil.which(executable)]
    if missing:
        raise RuntimeError(f"Missing executables inside Docker: {', '.join(missing)}")


# -----------------------------------------------------------------------------
# Input selection: do not use tkinter inside a headless Linux container.
# -----------------------------------------------------------------------------
def select_local_file() -> Path | None:
    files = sorted(
        (path for path in INPUT_DIR.rglob("*")
         if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS),
        key=lambda path: str(path).lower(),
    )
    print(f"\nLocal files in {INPUT_DIR}:")
    if files:
        for number, path in enumerate(files, start=1):
            print(f"  {number:2}. {path.relative_to(INPUT_DIR)}")
    else:
        print("  (none: copy a video/audio file into the input/ folder)")

    print("\nType a file number, a container-visible path, or 0 to cancel.")
    answer = input("> ").strip().strip('"').strip("'")
    if not answer or answer == "0":
        return None

    if answer.isdecimal():
        index = int(answer) - 1
        if not 0 <= index < len(files):
            print("Invalid file number.")
            return None
        return files[index]

    candidate = Path(answer).expanduser()
    if not candidate.is_absolute():
        candidate = INPUT_DIR / candidate
    candidate = candidate.resolve()
    if not candidate.is_file():
        print(f"File not found inside the container: {candidate}")
        print("Host paths are not automatically visible. Use the project's input/ folder.")
        return None
    return candidate


def choose_source() -> tuple[str, str | Path] | None:
    print("\nMedia source:\n  1. Remote URL (M3U8 / MP4)\n  2. Local file (input/)\n  0. Cancel")
    choice = input("> ").strip()
    if choice == "1":
        url = input("Media URL: ").strip()
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            print("Please provide a valid HTTP(S) URL.")
            return None
        return "url", url
    if choice == "2":
        selected = select_local_file()
        return ("local", selected) if selected else None
    return None


def network_options(source_type: str) -> list[str]:
    if source_type != "url":
        return []
    return [
        "-rw_timeout", "30000000",  # 30 s network I/O timeout (microseconds)
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
        "-reconnect_delay_max", "10",
        "-user_agent", USER_AGENT,
    ]


def probe_duration(source_type: str, source: str | Path) -> float | None:
    """Best-effort probe: unreachable metadata must not block transcription."""
    command = [
        "ffprobe", "-v", "error",
        *(["-rw_timeout", "30000000", "-user_agent", USER_AGENT]
          if source_type == "url" else []),
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
        if result.returncode != 0:
            return None
        duration = float(result.stdout.strip())
        return duration if math.isfinite(duration) and duration > 0 else None
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return None


def extract_audio_segments(source_type: str, source: str | Path) -> list[Path]:
    """Stream directly from URL/file to 60s mono, 16 kHz PCM WAV chunks."""
    clean_temp()
    output_pattern = TEMP_DIR / "segment_%05d.wav"
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        *network_options(source_type),
        "-i", str(source),
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-ac", "1", "-ar", str(AUDIO_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        "-f", "segment", "-segment_time", str(SEGMENT_DURATION),
        "-segment_format", "wav", "-reset_timestamps", "1",
        str(output_pattern),
    ]
    print("\nExtracting and splitting audio (60-second chunks)...", flush=True)
    result = subprocess.run(command, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        details = result.stderr.strip()
        raise RuntimeError(f"FFmpeg failed.\n{details or 'No error details returned.'}")

    files = sorted(TEMP_DIR.glob("segment_*.wav"))
    if not files:
        raise RuntimeError("FFmpeg produced no audio segments.")
    print(f"Audio ready: {len(files)} segment(s).")
    return files


# -----------------------------------------------------------------------------
# Whisper model management. Whisper stores .pt files directly in models/.
# -----------------------------------------------------------------------------
def models() -> list[str]:
    return whisper.available_models()


def model_path(name: str) -> Path:
    # These official model names and URLs are provided by pinned openai-whisper.
    url = whisper._MODELS[name]
    return MODELS_DIR / Path(urlparse(url).path).name


def readable_size(path: Path) -> str:
    size = path.stat().st_size
    return f"{size / 1024 ** 3:.2f} GiB" if size >= 1024 ** 3 else f"{size / 1024 ** 2:.0f} MiB"


def show_models() -> None:
    print("\nAvailable Whisper models:\n")
    for index, name in enumerate(models(), start=1):
        path = model_path(name)
        status = f"installed ({readable_size(path)})" if path.is_file() else "not installed"
        print(f"  {index:2}. {name:<19} {status}")
    print("\nNote: large = large-v3; turbo = large-v3-turbo (shared files).")


def choose_model() -> str | None:
    show_models()
    index = ask_index(len(models()), "\nModel number (downloaded automatically if needed; 0 = cancel): ")
    return models()[index] if index is not None else None


def get_device() -> str:
    return "cuda" if torch.cuda.is_available() else "cpu"


def load_whisper_model(name: str, device: str | None = None):
    device = device or get_device()
    if not model_path(name).is_file():
        print(f"\nModel '{name}' not installed: downloading it automatically...")
    print(f"Loading model: {name} | Device: {device}", flush=True)
    return whisper.load_model(name, device=device, download_root=str(MODELS_DIR))


def download_model() -> None:
    name = choose_model()
    if name is None:
        return
    if model_path(name).is_file():
        print(f"Model '{name}' is already downloaded.")
        return
    instance = None
    try:
        instance = load_whisper_model(name, device="cpu")
        print(f"Model '{name}' downloaded successfully.")
    finally:
        del instance
        gc.collect()


def delete_model() -> None:
    # Deduplicate aliases pointing to the same checkpoint file.
    groups: dict[Path, list[str]] = {}
    for name in models():
        path = model_path(name)
        if path.is_file():
            groups.setdefault(path, []).append(name)
    if not groups:
        print("\nNo models installed.")
        return
    installed = list(groups.items())
    print("\nInstalled model files:\n")
    for number, (path, aliases) in enumerate(installed, start=1):
        print(f"  {number:2}. {', '.join(aliases)} ({readable_size(path)})")
    index = ask_index(len(installed), "\nModel to delete (0 = cancel): ")
    if index is None:
        return
    path, aliases = installed[index]
    confirmation = input(f"Delete {', '.join(aliases)} permanently? [y/N]: ").strip().lower()
    if confirmation in {"y", "yes"}:
        path.unlink()
        print("Model file deleted.")
    else:
        print("Deletion cancelled.")


def model_manager() -> None:
    while True:
        clear_screen()
        header("WHISPER MODEL MANAGER")
        print("\n1. Show models\n2. Download model\n3. Delete model\n0. Back")
        choice = input("> ").strip()
        if choice == "0":
            return
        try:
            if choice == "1":
                show_models()
            elif choice == "2":
                download_model()
            elif choice == "3":
                delete_model()
            else:
                print("Invalid option.")
        except Exception as error:
            print(f"\nERROR: {error}")
        pause()


# -----------------------------------------------------------------------------
# Transcription and export. Save progress as .partial.txt until completion.
# -----------------------------------------------------------------------------
def choose_language() -> str | None:
    print("\nLanguage:\n  1. English\n  2. French\n  3. Automatic detection")
    answer = input("> ").strip()
    return {"1": "en", "2": "fr", "3": None}.get(answer, None)


def output_paths(course_name: str) -> tuple[Path, Path]:
    stem = sanitize_filename(course_name)
    final = EXPORT_DIR / f"{stem}.txt"
    if final.exists() or (EXPORT_DIR / f"{stem}.partial.txt").exists():
        stem += "_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        final = EXPORT_DIR / f"{stem}.txt"
    partial = EXPORT_DIR / f"{stem}.partial.txt"
    return final, partial


def transcribe_segments(model, segments: list[Path], language: str | None, partial: Path) -> int:
    """Write each finished segment immediately, allowing recovery after errors."""
    written = 0
    total = len(segments)
    with partial.open("w", encoding="utf-8") as output:
        for index, segment in enumerate(segments):
            timestamp = format_duration(index * SEGMENT_DURATION)
            print(f"[{index + 1}/{total}] {timestamp} | Transcribing...", flush=True)
            options = {"verbose": False, "fp16": get_device() == "cuda"}
            if language is not None:
                options["language"] = language
            result = model.transcribe(str(segment), **options)
            text = result["text"].strip()
            if text:
                if written:
                    output.write("\n\n")
                output.write(f"[{timestamp}] {text}")
                output.flush()
                written += 1
    return written


def transcribe_course() -> None:
    clear_screen()
    header("COURSE TRANSCRIPTION")
    course_name = input("\nCourse name: ").strip() or "transcript"
    selected = choose_source()
    if selected is None:
        return
    source_type, source = selected
    print("\nReading media metadata...")
    duration = probe_duration(source_type, source)
    print(f"Duration: {format_duration(duration)}")
    if duration is None:
        print("Metadata unavailable; attempting FFmpeg extraction anyway.")
    else:
        print(f"Estimated segments: {math.ceil(duration / SEGMENT_DURATION)}")

    language = choose_language()
    model_name = choose_model()
    if model_name is None:
        return
    if model_name.endswith(".en") and language == "fr":
        print("English-only model selected for French. Choose a multilingual model.")
        return

    instance = None
    partial = None
    try:
        segments = extract_audio_segments(source_type, source)
        instance = load_whisper_model(model_name)
        final, partial = output_paths(course_name)
        transcribe_segments(instance, segments, language, partial)
        partial.replace(final)
        print(f"\nTranscription complete!\nOutput: {final}")
    except KeyboardInterrupt:
        print("\nTranscription interrupted.")
        if partial and partial.exists():
            print(f"Partial transcription saved: {partial}")
    except Exception as error:
        print(f"\nERROR: {error}")
        if partial and partial.exists():
            print(f"Partial transcription saved: {partial}")
    finally:
        del instance
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        clean_temp()
        print("Temporary audio files deleted; original input files untouched.")


def system_info() -> None:
    clear_screen()
    header("SYSTEM INFORMATION")
    import platform
    print(f"\nPython:       {platform.python_version()}")
    print(f"Platform:     {platform.system()} / {platform.machine()}")
    print(f"PyTorch:      {torch.__version__}")
    print(f"Whisper:      {getattr(whisper, '__version__', 'unknown')}")
    print(f"Device:       {get_device()}")
    print(f"FFmpeg:       {shutil.which('ffmpeg')}")
    print(f"FFprobe:      {shutil.which('ffprobe')}")
    print(f"Input:        {INPUT_DIR}")
    print(f"Export:       {EXPORT_DIR}")
    print(f"Models:       {MODELS_DIR}")
    print(f"Temporary:    {TEMP_DIR}")
    if get_device() == "cpu":
        print("\nDocker CPU mode: Apple MPS is not available in Linux containers.")


def main() -> None:
    ensure_directories()
    check_dependencies()
    clean_temp()
    while True:
        clear_screen()
        header("COURSE TRANSCRIBER")
        print("\n1. Transcribe a course\n2. Manage Whisper models\n3. System information\n0. Exit")
        choice = input("> ").strip()
        if choice == "0":
            clean_temp()
            print("Goodbye.")
            return
        try:
            if choice == "1":
                transcribe_course()
            elif choice == "2":
                model_manager()
            elif choice == "3":
                system_info()
            else:
                print("Invalid option.")
        except KeyboardInterrupt:
            print("\nCancelled.")
            clean_temp()
        except Exception as error:
            print(f"\nERROR: {error}")
            clean_temp()
        pause()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nGoodbye.")
