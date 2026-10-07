"""Interactive, local-only import of Google JSON files; never log their contents."""
from __future__ import annotations

import getpass
import json
from pathlib import Path
import warnings
from urllib.parse import urlsplit

from google.oauth2.service_account import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .common import SetupError, atomic_json, file_lock, read_json


def hidden_input(prompt):
    # getpass normally falls back to echoed input without a usable terminal. Refuse that fallback.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except (getpass.GetPassWarning, EOFError, OSError):
        raise SetupError("Скрытый ввод недоступен. Запустите BAT в обычном окне Windows или выберите JSON-файл.") from None


def validate_service_account(data):
    try:
        if not isinstance(data, dict) or data.get("type") != "service_account":
            raise ValueError
        if not str(data.get("client_email", "")).endswith(".gserviceaccount.com"):
            raise ValueError
        if data.get("token_uri") not in {
            "https://oauth2.googleapis.com/token", "https://accounts.google.com/o/oauth2/token"
        }:
            raise ValueError
        Credentials.from_service_account_info(data)
    except Exception:
        raise SetupError("Некорректный Google service-account JSON. Выберите скачанный ключ сервисного аккаунта.") from None


def validate_oauth_client(data):
    try:
        if not isinstance(data, dict) or not isinstance(data.get("installed"), dict):
            raise ValueError
        client = data["installed"]
        if not all(client.get(key) for key in ("client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris")):
            raise ValueError
        auth = urlsplit(client["auth_uri"])
        if auth.scheme != "https" or auth.hostname != "accounts.google.com" or auth.username or auth.password:
            raise ValueError
        if client["token_uri"] not in {
            "https://oauth2.googleapis.com/token", "https://accounts.google.com/o/oauth2/token"
        }:
            raise ValueError
        InstalledAppFlow.from_client_config(data, ["https://www.googleapis.com/auth/drive"])
    except Exception:
        raise SetupError("Нужен Google OAuth JSON типа Desktop app (installed), не service account и не Web client.") from None


def choose_json_file():
    """Native dialog on Windows; hidden path prompt when Tk isn't available."""
    window = None
    try:
        import tkinter as tk
        from tkinter import filedialog
        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        selected = filedialog.askopenfilename(parent=window, title="Выберите Google credentials JSON",
            filetypes=[("Google JSON", "*.json"), ("Все файлы", "*.*")])
        if not selected:
            raise SetupError("Выбор Google credentials отменён.")
        return Path(selected)
    except SetupError:
        raise
    except Exception:
        print("Диалог выбора недоступен. Укажите путь к скачанному JSON (ввод скрыт).")
        path = hidden_input("Путь к JSON: ").strip().strip('"')
        if not path:
            raise SetupError("Выбор Google credentials отменён.")
        return Path(path)
    finally:
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass


def import_credentials(shared, filename, label, validator, existing=None):
    existing = existing or shared / filename
    if existing.exists():
        try:
            validator(read_json(existing))
            return existing
        except SetupError:
            print(f"Сохранённый {label} не прошёл проверку. Содержимое не выводится.")
            if input("Заменить этот файл? Введите ДА, иначе отмена: ").strip().upper() != "ДА":
                raise SetupError("Google credentials не заменены.")
    print(f"Нужен {label}.")
    print("1 — выбрать скачанный JSON-файл; 2 — вставить JSON одной строкой (скрытый ввод); Enter — отмена.")
    choice = input("Способ ввода: ").strip()
    if choice == "1":
        data = read_json(choose_json_file())
    elif choice == "2":
        # No shell arguments, history, clipboard reads or echoed secret values.
        try:
            data = json.loads(hidden_input("Google JSON (ввод скрыт): "))
        except ValueError:
            raise SetupError("Некорректный JSON. Секретные данные не сохранены.") from None
    else:
        raise SetupError("Ввод Google credentials отменён.")
    validator(data)  # failed import leaves any existing file byte-for-byte unchanged
    target = existing if existing.exists() else shared / filename
    atomic_json(target, data)
    print(f"{label} сохранён централизованно в _shared. Содержимое не выводится.")
    return target


def ensure_google_credentials(shared: Path, need_oauth=True):
    shared.mkdir(parents=True, exist_ok=True)
    with file_lock(shared / ".credential-import.lock"):
        service = shared / "google_service_account.json"
        legacy = shared / "google_credentials.json"
        import_credentials(shared, "google_service_account.json", "ключ Google service account",
                           validate_service_account, existing=legacy if not service.exists() and legacy.exists() else service)
        if need_oauth:
            import_credentials(shared, "google_oauth_client.json", "OAuth client Google типа Desktop app",
                               validate_oauth_client)
