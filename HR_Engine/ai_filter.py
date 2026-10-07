import json
import sys
import time
from pathlib import Path

import requests
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# ============================================================
# БАЗОВАЯ КОНФИГУРАЦИЯ
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

CONFIG_FILE = Path(
    sys.argv[1] if len(sys.argv) > 1 else "config.json"
).resolve()

with open(CONFIG_FILE, "r", encoding="utf-8") as file:
    CONFIG = json.load(file)

PROJECT_DIR = CONFIG_FILE.parent

GOOGLE_CREDENTIALS_FILE = BASE_DIR / "google_credentials.json"
SPREADSHEET_ID = CONFIG["spreadsheet_id"]

AI_CONFIG = CONFIG.get("ai", {})
OLLAMA_MODEL = AI_CONFIG.get("model", "qwen3:8b")
OLLAMA_URL = AI_CONFIG.get(
    "ollama_url",
    "http://127.0.0.1:11434/api/chat",
)

AI_SHEETS = AI_CONFIG.get("sheets", [])

# Минимальный интервал между ОПЕРАЦИЯМИ ЗАПИСИ в Google Sheets.
# 1.1 сек. ≈ не более 54 write-запросов в минуту.
GOOGLE_WRITE_INTERVAL = float(
    AI_CONFIG.get("google_write_interval", 1.1)
)

# Если Google всё же вернул 429, ждём сброса минутного окна
# и повторяем запись.
GOOGLE_429_WAIT_SECONDS = int(
    AI_CONFIG.get("google_429_wait_seconds", 65)
)

_last_google_write_at = 0.0

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]


# ============================================================
# ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def normalize_column_name(value):
    column = str(value).strip().upper()

    if not column or not column.isalpha():
        raise ValueError(
            f"Некорректное имя столбца: {value!r}. "
            "Ожидается A, B, C, ... AA и т. д."
        )

    return column


def normalize_sheet_name(name):
    """
    Нормализует имя листа:
    - заменяет неразрывные пробелы на обычные;
    - схлопывает повторные пробелы;
    - убирает пробелы по краям.
    """
    return " ".join(
        str(name).replace("\xa0", " ").split()
    ).strip()


def normalize_sheet_config(raw):
    required = [
        "sheet",
        "prompt_file",
        "result_column",
        "text_column",
        "number_column",
    ]

    missing = [
        key for key in required
        if not str(raw.get(key, "")).strip()
    ]

    if missing:
        raise RuntimeError(
            "В конфигурации AI-листа отсутствуют поля: "
            + ", ".join(missing)
        )

    return {
        "sheet": str(raw["sheet"]).strip(),
        "prompt_file": str(raw["prompt_file"]).strip(),
        "result_column": normalize_column_name(
            raw["result_column"]
        ),
        "text_column": normalize_column_name(
            raw["text_column"]
        ),
        "number_column": normalize_column_name(
            raw["number_column"]
        ),
        "processed_column": normalize_column_name(
            raw.get("processed_column", "Y")
        ),
        "processing_version": str(
            raw.get("processing_version", "1")
        ).strip(),
        "start_row": int(raw.get("start_row", 2)),
        "match_value": str(
            raw.get("match_value", "К")
        ),
        "unknown_value": str(
            raw.get("unknown_value", "П")
        ),
        "on_error_value": str(
            raw.get("on_error_value", "П")
        ),
        "no_match_action": str(
            raw.get("no_match_action", "keep")
        ).strip().lower(),
        "no_match_value": str(
            raw.get("no_match_value", "Неподходит")
        ).strip(),
        "delay_seconds": float(
            raw.get("delay_seconds", 0.2)
        ),
    }


def wait_for_google_write_slot():
    global _last_google_write_at

    elapsed = time.monotonic() - _last_google_write_at
    wait_time = GOOGLE_WRITE_INTERVAL - elapsed

    if wait_time > 0:
        time.sleep(wait_time)


