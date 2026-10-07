import json
import re
import sys
import traceback
from pathlib import Path

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from playwright.sync_api import sync_playwright


# ============================================================
# ПУТИ И КОНФИГУРАЦИЯ
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = Path(
    sys.argv[1] if len(sys.argv) > 1 else "config.json"
).resolve()

with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)

PROJECT_DIR = CONFIG_FILE.parent

AUTH_FILE = PROJECT_DIR / "rabota_auth.json"
GOOGLE_CREDENTIALS_FILE = BASE_DIR / "google_credentials.json"

SPREADSHEET_ID = CONFIG["spreadsheet_id"]
VACANCIES = CONFIG.get("vacancies", [])

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

POSITIVE_VALUES = {
    "1",
    "2",
    "3",
    "подходит",
}

NEGATIVE_VALUES = {
    "не подходит",
    "неподходит",
}

PRIMARY_MODES = {
    "первичный",
    "первичный контакт",
}

CONSIDER_MODES = {
    "подумать",
}


# ============================================================
# ОБЩИЕ ФУНКЦИИ
# ============================================================

def normalize_text(value):
    return " ".join(
        str(value or "")
        .replace("\xa0", " ")
        .strip()
        .lower()
        .split()
    )


def normalize_decision(value):
    value = normalize_text(value)

    if re.fullmatch(r"[123][\.\)]?", value):
        return value[0]

    return value


def normalize_sheet_name(value):
    return " ".join(
        str(value or "")
        .replace("\xa0", " ")
        .split()
    ).strip()


def parse_hyperlink_formula(formula):
    formula = str(formula or "").strip()

    # =HYPERLINK("https://...";"Имя")
    # =HYPERLINK("https://...","Имя")
    match = re.search(
        r'HYPERLINK\(\s*"([^"]+)"',
        formula,
        flags=re.IGNORECASE,
    )

    if match:
        return match.group(1).strip()

    # Иногда сама ячейка может содержать обычную ссылку.
    if formula.startswith("http://") or formula.startswith("https://"):
        return formula

    return None


# ============================================================
# GOOGLE SHEETS
# ============================================================

def create_google_service():
    if not GOOGLE_CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            "Не найден Google-ключ:\n"
            f"{GOOGLE_CREDENTIALS_FILE}"
        )

    credentials = Credentials.from_service_account_file(
        str(GOOGLE_CREDENTIALS_FILE),
        scopes=GOOGLE_SCOPES,
    )

    return build(
        "sheets",
        "v4",
        credentials=credentials,
        cache_discovery=False,
    )


def get_real_sheet_titles(service):
    response = (
        service.spreadsheets()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            fields="sheets(properties(title))",
        )
        .execute()
    )

    return [
        item["properties"]["title"]
        for item in response.get("sheets", [])
    ]


def resolve_sheet_name(configured_name, real_titles):
    normalized = normalize_sheet_name(configured_name)

    lookup = {
        normalize_sheet_name(title): title
        for title in real_titles
    }

    return lookup.get(normalized)


def get_values(
    service,
    sheet_name,
    range_a1,
    render_option="FORMATTED_VALUE",
):
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{sheet_name}'!{range_a1}",
            valueRenderOption=render_option,
        )
        .execute()
    )

    return response.get("values", [])


def cell(row, index):
    if index < len(row):
        return row[index]

    return ""


def load_sheet_queue(service, sheet_name):
    values = get_values(
        service,
        sheet_name,
        "A1:E",
        render_option="FORMATTED_VALUE",
    )

    formulas_b = get_values(
        service,
        sheet_name,
        "B1:B",
        render_option="FORMULA",
    )

    if not values:
        return {
            "mode": "",
            "rows": [],
        }

    mode_raw = cell(values[0], 4)
    mode = normalize_text(mode_raw)

    result = []

    for row_number, row in enumerate(
        values[1:],
        start=2,
    ):
        decision_raw = cell(row, 0)
        name = str(cell(row, 1) or "").strip()
        processed = str(cell(row, 4) or "").strip()

        # E уже занята -> никогда не трогаем строку.
        if processed:
            continue

        decision = normalize_decision(decision_raw)

        # A пустая -> ничего не делаем.
        if not decision:
            continue

        if decision in POSITIVE_VALUES:
            if mode in CONSIDER_MODES:
                action = "consider"
                action_label = "Подумать"

            elif mode in PRIMARY_MODES:
                action = "primary"
                action_label = "Первичный"

            else:
                print(
                    f"[{sheet_name} / строка {row_number}] "
                    f"неизвестное значение E1={mode_raw!r}; "
                    "положительное решение пропущено."
                )
                continue

            sheet_result = "Первичный"

        elif decision in NEGATIVE_VALUES:
            action = "reject"
            action_label = "Отказ"
            sheet_result = "Наш отказ"

        else:
            print(
                f"[{sheet_name} / строка {row_number}] "
                f"неизвестное значение A={decision_raw!r}; пропуск."
            )
            continue

        formula_index = row_number - 1

        formula = (
            cell(formulas_b[formula_index], 0)
            if formula_index < len(formulas_b)
            else ""
        )

        resume_url = parse_hyperlink_formula(formula)

        if not resume_url:
            print(
                f"[{sheet_name} / строка {row_number}] "
                "не удалось получить ссылку из B; пропуск."
            )
            continue

        result.append(
            {
                "sheet": sheet_name,
                "row": row_number,
                "name": name or f"строка {row_number}",
                "decision": str(decision_raw).strip(),
                "resume_url": resume_url,
                "action": action,
                "action_label": action_label,
                "sheet_result": sheet_result,
            }
        )

    return {
        "mode": mode,
        "rows": result,
    }


