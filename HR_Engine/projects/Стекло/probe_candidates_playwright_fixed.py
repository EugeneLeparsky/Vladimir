from pathlib import Path
import traceback
from playwright.sync_api import sync_playwright

# ==============================================================
# НАСТРОЙКИ
# ==============================================================

VACANCY_ID = "136993801"

BASE_DIR = Path(__file__).resolve().parent
AUTH_FILE = BASE_DIR / "rabota_auth.json"

RESPONSES_URL = (
    "https://rabota.by/employer/vacancyresponses"
    f"?vacancyId={VACANCY_ID}"
)

API_URL = (
    "https://rabota.by/shards/employer/vacancyresponses/candidates_list"
    f"?vacancyId={VACANCY_ID}&order=RELEVANCE&limit=50&page=0"
)


# ==============================================================
# ОСНОВНОЙ ТЕСТ
# ==============================================================

def main():
    print("=" * 70)
    print("ТЕСТ rabota.by через Playwright")
    print("=" * 70)

    print(f"Папка скрипта: {BASE_DIR}")
    print(f"Файл авторизации: {AUTH_FILE}")

    if not AUTH_FILE.exists():
        raise FileNotFoundError(
            f"Не найден файл авторизации:\n{AUTH_FILE}\n\n"
            "Положи rabota_auth.json в ту же папку, где находится этот скрипт."
        )

    with sync_playwright() as p:
        print("\nЗапускаем Chromium...")

        browser = p.chromium.launch(
            headless=False
        )

        try:
            context = browser.new_context(
                storage_state=str(AUTH_FILE)
            )

            page = context.new_page()

            print("Открываем обычную страницу откликов...")

            page_response = page.goto(
                RESPONSES_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            if page_response is not None:
                print(
                    f"Страница откликов: HTTP {page_response.status}"
                )
            else:
                print(
                    "Страница откликов открылась, "
                    "но Playwright не вернул HTTP-статус."
                )

            page.wait_for_timeout(3000)

            print(
                "\nЗапрашиваем candidates_list "
                "через авторизованный браузер..."
            )

            response = page.request.get(
                API_URL,
                timeout=60000,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": RESPONSES_URL,
                },
            )

            print(f"API HTTP: {response.status}")

            body = response.text()

            if response.ok:
                data = response.json()

                contact_center = data.get(
                    "contactCenterCandidates",
                    {}
                )

                candidates = contact_center.get(
                    "candidates",
                    []
                )

                resumes = (
                    contact_center
                    .get("rawEnrichingData", {})
                    .get("resumes", {})
                    .get("resumes", [])
                )

                print(f"Кандидатов: {len(candidates)}")
                print(f"Объектов резюме: {len(resumes)}")

                print("\n" + "=" * 70)
                print("ТЕСТ УСПЕШЕН")
                print("=" * 70)

                print(
                    "Авторизация работает, "
                    "candidates_list доступен через Playwright."
                )

            else:
                print("\n" + "=" * 70)
                print("API ВЕРНУЛ ОШИБКУ")
                print("=" * 70)

                print("\nНачало ответа сервера:")
                print("-" * 70)
                print(body[:3000])
                print("-" * 70)

                if response.status in (401, 403):
                    print(
                        "\nПохоже на проблему авторизации "
                        "или прав доступа."
                    )

                elif response.status >= 500:
                    print(
                        "\nЭто серверная ошибка rabota.by. "
                        "Она сама по себе НЕ означает, "
                        "что rabota_auth.json истёк."
                    )

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