def execute_google_write(request_factory):
    """
    Выполняет одну операцию записи в Google Sheets с ограничением
    частоты. При 429 ждёт сброса минутного лимита и повторяет запрос.
    """
    global _last_google_write_at

    attempts = 0

    while True:
        attempts += 1
        wait_for_google_write_slot()

        try:
            result = request_factory().execute()
            _last_google_write_at = time.monotonic()
            return result

        except HttpError as error:
            _last_google_write_at = time.monotonic()

            status = getattr(error.resp, "status", None)

            if status == 429 and attempts < 4:
                print(
                    "  Google Sheets: достигнут лимит записи (429). "
                    f"Ждём {GOOGLE_429_WAIT_SECONDS} сек. и повторяем..."
                )
                time.sleep(GOOGLE_429_WAIT_SECONDS)
                continue

            raise


# ============================================================
# GOOGLE SHEETS
# ============================================================

def create_google_service():
    if not GOOGLE_CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"Не найден Google-ключ:\n"
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


def get_sheet_titles(service):
    response = (
        service.spreadsheets()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            fields="sheets(properties(title))",
        )
        .execute()
    )

    return [
        sheet["properties"]["title"]
        for sheet in response.get("sheets", [])
    ]


def read_column(
    service,
    sheet_name,
    column,
    start_row,
):
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=SPREADSHEET_ID,
            range=(
                f"'{sheet_name}'!"
                f"{column}{start_row}:{column}"
            ),
            majorDimension="COLUMNS",
            valueRenderOption="FORMATTED_VALUE",
        )
        .execute()
    )

    values = response.get("values", [])

    if not values:
        return []

    return values[0]


def read_sheet_rows(service, cfg):
    result_values = read_column(
        service,
        cfg["sheet"],
        cfg["result_column"],
        cfg["start_row"],
    )

    text_values = read_column(
        service,
        cfg["sheet"],
        cfg["text_column"],
        cfg["start_row"],
    )

    number_values = read_column(
        service,
        cfg["sheet"],
        cfg["number_column"],
        cfg["start_row"],
    )

    processed_values = read_column(
        service,
        cfg["sheet"],
        cfg["processed_column"],
        cfg["start_row"],
    )

    row_count = max(
        len(result_values),
        len(text_values),
        len(number_values),
        len(processed_values),
        0,
    )

    rows = []

    for index in range(row_count):
        rows.append(
            {
                "row_number": cfg["start_row"] + index,
                "selector": (
                    str(result_values[index]).strip()
                    if index < len(result_values)
                    else ""
                ),
                "text": (
                    str(text_values[index]).strip()
                    if index < len(text_values)
                    else ""
                ),
                "number": (
                    str(number_values[index]).strip()
                    if index < len(number_values)
                    else ""
                ),
                "processed_version": (
                    str(processed_values[index]).strip()
                    if index < len(processed_values)
                    else ""
                ),
            }
        )

    return rows


def write_result(
    service,
    sheet_name,
    column,
    row_number,
    value,
):
    target_range = (
        f"'{sheet_name}'!"
        f"{column}{row_number}"
    )

    execute_google_write(
        lambda: (
            service.spreadsheets()
            .values()
            .update(
                spreadsheetId=SPREADSHEET_ID,
                range=target_range,
                valueInputOption="USER_ENTERED",
                body={"values": [[value]]},
            )
        )
    )


def clear_result(
    service,
    sheet_name,
    column,
    row_number,
):
    target_range = (
        f"'{sheet_name}'!"
        f"{column}{row_number}"
    )

    execute_google_write(
        lambda: (
            service.spreadsheets()
            .values()
            .clear(
                spreadsheetId=SPREADSHEET_ID,
                range=target_range,
                body={},
            )
        )
    )


def mark_processed(
    service,
    cfg,
    row_number,
):
    write_result(
        service=service,
        sheet_name=cfg["sheet"],
        column=cfg["processed_column"],
        row_number=row_number,
        value=cfg["processing_version"],
    )


# ============================================================
# PROMPTS
# ============================================================

def load_prompt(prompt_file):
    prompt_path = PROJECT_DIR / prompt_file

    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Не найден файл промпта:\n{prompt_path}"
        )

    text = prompt_path.read_text(
        encoding="utf-8"
    ).strip()

    if not text:
        raise RuntimeError(
            f"Файл промпта пуст:\n{prompt_path}"
        )

    return text


# ============================================================
# OLLAMA / QWEN
# ============================================================

