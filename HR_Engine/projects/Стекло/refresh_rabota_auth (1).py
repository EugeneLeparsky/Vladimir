from pathlib import Path
import shutil
import traceback
from datetime import datetime

from playwright.sync_api import sync_playwright

# ==============================================================
# ПУТИ
# ==============================================================

BASE_DIR = Path(__file__).resolve().parent
AUTH_FILE = BASE_DIR / "rabota_auth.json"
BACKUP_DIR = BASE_DIR / "auth_backups"

LOGIN_URL = (
    "https://rabota.by/account/login"
    "?backurl=%2Femployer%2Fvacancies"
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
# ОБНОВЛЕНИЕ АВТОРИЗАЦИИ
# ==============================================================

def main():
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
        print("\nrabota_auth.json пока отсутствует — будет создан новый.")

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

            input("\nНажми Enter ТОЛЬКО ПОСЛЕ успешного входа...")

            # Не выполняем page.goto().
            # После входа rabota.by может сам продолжать редиректы.
            print("\nЖдём завершения внутренних переходов rabota.by...")

            page.wait_for_timeout(4000)

            try:
                page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=10000,
                )
            except Exception:
                # Это не критично: SPA/фоновые переходы могут не дать
                # стабильного события загрузки.
                pass

            current_url = page.url

            print(f"Текущий URL: {current_url}")

            if looks_like_login_page(current_url):
                raise RuntimeError(
                    "Похоже, вход ещё не завершён: "
                    "браузер всё ещё находится на странице авторизации."
                )

            # Дополнительная мягкая проверка:
            # смотрим, есть ли cookies для rabota.by/hh.
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
                print(f"Резервная копия старой авторизации: {backup_file}")

            # Сохраняем cookies + localStorage.
            context.storage_state(
                path=str(AUTH_FILE)
            )

            if not AUTH_FILE.exists() or AUTH_FILE.stat().st_size == 0:
                raise RuntimeError(
                    "Не удалось сохранить rabota_auth.json."
                )

            print("\n" + "=" * 70)
            print("ГОТОВО")
            print("=" * 70)
            print("Свежая авторизация сохранена:")
            print(AUTH_FILE)
            print("\nТеперь можно запускать основной скрипт проекта.")

            context.close()

        finally:
            browser.close()


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