def write_e(
    service,
    sheet_name,
    row_number,
    value,
):
    (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{sheet_name}'!E{row_number}",
            valueInputOption="USER_ENTERED",
            body={
                "values": [[value]],
            },
        )
        .execute()
    )


# ============================================================
# PLAYWRIGHT: ДЕЙСТВИЯ НА rabota.by
# ============================================================

def wait_short(page):
    page.wait_for_timeout(700)


def perform_consider(page):
    # По записанному обучению:
    # Пригласить -> Статус -> Подумать -> Изменить статус
    page.get_by_role(
        "button",
        name="Пригласить",
        exact=True,
    ).click()

    wait_short(page)

    page.get_by_role(
        "combobox",
        name="Статус",
    ).click()

    wait_short(page)

    page.get_by_role(
        "listbox",
    ).get_by_text(
        "Подумать",
        exact=True,
    ).click()

    wait_short(page)

    page.get_by_role(
        "button",
        name="Изменить статус",
        exact=True,
    ).click()

    wait_short(page)


def perform_primary(page):
    # По той же схеме, что и "Подумать":
    # Пригласить -> Статус -> Первичный контакт -> Изменить статус
    page.get_by_role(
        "button",
        name="Пригласить",
        exact=True,
    ).click()

    wait_short(page)

    page.get_by_role(
        "combobox",
        name="Статус",
    ).click()

    wait_short(page)

    listbox = page.get_by_role("listbox")

    # Сначала точное штатное название стадии.
    option = listbox.get_by_text(
        "Первичный контакт",
        exact=True,
    )

    if option.count() > 0:
        option.first.click()
    else:
        # Резерв на случай сокращённой подписи "Первичный".
        listbox.get_by_text(
            re.compile(r"^Первич", re.IGNORECASE)
        ).first.click()

    wait_short(page)

    page.get_by_role(
        "button",
        name="Изменить статус",
        exact=True,
    ).click()

    wait_short(page)


def perform_reject(page):
    # По записанному обучению:
    # Отказать -> Не подходит -> Изменить статус
    page.get_by_role(
        "button",
        name="Отказать",
        exact=True,
    ).click()

    wait_short(page)

    page.get_by_role(
        "button",
        name="Не подходит",
        exact=True,
    ).click()

    wait_short(page)

    page.get_by_role(
        "button",
        name="Изменить статус",
        exact=True,
    ).click()

    wait_short(page)


def has_existing_rejection(page):
    """
    Проверяет, был ли кандидат уже отклонён ранее.

    Логика по записанному обучению:
    - если на странице уже есть кнопка "Изменить статус",
      кандидат ранее уже проходил обработку;
    - открываем "Показать всю историю";
    - ищем в раскрытой истории явный статус "Отказ"
      или "Не подходит".

    Возвращает True только при явном обнаружении отказа.
    """
    change_status = page.get_by_role(
        "button",
        name="Изменить статус",
        exact=True,
    )

    if change_status.count() == 0:
        return False

    print(
        "  На странице уже есть «Изменить статус» — "
        "проверяем историю..."
    )

    show_history = page.get_by_role(
        "button",
        name="Показать всю историю",
        exact=True,
    )

    if show_history.count() > 0:
        show_history.first.click()
        page.wait_for_timeout(800)
    else:
        print(
            "  Кнопка «Показать всю историю» не найдена. "
            "Автоматически считать кандидата отклонённым нельзя."
        )
        return False

    # Считываем именно текст страницы после раскрытия истории.
    history_text = (
        page.locator("body")
        .inner_text(timeout=10000)
        .replace("\xa0", " ")
    )

    normalized = "\n".join(
        line.strip()
        for line in history_text.splitlines()
        if line.strip()
    )

    # "Отказ" ищем отдельным словом, чтобы не спутать
    # с кнопкой "Отказать".
    refusal_word = re.search(
        r"(?<![А-Яа-яЁё])Отказ(?![А-Яа-яЁё])",
        normalized,
        flags=re.IGNORECASE,
    )

    not_suitable = re.search(
        r"(?<![А-Яа-яЁё])Не\s+подходит(?![А-Яа-яЁё])",
        normalized,
        flags=re.IGNORECASE,
    )

    if refusal_word or not_suitable:
        print(
            "  В истории найден ранее установленный отказ."
        )
        return True

    print(
        "  В истории явный отказ не найден."
    )
    return False