def check_ollama():
    try:
        response = requests.get(
            "http://127.0.0.1:11434/api/tags",
            timeout=10,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError(
            "Не удалось подключиться к Ollama.\n"
            "Проверь, что Ollama запущен."
        ) from error

    models = {
        item.get("name", "")
        for item in response.json().get("models", [])
    }

    if OLLAMA_MODEL not in models:
        raise RuntimeError(
            f"В Ollama не найдена модель "
            f"{OLLAMA_MODEL}.\n"
            f"Доступные модели: "
            f"{', '.join(sorted(models)) or 'нет'}"
        )


def build_row_prompt(
    sheet_name,
    text_column,
    number_column,
    text_value,
    number_value,
):
    return (
        "Проанализируй одну строку таблицы.\n\n"
        f"Лист: {sheet_name}\n"
        f"Поле {text_column}: {text_value}\n"
        f"Поле {number_column}: {number_value}\n"
    )


def call_qwen_json(messages):
    """
    Выполняет один stateless-запрос к Ollama и возвращает JSON.

    История между контурами не передаётся: каждый вызов получает
    собственный массив messages.
    """
    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "format": "json",
        "stream": False,
        "think": False,
        "options": {
            "temperature": 0,
        },
        "keep_alive": "10m",
    }

    started = time.perf_counter()

    response = requests.post(
        OLLAMA_URL,
        json=payload,
        timeout=120,
    )
    response.raise_for_status()

    elapsed = time.perf_counter() - started

    data = response.json()
    content = (
        data.get("message", {})
        .get("content", "")
        .strip()
    )

    if not content:
        raise RuntimeError(
            "Qwen вернул пустой ответ."
        )

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Qwen вернул невалидный JSON:\n"
            f"{content}"
        ) from error

    if not isinstance(parsed, dict):
        raise RuntimeError(
            "Qwen должен вернуть JSON-объект."
        )

    return {
        "parsed": parsed,
        "elapsed": elapsed,
    }


def parse_classification_response(response):
    parsed = response["parsed"]

    result = str(
        parsed.get("result", "")
    ).strip().upper()

    reason = str(
        parsed.get("reason", "")
    ).strip()

    if result not in {
        "MATCH",
        "NO_MATCH",
        "UNKNOWN",
    }:
        raise RuntimeError(
            "Qwen вернул недопустимое "
            f"значение result: {result!r}"
        )

    return {
        "result": result,
        "reason": reason,
        "elapsed": response["elapsed"],
        "raw": parsed,
    }


def build_row_prompt(
    sheet_name,
    text_column,
    number_column,
    text_value,
    number_value,
):
    return (
        "ИСХОДНЫЕ ДАННЫЕ СТРОКИ:\n"
        f"Лист: {sheet_name}\n"
        f"Поле {text_column}: {text_value}\n"
        f"Поле {number_column}: {number_value}\n"
    )


def ask_qwen(
    system_prompt,
    sheet_name,
    text_column,
    number_column,
    text_value,
    number_value,
):
    """
    Контур 1: основной классификатор.

    Его правила полностью задаются проектным prompt_*.txt.
    """
    row_prompt = build_row_prompt(
        sheet_name=sheet_name,
        text_column=text_column,
        number_column=number_column,
        text_value=text_value,
        number_value=number_value,
    )

    response = call_qwen_json(
        messages=[
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": row_prompt,
            },
        ]
    )

    return parse_classification_response(response)


