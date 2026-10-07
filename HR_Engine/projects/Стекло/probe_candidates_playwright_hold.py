from pathlib import Path
import traceback
from playwright.sync_api import sync_playwright

VACANCY_ID = "136993801"
AUTH_FILE = "rabota_auth.json"

RESPONSES_URL = (
    "https://rabota.by/employer/vacancyresponses"
    f"?vacancyId={VACANCY_ID}"
)

API_URL = (
    "https://rabota.by/shards/employer/vacancyresponses/candidates_list"
    f"?vacancyId={VACANCY_ID}&order=RELEVANCE&limit=50&page=0"
)

def main():
    auth_path = Path(AUTH_FILE)

    print("=" * 70)
    print("ТЕСТ rabota.by через Playwright")
    print("=" * 70)
    print(f"Текущая папка: {Path.cwd()}")
    print(f"Ищем авторизацию: {auth_path.resolve()}")

    if not auth_path.exists():
        raise FileNotFoundError(
            f"Не найден {AUTH_FILE}. "
            "Положи probe_candidates_playwright_hold.py рядом с rabota_auth.json."
        )

    with sync_playwright() as p:
        print("\nЗапускаем Chromium...")
        browser = p.chromium.launch(headless=False)

        try:
            context = browser.new_context(storage_state=str(auth_path))
            page = context.new_page()

            print("Открываем обычную страницу откликов...")
            page_response = page.goto(
                RESPONSES_URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            if page_response:
                print(f"Страница откликов: HTTP {page_response.status}")
            else:
                print("Страница открыта, но HTTP-статус не получен.")

            page.wait_for_timeout(3000)

            print("\nЗапрашиваем candidates_list через page.request...")
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

                contact_center = data.get("contactCenterCandidates", {})

                candidates = contact_center.get("candidates", [])
                resumes = (
                    contact_center
                    .get("rawEnrichingData", {})
                    .get("resumes", {})
                    .get("resumes", [])
                )

                print(f"Кандидатов: {len(candidates)}")
                print(f"Объектов резюме: {len(resumes)}")
                print("\nТЕСТ УСПЕШЕН.")
            else:
                print("\nAPI вернул ошибку.")
                print("Начало тела ответа:")
                print("-" * 70)
                print(body[:3000])
                print("-" * 70)

                if response.status in (401, 403):
                    print("Похоже на проблему авторизации или прав доступа.")
                elif response.status >= 500:
                    print(
                        "Это серверная ошибка rabota.by. "
                        "Она не доказывает, что rabota_auth.json истёк."
                    )

            context.close()

        finally:
            browser.close()

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n" + "=" * 70)
        print("ОШИБКА")
        print("=" * 70)
        traceback.print_exc()

    print("\n" + "=" * 70)
    input("Нажми Enter, чтобы закрыть это окно...")
