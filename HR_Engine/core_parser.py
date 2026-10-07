import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright


# ============================================================
# НАСТРОЙКИ ПРОЕКТА
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = Path(
    sys.argv[1] if len(sys.argv) > 1 else "config.json"
).resolve()

with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)

PROJECT_DIR = CONFIG_FILE.parent

VACANCIES = {
    item["vacancy_id"]: {
        "name": item["name"],
        "sheet": item["sheet"],
    }
    for item in CONFIG["vacancies"]
}

AUTH_FILE = str(PROJECT_DIR / "rabota_auth.json")

GOOGLE_CREDENTIALS_FILE = str(
    BASE_DIR / "google_credentials.json"
)

SPREADSHEET_ID = CONFIG["spreadsheet_id"]

API_URL = (
    "https://rabota.by/shards/employer/"
    "vacancyresponses/candidates_list"
)

SHOW_BROWSER = CONFIG.get("show_browser", True)

CONTACTS_WAIT_MS = 1500
DELAY_BETWEEN_RESUMES = 0.5

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


# ============================================================
# RABOTA.BY: АВТОРИЗАЦИЯ
# ============================================================

def create_rabota_session():
    auth_path = Path(AUTH_FILE)

    if not auth_path.exists():
        raise FileNotFoundError(
            f"Не найден файл авторизации:\n{AUTH_FILE}\n\n"
            "Создай заново rabota_auth.json в папке проекта."
        )

    with open(auth_path, "r", encoding="utf-8") as file:
        storage = json.load(file)

    session = requests.Session()

    for cookie in storage.get("cookies", []):
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )

    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        }
    )

    return session


# ============================================================
# GOOGLE SHEETS: ПОДКЛЮЧЕНИЕ
# ============================================================

def create_google_service():
    credentials_path = Path(GOOGLE_CREDENTIALS_FILE)

    if not credentials_path.exists():
        raise FileNotFoundError(
            f"Не найден Google-ключ:\n{GOOGLE_CREDENTIALS_FILE}"
        )

    credentials = Credentials.from_service_account_file(
        str(credentials_path),
        scopes=GOOGLE_SCOPES,
    )

    return build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )


def normalize_sheet_name(name):
    return " ".join(
        str(name).replace("\xa0", " ").split()
    ).strip()


def get_sheet_metadata(service):
    response = (
        service.spreadsheets()
        .get(spreadsheetId=SPREADSHEET_ID)
        .execute()
    )

    result = {}

    for sheet in response.get("sheets", []):
        properties = sheet.get("properties", {})
        title = properties.get("title")

        if title:
            result[title] = {
                "sheet_id": properties.get("sheetId"),
                "row_count": properties.get(
                    "gridProperties",
                    {}
                ).get("rowCount", 1000),
            }

    return result


def resolve_real_sheet_names(sheet_metadata):
    normalized_names = {
        normalize_sheet_name(real_name): real_name
        for real_name in sheet_metadata
    }

    for vacancy_config in VACANCIES.values():
        configured_name = vacancy_config["sheet"]

        real_name = normalized_names.get(
            normalize_sheet_name(configured_name)
        )

        if real_name is None:
            available = ", ".join(
                f"'{name}'" for name in sheet_metadata
            )

            raise RuntimeError(
                f"Не найден лист '{configured_name}'.\n"
                f"Доступные листы: {available}"
            )

        vacancy_config["sheet"] = real_name


# ============================================================
# GOOGLE SHEETS: ПОИСК ПОДГОТОВЛЕННЫХ СТРОК
# ============================================================

