from __future__ import annotations

import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

RELEASE = Path(__file__).resolve().parent
PROJECT = RELEASE.parent
SOURCE_FILE = PROJECT / "source.json"
VERSION_FILE = RELEASE / "version.json"
TIMEOUT = 20


def version_key(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in value.strip().split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts) or (0,)


def parse_github(url: str) -> Optional[tuple[str, str]]:
    text = url.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if "github.com/" in text:
        text = text.split("github.com/", 1)[1]
    bits = [bit for bit in text.split("/") if bit]
    if len(bits) < 2 or ":" in bits[0]:
        return None
    return bits[0], bits[1]


def apply() -> bool:
    try:
        return _apply()
    except Exception:
        return False


def _apply() -> bool:
    if not SOURCE_FILE.exists():
        return False
    config = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    remote = parse_github(str(config.get("url", "")))
    if remote is None:
        return False
    owner, repo = remote
    branch = str(config.get("branch") or "main").strip() or "main"
    current = _read_version(VERSION_FILE)
    latest = _remote_version(owner, repo, branch)
    if latest is None or version_key(latest) <= version_key(current):
        return False
    window = _notice(f"Updating Chest Counter to {latest}...")
    scratch = None
    try:
        downloaded = _download_release(owner, repo, branch)
        if downloaded is None:
            return False
        payload, scratch = downloaded
        _replace(payload, RELEASE)
        got = _read_version(VERSION_FILE)
        return version_key(got) >= version_key(latest)
    finally:
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        if window is not None:
            window.destroy()


def _read_version(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "0"
    return str(data.get("version", "0"))


def _read_url(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "chest-counter"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def _remote_version(owner: str, repo: str, branch: str) -> Optional[str]:
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/release/version.json"
    try:
        data = json.loads(_read_url(url).decode("utf-8"))
    except Exception:
        return None
    version = data.get("version")
    return str(version) if version else None


def _download_release(owner: str, repo: str, branch: str) -> Optional[tuple[Path, Path]]:
    url = f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{branch}"
    temp = Path(tempfile.mkdtemp(prefix="chest-counter-"))
    archive = temp / "update.zip"
    try:
        archive.write_bytes(_read_url(url))
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        return None
    extracted = temp / "src"
    extracted.mkdir()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                name = info.filename.replace("\\", "/")
                if not name or name.endswith("/") or ".." in Path(name).parts:
                    continue
                target = extracted / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        return None
    kids = [path for path in extracted.iterdir() if path.is_dir()]
    base = kids[0] if len(kids) == 1 else extracted
    payload = base / "release"
    if not (payload / "version.json").exists():
        shutil.rmtree(temp, ignore_errors=True)
        return None
    return payload, temp


def _replace(payload: Path, destination: Path) -> None:
    incoming: set[Path] = set()
    for path in payload.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        rel = path.relative_to(payload)
        incoming.add(rel)
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for path in list(destination.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.relative_to(destination) not in incoming:
            path.unlink()


def _notice(text: str):
    import tkinter as tk

    root = tk.Tk()
    root.title("Chest Counter")
    root.geometry("360x90")
    root.resizable(False, False)
    icon = RELEASE / "icon.ico"
    if icon.exists():
        try:
            root.iconbitmap(icon)
        except tk.TclError:
            pass
    tk.Label(root, text=text, padx=16, pady=24).pack(fill="both", expand=True)
    root.update()
    return root