def perform_action(page, item):
    print(
        f"  Открываем: {item['resume_url']}"
    )

    page.goto(
        item["resume_url"],
        wait_until="domcontentloaded",
        timeout=60000,
    )

    page.wait_for_timeout(1200)

    # Сначала проверяем, не был ли кандидат уже отклонён.
    # Если был, никакого повторного действия на rabota.by не делаем.
    if has_existing_rejection(page):
        return {
            "result": "already_rejected",
            "sheet_value": "Наш отказ",
        }

    if item["action"] == "primary":
        perform_primary(page)
        return {
            "result": "performed",
            "sheet_value": item["sheet_result"],
        }

    if item["action"] == "consider":
        perform_consider(page)
        return {
            "result": "performed",
            "sheet_value": item["sheet_result"],
        }

    if item["action"] == "reject":
        perform_reject(page)
        return {
            "result": "performed",
            "sheet_value": item["sheet_result"],
        }

    raise RuntimeError(
        f"Неизвестное действие: {item['action']}"
    )


# ============================================================
# ПЛАН
# ============================================================

def build_plan(service):
    real_titles = get_real_sheet_titles(service)

    plan = []

    for vacancy in VACANCIES:
        configured_sheet = vacancy.get("sheet", "")

        sheet_name = resolve_sheet_name(
            configured_sheet,
            real_titles,
        )

        if not sheet_name:
            print(
                f"Лист {configured_sheet!r} не найден -> пропуск."
            )
            continue

        queue = load_sheet_queue(
            service,
            sheet_name,
        )

        print(
            f"\nЛист {sheet_name!r}: "
            f"E1={queue['mode']!r}; "
            f"к обработке={len(queue['rows'])}"
        )

        plan.extend(queue["rows"])

    return plan


def confirm_item(item):
    print("\n" + "-" * 70)
    print(
        f"{item['sheet']} / строка {item['row']}"
    )
    print(
        f"Кандидат: {item['name']}"
    )
    print(
        f"A: {item['decision']!r}"
    )
    print(
        f"Действие на rabota.by: {item['action_label']}"
    )
    print(
        f"После успеха E: {item['sheet_result']!r}"
    )

    answer = input(
        "Выполнить? [д/н]: "
    ).strip().lower()

    return answer in {
        "д",
        "да",
        "y",
        "yes",
    }


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 70)
    print("HR ENGINE — РАЗБОР ОТКЛИКОВ ЧЕРЕЗ PLAYWRIGHT")
    print("=" * 70)
    print(f"Проект: {PROJECT_DIR}")
    print(f"Авторизация: {AUTH_FILE}")
    print()

    if not AUTH_FILE.exists():
        raise FileNotFoundError(
            "Не найден rabota_auth.json:\n"
            f"{AUTH_FILE}"
        )

    service = create_google_service()

    print("Читаем таблицу...")
    plan = build_plan(service)

    print("\n" + "=" * 70)
    print(f"Всего строк к рассмотрению: {len(plan)}")
    print("=" * 70)

    if not plan:
        print("Нечего обрабатывать.")
        return

    stats = {
        "done": 0,
        "skipped": 0,
        "errors": 0,
    }

    with sync_playwright() as p:
        print("\nОткрываем авторизованный браузер rabota.by...")

        browser = p.chromium.launch(
            headless=False
        )

        try:
            context = browser.new_context(
                storage_state=str(AUTH_FILE)
            )

            page = context.new_page()

            # Проверяем, что состояние действительно загружается.
            page.goto(
                "https://rabota.by/",
                wait_until="domcontentloaded",
                timeout=60000,
            )

            page.wait_for_timeout(1000)

            for item in plan:
                if not confirm_item(item):
                    print("  Пропущено пользователем.")
                    stats["skipped"] += 1
                    continue

                try:
                    action_result = perform_action(
                        page,
                        item,
                    )

                    value_for_e = action_result["sheet_value"]

                    # В E пишем только после успешного сценария
                    # либо после подтверждённого существующего отказа.
                    write_e(
                        service=service,
                        sheet_name=item["sheet"],
                        row_number=item["row"],
                        value=value_for_e,
                    )

                    if action_result["result"] == "already_rejected":
                        print(
                            "  Повторное действие не требуется: "
                            "отказ уже был зафиксирован на rabota.by."
                        )

                    print(
                        f"  ГОТОВО: E{item['row']} = "
                        f"{value_for_e!r}"
                    )

                    stats["done"] += 1

                except Exception as error:
                    stats["errors"] += 1

                    print(
                        "  ОШИБКА: "
                        f"{type(error).__name__}: {error}"
                    )
                    print(
                        "  Столбец E НЕ изменён. "
                        "Строку можно обработать повторно."
                    )

            context.close()

        finally:
            browser.close()

    print("\n" + "=" * 70)
    print("ЗАВЕРШЕНО")
    print(f"Успешно: {stats['done']}")
    print(f"Пропущено: {stats['skipped']}")
    print(f"Ошибок: {stats['errors']}")
    print("=" * 70)


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