def get_sheet_state(service, sheet_name, row_count):
    """
    Возвращает:

    existing_ids:
        Все уже записанные ID из столбца Z.

    free_prepared_rows:
        Свободные строки, в которых есть заготовка:
        выпадающий список, чекбокс, форматирование либо
        другие данные в A:Z, но B/C/D/Z ещё пустые.

    last_prepared_or_used_row:
        Последняя строка подготовленной области.
        Если подготовленные строки закончились, добавление
        новых кандидатов начнётся под этой строкой.
    """

    # Читаем значения. Нужны B, C, D и Z.
    values_response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{sheet_name}'!A:Z",
            majorDimension="ROWS",
            valueRenderOption="FORMATTED_VALUE",
        )
        .execute()
    )

    values = values_response.get("values", [])

    existing_ids = set()
    occupied_candidate_rows = set()
    last_value_row = 0

    for row_number, row in enumerate(values, start=1):
        if any(str(value).strip() for value in row):
            last_value_row = row_number

        # A:Z:
        # B = индекс 1
        # C = индекс 2
        # D = индекс 3
        # Z = индекс 25
        name = str(row[1]).strip() if len(row) > 1 else ""
        age = str(row[2]).strip() if len(row) > 2 else ""
        phone = str(row[3]).strip() if len(row) > 3 else ""
        resume_id = str(row[25]).strip() if len(row) > 25 else ""

        if resume_id:
            existing_ids.add(resume_id)

        # Строку нельзя использовать, если в ней уже есть
        # хотя бы одна часть данных кандидата.
        if name or age or phone or resume_id:
            occupied_candidate_rows.add(row_number)

    # Получаем информацию не только о значениях,
    # но и о выпадающих списках, чекбоксах и форматировании.
    #
    # Благодаря этому строка с пустыми B/C/D/Z,
    # но с подготовленными селекторами, считается
    # именно подготовленной строкой.
    metadata_response = (
        service.spreadsheets()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            ranges=[
                f"'{sheet_name}'!A1:Z{row_count}"
            ],
            includeGridData=True,
            fields=(
                "sheets("
                "data("
                "startRow,"
                "rowData("
                "values("
                "userEnteredValue,"
                "effectiveValue,"
                "dataValidation,"
                "userEnteredFormat"
                ")"
                ")"
                ")"
                ")"
            ),
        )
        .execute()
    )

    prepared_rows = set()

    sheets = metadata_response.get("sheets", [])

    for sheet in sheets:
        for grid_data in sheet.get("data", []):
            start_row = grid_data.get("startRow", 0)

            for index, row_data in enumerate(
                grid_data.get("rowData", [])
            ):
                row_number = start_row + index + 1
                cells = row_data.get("values", [])

                is_prepared = False

                for cell in cells:
                    # Выпадающий список / чекбокс.
                    if cell.get("dataValidation"):
                        is_prepared = True
                        break

                    # Значения, формулы, чекбоксы и т. д.
                    if cell.get("userEnteredValue"):
                        is_prepared = True
                        break

                    if cell.get("effectiveValue"):
                        is_prepared = True
                        break

                    # Подготовленное форматирование.
                    if cell.get("userEnteredFormat"):
                        is_prepared = True
                        break

                if is_prepared:
                    prepared_rows.add(row_number)

    # Первая строка чаще всего шапка.
    # Поэтому никогда не вставляем кандидатов в строку 1.
    prepared_rows.discard(1)

    # Свободные подготовленные строки:
    # они имеют шаблон, но не содержат данных кандидата.
    free_prepared_rows = sorted(
        row_number
        for row_number in prepared_rows
        if row_number not in occupied_candidate_rows
    )

    # Если в таблице есть заготовки до 221 строки,
    # то новые записи после их окончания будут добавляться
    # начиная с 222, а не под последней случайной записью.
    last_prepared_or_used_row = max(
        max(prepared_rows, default=0),
        last_value_row,
        1,
    )

    return {
        "existing_ids": existing_ids,
        "free_prepared_rows": free_prepared_rows,
        "last_prepared_or_used_row": (
            last_prepared_or_used_row
        ),
    }


def write_candidate_to_sheet(
    service,
    sheet_id,
    row_number,
    candidate,
):
    """
    Записывает одного кандидата именно в нужную строку.

    B — ФИО с гиперссылкой.
    C — возраст.
    D — телефон.
    Z — ID резюме.
    """

    name = str(candidate["name"]).replace('"', '""')
    resume_url = str(candidate["resume_url"]).replace('"', '""')

    # В русской локали Google Sheets используется ;
    # внутри формулы HYPERLINK.
    hyperlink_formula = (
        f'=HYPERLINK("{resume_url}";"{name}")'
    )

    row_index = row_number - 1

    requests_body = [
        {
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                    "startColumnIndex": 1,
                    "endColumnIndex": 4,
                },
                "rows": [
                    {
                        "values": [
                            {
                                "userEnteredValue": {
                                    "formulaValue": hyperlink_formula
                                }
                            },
                            {
                                "userEnteredValue": {
                                    "stringValue": str(
                                        candidate["age"]
                                    )
                                }
                            },
                            {
                                "userEnteredValue": {
                                    "stringValue": str(
                                        candidate["phone"]
                                    )
                                }
                            },
                        ]
                    }
                ],
                "fields": "userEnteredValue",
            }
        },
        {
            "updateCells": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_index,
                    "endRowIndex": row_index + 1,
                    "startColumnIndex": 25,
                    "endColumnIndex": 26,
                },
                "rows": [
                    {
                        "values": [
                            {
                                "userEnteredValue": {
                                    "stringValue": str(
                                        candidate["resume_id"]
                                    )
                                }
                            }
                        ]
                    }
                ],
                "fields": "userEnteredValue",
            }
        },
    ]

    (
        service.spreadsheets()
        .batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": requests_body},
        )
        .execute()
    )


# ============================================================
# RABOTA.BY: ПОЛУЧЕНИЕ СПИСКА ОТКЛИКОВ
# ============================================================

