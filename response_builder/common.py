from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


class SetupError(RuntimeError):
    """A safe, user-facing message, never a raw remote exception."""


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise SetupError(f"Не удалось прочитать JSON: {path.name}") from None


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def project_name(value: str) -> str:
    value = value.strip()
    if (not value or value in {".", ".."} or value.endswith((".", " "))
            or re.search(r'[<>:"/\\|?*\x00-\x1f%!:&^]', value)
            or value.startswith("_") or len(value) > 100
            or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?", value)):
        raise SetupError("Недопустимое имя папки проекта. Не используйте служебные имена и спецсимволы.")
    return value


def google_id(value: str) -> str:
    value = value.strip()
    if value.startswith("https://"):
        match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
        if not match or urlsplit(value).hostname != "docs.google.com":
            raise SetupError("Нужен ID или ссылка Google-таблицы.")
        value = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise SetupError("Некорректный ID Google-таблицы.")
    return value


def column_index(value: str) -> int:
    if not re.fullmatch(r"[A-Za-z]+", value):
        raise SetupError("Столбец задаётся буквами: B, Z, AA.")
    number = 0
    for char in value.upper():
        number = number * 26 + ord(char) - ord("A") + 1
    if number > 18278:
        raise SetupError("Номер столбца вне диапазона Google Sheets.")
    return number - 1


def column_name(index: int) -> str:
    value = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        value = chr(65 + rest) + value
    return value


def sheet_range(title: str, cells: str) -> str:
    return "'" + title.replace("'", "''") + "'!" + cells


@contextlib.contextmanager
def file_lock(path: Path):
    """OS-held lock: released even after a crash; keep the lock file in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SetupError("Другой процесс уже работает с этим проектом/авторизацией.") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def safe_failure(exc: BaseException) -> str:
    # HTTP bodies, cookie headers, credential errors and resume text are not logged.
    return str(exc) if isinstance(exc, SetupError) else f"Сбой {type(exc).__name__}; секретные детали скрыты."