def ask_qwen_auditor(
    system_prompt,
    sheet_name,
    text_column,
    number_column,
    text_value,
    number_value,
    primary_answer,
):
    """
    Контур 2: аудитор.

    Он НЕ классифицирует строку заново и НЕ голосует за другой result.
    Его единственная задача — проверить уже готовый ответ контура 1:
    - следует ли result из правил;
    - не противоречит ли reason исходным данным;
    - не использованы ли как исходный факт сведения,
      которых нет во входных данных;
    - нет ли логического противоречия между полями ответа;
    - выполнены ли обязательные условия проектного prompt_*.txt.
    """
    auditor_prompt = (
        "Ты — независимый аудитор результата классификатора.\n"
        "НЕ решай задачу голосованием и НЕ пытайся просто выдать "
        "собственный MATCH/NO_MATCH.\n"
        "Проверь конкретный ответ первого контура.\n\n"
        "Поставь PASS только если одновременно:\n"
        "1. итоговый result допустим правилами проекта;\n"
        "2. итоговый result логически следует из исходных данных "
        "и правил проекта;\n"
        "3. объяснение не противоречит исходным данным;\n"
        "4. ответ не выдаёт отсутствующий во входе исходный факт "
        "за явно предоставленный факт;\n"
        "5. внутренние поля ответа не противоречат друг другу;\n"
        "6. соблюдены все обязательные условия prompt проекта.\n\n"
        "Если найдено хотя бы одно существенное нарушение — FAIL.\n"
        "Не исправляй ответ. Только перечисли конкретные ошибки.\n\n"
        "Верни только JSON:\n"
        "{\n"
        '  "verdict": "PASS",\n'
        '  "issues": [],\n'
        '  "reason": "краткий итог аудита"\n'
        "}\n\n"
        "Допустимые verdict: PASS, FAIL.\n"
        "issues — массив строк. При FAIL он не должен быть пустым.\n"
        "Никакого текста до или после JSON.\n\n"
        "ПРАВИЛА ПРОЕКТА:\n"
        "--------------------\n"
        f"{system_prompt}\n"
        "--------------------"
    )

    user_prompt = (
        build_row_prompt(
            sheet_name=sheet_name,
            text_column=text_column,
            number_column=number_column,
            text_value=text_value,
            number_value=number_value,
        )
        + "\nОТВЕТ КОНТУРА 1:\n"
        + json.dumps(
            primary_answer["raw"],
            ensure_ascii=False,
            indent=2,
        )
    )

    response = call_qwen_json(
        messages=[
            {
                "role": "system",
                "content": auditor_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ]
    )

    parsed = response["parsed"]

    verdict = str(
        parsed.get("verdict", "")
    ).strip().upper()

    if verdict not in {"PASS", "FAIL"}:
        raise RuntimeError(
            "Qwen-аудитор вернул недопустимый verdict: "
            f"{verdict!r}"
        )

    raw_issues = parsed.get("issues", [])

    if raw_issues is None:
        raw_issues = []

    if not isinstance(raw_issues, list):
        raise RuntimeError(
            "Qwen-аудитор должен вернуть issues как массив."
        )

    issues = [
        str(item).strip()
        for item in raw_issues
        if str(item).strip()
    ]

    reason = str(
        parsed.get("reason", "")
    ).strip()

    if verdict == "FAIL" and not issues:
        issues = [
            reason or
            "Аудитор сообщил FAIL без детализации."
        ]

    return {
        "verdict": verdict,
        "issues": issues,
        "reason": reason,
        "elapsed": response["elapsed"],
        "raw": parsed,
    }


def ask_qwen_corrector(
    system_prompt,
    sheet_name,
    text_column,
    number_column,
    text_value,
    number_value,
    primary_answer,
    audit_answer,
):
    """
    Контур 3: корректор.

    Запускается только после FAIL аудитора. Получает:
    - исходные данные;
    - проектные правила;
    - ошибочный ответ первого контура;
    - конкретный список ошибок аудитора.

    Возвращает новый окончательный result.
    """
    corrector_prompt = (
        "Ты — корректор результата классификации.\n"
        "Аудитор нашёл существенные ошибки в ответе первого контура.\n"
        "Исправь классификацию по исходным данным и ПРАВИЛАМ ПРОЕКТА.\n"
        "Обязательно устрани ВСЕ перечисленные замечания аудитора.\n"
        "Не сохраняй прежний result только потому, что его выбрал "
        "первый контур.\n"
        "Не придумывай отсутствующие исходные факты.\n\n"
        "Верни только JSON с полями:\n"
        '{"result":"MATCH|NO_MATCH|UNKNOWN","reason":"краткая причина"}\n'
        "Никакого текста до или после JSON.\n\n"
        "ПРАВИЛА ПРОЕКТА:\n"
        "--------------------\n"
        f"{system_prompt}\n"
        "--------------------"
    )

    user_prompt = (
        build_row_prompt(
            sheet_name=sheet_name,
            text_column=text_column,
            number_column=number_column,
            text_value=text_value,
            number_value=number_value,
        )
        + "\nОШИБОЧНЫЙ ОТВЕТ КОНТУРА 1:\n"
        + json.dumps(
            primary_answer["raw"],
            ensure_ascii=False,
            indent=2,
        )
        + "\n\nЗАМЕЧАНИЯ АУДИТОРА:\n"
        + json.dumps(
            audit_answer["issues"],
            ensure_ascii=False,
            indent=2,
        )
        + "\n\nИТОГ АУДИТА:\n"
        + (audit_answer["reason"] or "[без дополнительного комментария]")
    )

    response = call_qwen_json(
        messages=[
            {
                "role": "system",
                "content": corrector_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ]
    )

    return parse_classification_response(response)


def get_verified_answer(
    system_prompt,
    sheet_name,
    text_column,
    number_column,
    text_value,
    number_value,
):
    """
    Полный цикл:
    1. основной классификатор;
    2. независимый аудит результата;
    3. при FAIL — автоматическая коррекция.

    PASS аудитора означает, что в таблицу идёт ответ первого контура.
    FAIL означает, что ответ первого контура отбрасывается, а в таблицу
    идёт новый ответ корректора.
    """
    primary = ask_qwen(
        system_prompt=system_prompt,
        sheet_name=sheet_name,
        text_column=text_column,
        number_column=number_column,
        text_value=text_value,
        number_value=number_value,
    )

    audit = ask_qwen_auditor(
        system_prompt=system_prompt,
        sheet_name=sheet_name,
        text_column=text_column,
        number_column=number_column,
        text_value=text_value,
        number_value=number_value,
        primary_answer=primary,
    )

    if audit["verdict"] == "PASS":
        return {
            "answer": primary,
            "primary": primary,
            "audit": audit,
            "corrector": None,
            "audit_passed": True,
        }

    corrector = ask_qwen_corrector(
        system_prompt=system_prompt,
        sheet_name=sheet_name,
        text_column=text_column,
        number_column=number_column,
        text_value=text_value,
        number_value=number_value,
        primary_answer=primary,
        audit_answer=audit,
    )

    return {
        "answer": corrector,
        "primary": primary,
        "audit": audit,
        "corrector": corrector,
        "audit_passed": False,
    }



# ============================================================
# ОБРАБОТКА ОДНОГО ЛИСТА
# ============================================================

def process_sheet(
    service,
    cfg,
    system_prompt,
):
    sheet_name = cfg["sheet"]

    print("\n" + "=" * 70)
    print(f"ЛИСТ: {sheet_name}")
    print(f"PROMPT: {cfg['prompt_file']}")
    print(
        "СТОЛБЦЫ: "
        f"результат={cfg['result_column']}, "
        f"текст={cfg['text_column']}, "
        f"число={cfg['number_column']}, "
        f"обработано={cfg['processed_column']}"
    )
    print(
        "ВЕРСИЯ ОБРАБОТКИ: "
        f"{cfg['processing_version']}"
    )
    print("=" * 70)

    rows = read_sheet_rows(
        service=service,
        cfg=cfg,
    )

    if not rows:
        print("Нет строк для обработки.")
        return {
            "processed": 0,
            "skipped_current_version": 0,
            "match": 0,
            "no_match": 0,
            "unknown": 0,
            "errors": 0,
            "audit_passed": 0,
            "audit_failed": 0,
            "corrected": 0,
        }

    stats = {
        "processed": 0,
        "skipped_current_version": 0,
        "match": 0,
        "no_match": 0,
        "unknown": 0,
        "errors": 0,
        "audit_passed": 0,
        "audit_failed": 0,
        "corrected": 0,
    }

    print(f"Найдено строк: {len(rows)}")

    for row in rows:
        row_number = row["row_number"]
        selector = row["selector"]
        text_value = row["text"]
        number_value = row["number"]
        processed_version = row["processed_version"]

        # Полностью пустую строку не обрабатываем.
        if (
            not selector
            and not text_value
            and not number_value
            and not processed_version
        ):
            continue

        # Если строка уже обработана текущей версией правил,
        # повторно её не отправляем в Qwen.
        if processed_version == cfg["processing_version"]:
            stats["skipped_current_version"] += 1
            continue

        stats["processed"] += 1

        print("\n" + "-" * 60)
        print(f"Строка {row_number}")
        print(
            f"  {cfg['result_column']} сейчас: "
            f"{selector or '[пусто]'}"
        )
        print(
            f"  {cfg['text_column']}: "
            f"{text_value or '[пусто]'}"
        )
        print(
            f"  {cfg['number_column']}: "
            f"{number_value or '[пусто]'}"
        )
        print(
            f"  {cfg['processed_column']} версия: "
            f"{processed_version or '[пусто]'}"
        )

        # Неполные исходные данные.
        if not text_value or not number_value:
            print(
                "  Неполные данные → "
                f"{cfg['unknown_value']}"
            )

            write_result(
                service=service,
                sheet_name=sheet_name,
                column=cfg["result_column"],
                row_number=row_number,
                value=cfg["unknown_value"],
            )

            mark_processed(
                service=service,
                cfg=cfg,
                row_number=row_number,
            )

            stats["unknown"] += 1
            continue

        try:
            print("  → Контур 1: основная сессия Qwen...")

            verification = get_verified_answer(
                system_prompt=system_prompt,
                sheet_name=sheet_name,
                text_column=cfg["text_column"],
                number_column=cfg["number_column"],
                text_value=text_value,
                number_value=number_value,
            )

            primary = verification["primary"]
            audit = verification["audit"]
            corrector = verification["corrector"]
            answer = verification["answer"]

            print(
                "  ← Контур 1: "
                f"{primary['result']} "
                f"за {primary['elapsed']:.2f} сек."
            )
            print(
                f"    Причина: {primary['reason']}"
            )

            print(
                "  ← Контур 2 / аудит: "
                f"{audit['verdict']} "
                f"за {audit['elapsed']:.2f} сек."
            )

            if audit["reason"]:
                print(
                    f"    Итог аудита: {audit['reason']}"
                )

            if verification["audit_passed"]:
                stats["audit_passed"] += 1

                print(
                    "  ✓ Аудит пройден. "
                    f"Итог = {answer['result']}"
                )

            else:
                stats["audit_failed"] += 1
                stats["corrected"] += 1

                print(
                    "  ! Аудит нашёл ошибку первого контура."
                )

                for issue_index, issue in enumerate(
                    audit["issues"],
                    start=1,
                ):
                    print(
                        f"    Ошибка {issue_index}: {issue}"
                    )

                print(
                    "  → Контур 3: автоматическая коррекция..."
                )
                print(
                    "  ← Контур 3: "
                    f"{corrector['result']} "
                    f"за {corrector['elapsed']:.2f} сек."
                )
                print(
                    f"    Причина: {corrector['reason']}"
                )
                print(
                    "  ✓ В таблицу пойдёт исправленный итог: "
                    f"{answer['result']}"
                )

            if answer["result"] == "MATCH":
                print(
                    "  Действие: записываем "
                    f"{cfg['match_value']}"
                )

                write_result(
                    service=service,
                    sheet_name=sheet_name,
                    column=cfg["result_column"],
                    row_number=row_number,
                    value=cfg["match_value"],
                )

                mark_processed(
                    service=service,
                    cfg=cfg,
                    row_number=row_number,
                )

                stats["match"] += 1

            elif answer["result"] == "NO_MATCH":
                stats["no_match"] += 1

                if cfg["no_match_action"] == "keep":
                    print(
                        "  Действие: оставляем "
                        f"{cfg['result_column']} как есть"
                    )

                elif cfg["no_match_action"] == "clear":
                    print(
                        "  Действие: очищаем "
                        f"{cfg['result_column']}"
                    )

                    clear_result(
                        service=service,
                        sheet_name=sheet_name,
                        column=cfg["result_column"],
                        row_number=row_number,
                    )

                elif cfg["no_match_action"] == "set":
                    print(
                        "  Действие: выбираем в селекторе "
                        f"{cfg['no_match_value']}"
                    )

                    write_result(
                        service=service,
                        sheet_name=sheet_name,
                        column=cfg["result_column"],
                        row_number=row_number,
                        value=cfg["no_match_value"],
                    )

                else:
                    raise RuntimeError(
                        "Недопустимое no_match_action: "
                        f"{cfg['no_match_action']!r}. "
                        'Допустимо: "keep", "clear" или "set".'
                    )

                mark_processed(
                    service=service,
                    cfg=cfg,
                    row_number=row_number,
                )

            else:
                print(
                    "  Действие: UNKNOWN → "
                    f"{cfg['unknown_value']}"
                )

                write_result(
                    service=service,
                    sheet_name=sheet_name,
                    column=cfg["result_column"],
                    row_number=row_number,
                    value=cfg["unknown_value"],
                )

                mark_processed(
                    service=service,
                    cfg=cfg,
                    row_number=row_number,
                )

                stats["unknown"] += 1

        except Exception as error:
            stats["errors"] += 1

            print(
                "  Ошибка обработки строки: "
                f"{type(error).__name__}: "
                f"{error}"
            )

            if cfg["on_error_value"]:
                print(
                    "  При ошибке записываем "
                    f"{cfg['on_error_value']}"
                )

                write_result(
                    service=service,
                    sheet_name=sheet_name,
                    column=cfg["result_column"],
                    row_number=row_number,
                    value=cfg["on_error_value"],
                )

            print(
                "  Версию обработки не ставим: "
                "строка будет повторена при следующем запуске."
            )

        time.sleep(cfg["delay_seconds"])

    return stats


# ============================================================
# ЗАПУСК
# ============================================================

def main():
    print("=" * 70)
    print("AI FILTER")
    print("=" * 70)
    print(f"Конфигурация: {CONFIG_FILE}")
    print(f"Проект: {PROJECT_DIR}")
    print(f"Модель: {OLLAMA_MODEL}")
    print(
        "Интервал Google write: "
        f"{GOOGLE_WRITE_INTERVAL:.2f} сек."
    )

    if not AI_SHEETS:
        raise RuntimeError(
            "В config.json отсутствует "
            "ai.sheets или список пуст."
        )

    sheet_configs = [
        normalize_sheet_config(item)
        for item in AI_SHEETS
    ]

    print(
        "AI-листов в конфигурации: "
        f"{len(sheet_configs)}"
    )

    print("\nПроверяем Ollama...")
    check_ollama()
    print("Ollama доступен.")

    print("\nПодключаемся к Google Sheets...")
    service = create_google_service()
    real_sheet_titles = get_sheet_titles(service)

    available_sheets = {
        normalize_sheet_name(real_name): real_name
        for real_name in real_sheet_titles
    }

    print("Google Sheets доступен.")
    print("Листы, которые реально видит API:")

    for real_name in real_sheet_titles:
        print(f"  - {real_name!r}")

    total_stats = {
        "processed": 0,
        "skipped_current_version": 0,
        "match": 0,
        "no_match": 0,
        "unknown": 0,
        "errors": 0,
        "audit_passed": 0,
        "audit_failed": 0,
        "corrected": 0,
    }

    for cfg in sheet_configs:
        configured_name = cfg["sheet"]
        normalized_name = normalize_sheet_name(
            configured_name
        )

        real_name = available_sheets.get(
            normalized_name
        )

        if real_name is None:
            raise RuntimeError(
                "В таблице не найден лист "
                f"{configured_name!r}.\n"
                "Листы, которые видит API: "
                + ", ".join(
                    repr(name)
                    for name in real_sheet_titles
                )
            )

        if real_name != configured_name:
            print(
                "\nИмя листа нормализовано: "
                f"{configured_name!r} -> {real_name!r}"
            )

        cfg = dict(cfg)
        cfg["sheet"] = real_name

        print(
            "\nЗагружаем промпт для листа "
            f"'{cfg['sheet']}'..."
        )

        system_prompt = load_prompt(
            cfg["prompt_file"]
        )

        stats = process_sheet(
            service=service,
            cfg=cfg,
            system_prompt=system_prompt,
        )

        for key in total_stats:
            total_stats[key] += stats[key]

    print("\n" + "=" * 70)
    print("ГОТОВО")
    print(
        f"Обработано строк: "
        f"{total_stats['processed']}"
    )
    print(
        "Пропущено (уже текущая версия): "
        f"{total_stats['skipped_current_version']}"
    )
    print(
        f"MATCH: "
        f"{total_stats['match']}"
    )
    print(
        f"NO_MATCH: "
        f"{total_stats['no_match']}"
    )
    print(
        f"UNKNOWN: "
        f"{total_stats['unknown']}"
    )
    print(
        f"Ошибок: "
        f"{total_stats['errors']}"
    )
    print(
        "Аудит: PASS: "
        f"{total_stats['audit_passed']}"
    )
    print(
        "Аудит: FAIL: "
        f"{total_stats['audit_failed']}"
    )
    print(
        "Автоматически исправлено: "
        f"{total_stats['corrected']}"
    )
    print("=" * 70)


if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        print(
            "\nОШИБКА:\n"
            f"{type(error).__name__}: {error}"
        )
        sys.exit(1)