def get_resume_hash(candidate):
    try:
        default_link = (
            candidate["negotiationLinks"]
            ["changeTopic"]
            ["defaultLink"]
        )

        params = parse_qs(
            urlparse(default_link).query
        )

        return params.get("r", [None])[0]

    except (KeyError, TypeError):
        return None


def get_candidates_for_vacancy(session, vacancy_id):
    all_candidates = []
    page_number = 0

    while True:
        response = session.get(
            API_URL,
            params={
                "vacancyId": vacancy_id,
                "order": "RELEVANCE",
                "limit": 50,
                "page": page_number,
            },
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        candidates = (
            data.get("contactCenterCandidates", {})
            .get("candidates", [])
        )

        print(
            f"  Страница API {page_number + 1}: "
            f"кандидатов — {len(candidates)}"
        )

        if not candidates:
            break

        all_candidates.extend(candidates)
        page_number += 1

    return all_candidates


def collect_new_resume_rows(
    session,
    vacancy_id,
    vacancy_name,
    existing_ids,
):
    candidates = get_candidates_for_vacancy(
        session,
        vacancy_id,
    )

    rows = []
    seen_in_current_run = set()

    skipped_existing = 0
    skipped_not_new = 0
    skipped_without_link = 0

    for candidate in candidates:
        resume_id = candidate.get("resumeId")

        if not resume_id:
            skipped_without_link += 1
            continue

        resume_id = str(resume_id).strip()

        if resume_id in existing_ids:
            skipped_existing += 1
            continue

        if resume_id in seen_in_current_run:
            skipped_existing += 1
            continue

        if candidate.get("employerState") != "RESPONSE":
            skipped_not_new += 1
            continue

        if candidate.get("hasNewMessages") is not True:
            skipped_not_new += 1
            continue

        resume_hash = get_resume_hash(candidate)

        if not resume_hash:
            skipped_without_link += 1
            continue

        seen_in_current_run.add(resume_id)

        rows.append(
            {
                "vacancy_id": vacancy_id,
                "vacancy_name": vacancy_name,
                "resume_id": resume_id,
                "resume_url": (
                    f"https://rabota.by/resume/{resume_hash}"
                    f"?vacancyId={vacancy_id}"
                ),
            }
        )

    print(f"  Уже есть в столбце Z: {skipped_existing}")
    print(
        "  Пропущены по статусу/прочитанности: "
        f"{skipped_not_new}"
    )
    print(f"  Без ID или ссылки: {skipped_without_link}")
    print(f"  Новых резюме: {len(rows)}")

    return rows


# ============================================================
# RABOTA.BY: ФИО, ВОЗРАСТ, ТЕЛЕФОН
# ============================================================

def extract_resume_contacts(page, row, debug_saved):
    result = {
        **row,
        "name": "",
        "age": "",
        "phone": "",
        "error": "",
    }

    try:
        page.goto(
            row["resume_url"],
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        name_block = page.locator(
            '[data-qa="resume-main-info__header"]'
        )

        try:
            name_block.first.wait_for(
                state="visible",
                timeout=20_000,
            )
        except Exception:
            pass

        show_contacts = page.locator("a").filter(
            has_text="Показать все контакты"
        )

        if show_contacts.count() > 0:
            try:
                show_contacts.first.click(timeout=5_000)
                page.wait_for_timeout(CONTACTS_WAIT_MS)
            except Exception:
                pass

        if name_block.count() > 0:
            result["name"] = (
                name_block.first.inner_text().strip()
            )

        full_text = page.locator("body").inner_text()

        age_match = re.search(
            r"(?<!\d)(\d{1,2})\s+"
            r"(?:лет|года|год)\b",
            full_text,
            flags=re.IGNORECASE,
        )

        if age_match:
            result["age"] = age_match.group(1)

        phone_hrefs = page.eval_on_selector_all(
            "a[href^='tel:']",
            """
            elements => elements.map(
                element => element.getAttribute("href")
            )
            """,
        )

        for href in phone_hrefs:
            if href:
                phone = href.replace("tel:", "").strip()

                if phone:
                    result["phone"] = phone
                    break

        missing = []

        if not result["name"]:
            missing.append("ФИО")

        if not result["age"]:
            missing.append("возраст")

        if not result["phone"]:
            missing.append("телефон")

        if missing:
            result["error"] = (
                "Не найдено: " + ", ".join(missing)
            )

            if not debug_saved[0]:
                try:
                    page.screenshot(
                        path="debug_resume.png",
                        full_page=True,
                    )

                    with open(
                        "debug_resume.html",
                        "w",
                        encoding="utf-8",
                    ) as file:
                        file.write(page.content())

                    print(
                        "    Созданы debug_resume.png "
                        "и debug_resume.html"
                    )

                    debug_saved[0] = True

                except Exception:
                    pass

    except Exception as error:
        result["error"] = (
            f"{type(error).__name__}: {error}"
        )

    return result


# ============================================================
# ОБРАБОТКА ВАКАНСИИ
# ============================================================

def process_vacancy(
    service,
    sheet_id,
    row_count,
    session,
    vacancy_id,
    vacancy_config,
    page,
    debug_saved,
):
    vacancy_name = vacancy_config["name"]
    sheet_name = vacancy_config["sheet"]

    print("\n" + "=" * 70)
    print(f"Вакансия: {vacancy_name}")
    print(f"Лист: {sheet_name}")
    print(f"ID вакансии: {vacancy_id}")

    sheet_state = get_sheet_state(
        service=service,
        sheet_name=sheet_name,
        row_count=row_count,
    )

    existing_ids = sheet_state["existing_ids"]
    free_prepared_rows = sheet_state["free_prepared_rows"]
    last_prepared_or_used_row = (
        sheet_state["last_prepared_or_used_row"]
    )

    print(
        f"  ID уже найдено в Z: {len(existing_ids)}"
    )
    print(
        "  Свободных подготовленных строк: "
        f"{len(free_prepared_rows)}"
    )
    print(
        "  Конец подготовленной/используемой области: "
        f"{last_prepared_or_used_row}"
    )

    candidate_rows = collect_new_resume_rows(
        session=session,
        vacancy_id=vacancy_id,
        vacancy_name=vacancy_name,
        existing_ids=existing_ids,
    )

    if not candidate_rows:
        print("  Новых данных для записи нет.")
        return 0

    written = 0
    next_new_row = last_prepared_or_used_row + 1

    for index, row in enumerate(candidate_rows, start=1):
        # Сначала заполняем подготовленные свободные строки.
        if free_prepared_rows:
            target_row = free_prepared_rows.pop(0)
            row_type = "подготовленная строка"
        else:
            # Если заготовки закончились — пишем ниже них.
            target_row = next_new_row
            next_new_row += 1
            row_type = "новая строка ниже заготовок"

        print(
            f"  {index}/{len(candidate_rows)}. "
            f"ID {row['resume_id']} → строка {target_row} "
            f"({row_type})"
        )

        result = extract_resume_contacts(
            page=page,
            row=row,
            debug_saved=debug_saved,
        )

        if result["error"]:
            print(f"    ! {result['error']}")
        else:
            print(
                f"    ✓ {result['name']} | "
                f"{result['age']} | {result['phone']}"
            )

        # Пишем сразу после обработки кандидата.
        # При аварийном завершении уже записанные люди не потеряются.
        write_candidate_to_sheet(
            service=service,
            sheet_id=sheet_id,
            row_number=target_row,
            candidate=result,
        )

        written += 1

        time.sleep(DELAY_BETWEEN_RESUMES)

    print(f"  Записано кандидатов: {written}")

    return written


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    print(f"Конфигурация: {CONFIG_FILE}")
    print("Подключаемся к Google Sheets...")

    google_service = create_google_service()

    print("Проверяем листы Google Sheets...")

    sheet_metadata = get_sheet_metadata(google_service)

    resolve_real_sheet_names(sheet_metadata)

    print("Листы для работы:")

    for vacancy_config in VACANCIES.values():
        print(
            f"  {vacancy_config['name']} → "
            f"'{vacancy_config['sheet']}'"
        )

    print("Подключаемся к rabota.by...")

    rabota_session = create_rabota_session()

    total_written = 0
    debug_saved = [False]

    print("Запускаем браузер...")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not SHOW_BROWSER,
        )

        context = browser.new_context(
            storage_state=AUTH_FILE,
        )

        page = context.new_page()

        for vacancy_id, vacancy_config in VACANCIES.items():
            sheet_name = vacancy_config["sheet"]
            metadata = sheet_metadata[sheet_name]

            written = process_vacancy(
                service=google_service,
                sheet_id=metadata["sheet_id"],
                row_count=metadata["row_count"],
                session=rabota_session,
                vacancy_id=vacancy_id,
                vacancy_config=vacancy_config,
                page=page,
                debug_saved=debug_saved,
            )

            total_written += written

        context.close()
        browser.close()

    print("\n" + "=" * 70)
    print("ГОТОВО")
    print(f"Всего добавлено новых кандидатов: {total_written}")
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()

    except requests.HTTPError as error:
        print("\nОШИБКА HTTP:")
        print(error)
        print(
            "\nВероятно, истекла авторизация rabota.by. "
            "Нужно заново создать rabota_auth.json."
        )

    except FileNotFoundError as error:
        print(f"\nОШИБКА ФАЙЛА:\n{error}")

    except Exception as error:
        print(
            "\nНЕОЖИДАННАЯ ОШИБКА:\n"
            f"{type(error).__name__}: {error}"
        )
