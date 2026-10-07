from pathlib import Path
import shutil
import subprocess
import sys
import traceback
from datetime import datetime

from playwright.sync_api import sync_playwright


# ==============================================================
# ПУТИ
# ==============================================================

BASE_DIR = Path(__file__).resolve().parent
AUTH_FILE = BASE_DIR / "rabota_auth.json"
BACKUP_DIR = BASE_DIR / "auth_backups"
RECORDINGS_DIR = BASE_DIR / "recordings"

LOGIN_URL = (
    "https://rabota.by/account/login"
    "?backurl=%2Femployer%2Fvacancies"
)

RECORD_START_URL = (
    "https://rabota.by/employer/vacancyresponses"
)


# ==============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==============================================================

def make_backup():
    if not AUTH_FILE.exists():
        return None

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"rabota_auth_{timestamp}.json"

    shutil.copy2(AUTH_FILE, backup_file)

    return backup_file


def looks_like_login_page(url: str) -> bool:
    url = (url or "").lower()

    return (
        "/account/login" in url
        or "/account/signup" in url
        or "/account/register" in url
    )


# ==============================================================
# 1. ОБНОВЛЕНИЕ АВТОРИЗАЦИИ
# ==============================================================

def refresh_auth():
    print("=" * 70)
    print("ОБНОВЛЕНИЕ АВТОРИЗАЦИИ rabota.by")
    print("=" * 70)

    print(f"Папка проекта: {BASE_DIR}")
    print(f"Файл авторизации: {AUTH_FILE}")

    if AUTH_FILE.exists():
        print(
            "\nТекущий rabota_auth.json будет сохранён в резервную копию "
            "перед заменой."
        )
    else:
        print(
            "\nrabota_auth.json пока отсутствует — "
            "будет создан новый."
        )

    with sync_playwright() as p:
        print("\nОткрываем Chromium...")

        browser = p.chromium.launch(
            headless=False
        )

        try:
            # Намеренно создаём ЧИСТЫЙ контекст.
            # Старый rabota_auth.json не загружаем.
            context = browser.new_context()

            page = context.new_page()

            print("Открываем страницу входа rabota.by...")

            page.goto(
                LOGIN_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            print("\n" + "=" * 70)
            print("ДЕЙСТВИЯ В БРАУЗЕРЕ")
            print("=" * 70)
            print("1. Войди в НУЖНЫЙ аккаунт работодателя rabota.by.")
            print("2. Пройди коды/подтверждения, если сайт их попросит.")
            print("3. Дождись полной загрузки кабинета.")
            print("4. Для надёжности открой раздел вакансий или откликов.")
            print("5. НИЧЕГО не закрывай.")
            print("6. Вернись в это окно и нажми Enter.")
            print("=" * 70)

            input(
                "\nНажми Enter ТОЛЬКО ПОСЛЕ успешного входа..."
            )

            # Не выполняем page.goto().
            # После входа rabota.by может сам продолжать редиректы.
            print(
                "\nЖдём завершения внутренних переходов rabota.by..."
            )

            page.wait_for_timeout(4000)

            try:
                page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=10000,
                )
            except Exception:
                pass

            current_url = page.url

            print(f"Текущий URL: {current_url}")

            if looks_like_login_page(current_url):
                raise RuntimeError(
                    "Похоже, вход ещё не завершён: "
                    "браузер всё ещё находится на странице авторизации."
                )

            cookies = context.cookies()

            relevant_cookies = [
                cookie
                for cookie in cookies
                if (
                    "rabota.by" in cookie.get("domain", "")
                    or "hh.by" in cookie.get("domain", "")
                    or "hh.ru" in cookie.get("domain", "")
                )
            ]

            print(
                f"Cookies rabota.by/hh в текущей сессии: "
                f"{len(relevant_cookies)}"
            )

            if not relevant_cookies:
                raise RuntimeError(
                    "После входа не найдено cookies rabota.by/hh. "
                    "Авторизацию сохранять небезопасно."
                )

            backup_file = make_backup()

            if backup_file is not None:
                print(
                    "Резервная копия старой авторизации: "
                    f"{backup_file}"
                )

            # Сохраняем cookies + localStorage.
            context.storage_state(
                path=str(AUTH_FILE)
            )

            if (
                not AUTH_FILE.exists()
                or AUTH_FILE.stat().st_size == 0
            ):
                raise RuntimeError(
                    "Не удалось сохранить rabota_auth.json."
                )

            print("\n" + "=" * 70)
            print("ГОТОВО")
            print("=" * 70)
            print("Свежая авторизация сохранена:")
            print(AUTH_FILE)

            context.close()

        finally:
            browser.close()


# ==============================================================
# 2. ЗАПИСЬ ДЕЙСТВИЙ PLAYWRIGHT
# ==============================================================

def record_actions():
    print("=" * 70)
    print("ЗАПИСЬ ДЕЙСТВИЙ rabota.by")
    print("=" * 70)

    print(f"Папка проекта: {BASE_DIR}")
    print(f"Файл авторизации: {AUTH_FILE}")

    if not AUTH_FILE.exists():
        raise FileNotFoundError(
            "Не найден rabota_auth.json:\n"
            f"{AUTH_FILE}\n\n"
            "Сначала выбери пункт 1 и создай/обнови авторизацию."
        )

    RECORDINGS_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    output_file = (
        RECORDINGS_DIR
        / f"training_recording_{timestamp}.py"
    )

    print()
    print("Откроется rabota.by уже с авторизацией этого проекта.")
    print("Playwright Codegen будет записывать все действия.")
    print()
    print("Файл записи:")
    print(output_file)
    print()
    print("Когда закончишь:")
    print("1. Закрой браузер.")
    print("2. Закрой Playwright Inspector.")
    print("3. Пришли мне созданный training_recording_*.py.")
    print()

    command = [
        sys.executable,
        "-m",
        "playwright",
        "codegen",
        "--target=python",
        f"--load-storage={AUTH_FILE}",
        "-o",
        str(output_file),
        RECORD_START_URL,
    ]

    result = subprocess.run(
        command,
        cwd=str(BASE_DIR),
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Playwright Codegen завершился с ошибкой. "
            f"Код: {result.returncode}"
        )

    print("\n" + "=" * 70)
    print("ЗАПИСЬ ЗАВЕРШЕНА")
    print("=" * 70)

    if output_file.exists():
        print("Файл создан:")
        print(output_file)
    else:
        print(
            "Playwright завершился без ошибки, "
            "но файл записи не найден:"
        )
        print(output_file)


# ==============================================================
# МЕНЮ
# ==============================================================

def show_menu():
    print("\n" + "=" * 70)
    print("rabota.by — СЕРВИС ПРОЕКТА")
    print("=" * 70)
    print("1 — Обновление авторизации")
    print("2 — Запись действий в авторизованном браузере")
    print("0 — Выход")
    print("=" * 70)

    return input("Выбери действие: ").strip()


def main():
    while True:
        choice = show_menu()

        if choice == "1":
            refresh_auth()
            return

        if choice == "2":
            record_actions()
            return

        if choice == "0":
            print("Выход.")
            return

        print(
            "\nНеизвестный пункт. "
            "Введи 1, 2 или 0."
        )


# ==============================================================
# ЗАПУСК
# ==============================================================

if __name__ == "__main__":
    try:
        main()

    except Exception:
        print("\n" + "=" * 70)
        print("ОШИБКА")
        print("=" * 70)
        traceback.print_exc()

    print("\n" + "=" * 70)
    input("Нажми Enter, чтобы закрыть окно...")
