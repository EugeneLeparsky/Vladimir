from __future__ import annotations

import argparse
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
import sys
import uuid

from .common import (SetupError, atomic_json, column_index, file_lock, google_id,
                     project_name, read_json, safe_failure)
from .google_client import Drive, EXTRA_FIELDS, Sheets, service_credentials
from .rabota_client import Rabota
from .schema import response_record, scalar_paths


def choose(prompt, count):
    try:
        number = int(input(prompt))
    except ValueError:
        raise SetupError("Ожидался номер из списка.") from None
    if not 1 <= number <= count:
        raise SetupError("Номер вне списка.")
    return number - 1


def choose_vacancies(vacancies):
    for number, vacancy in enumerate(vacancies, 1):
        print(f"{number}. {vacancy['name']} (vacancy_id={vacancy['vacancy_id']})")
    try:
        selected = sorted({int(x.strip()) - 1 for x in input("Номера вакансий через запятую: ").split(",")})
    except ValueError:
        raise SetupError("Номера вакансий должны быть числами через запятую.") from None
    if not selected or any(x < 0 or x >= len(vacancies) for x in selected):
        raise SetupError("Выберите хотя бы одну вакансию из списка.")
    return [vacancies[x] for x in selected]


def select_schema(sample, vacancy, shared):
    saved = shared / "response_schema.json"
    if saved.exists():
        print("Есть ранее подтверждённая схема откликов: 1 — использовать; 2 — настроить заново.")
        if choose("Схема: ", 2) == 0:
            schema = read_json(saved)
            if response_record(sample, vacancy, schema) is None:
                raise SetupError("Образец не соответствует сохранённой схеме входящего отклика.")
            return schema
    paths = scalar_paths(sample)
    atomic_json(shared / "response_schema_fields.json", {"fields": paths,
        "note": "Только имена и типы полей; значения, cookies и токены не сохраняются."})
    print("Поля реального ответа candidates_list (значения скрыты):")
    for path, kind in sorted(paths.items()):
        print(f"  {path}: {kind}")
    print("ID резюме/кандидата НЕ является ID отдельного отклика.")
    print("Если устойчивый ID отклика неизвестен, прервите мастер: нельзя угадывать его по позиции/странице.")
    id_path = input("Поле устойчивого ID отклика (dot.path): ").strip()
    date_path = input("Поле времени поступления отклика (dot.path): ").strip()
    if id_path not in paths or date_path not in paths:
        raise SetupError("ID/дата не подтверждены структурой API. Диагностика: _shared/response_schema_fields.json.")
    print("Формат времени: 1 — ISO 8601 с часовым поясом, 2 — Unix seconds, 3 — Unix milliseconds.")
    encoding = ["iso8601", "unix_seconds", "unix_milliseconds"][choose("Формат: ", 3)]
    incoming_path = input("Поле признака ВХОДЯЩЕГО отклика (пусто только если API содержит исключительно входящие): ").strip()
    schema = {"response_id_path": id_path, "received_at_path": date_path, "time_encoding": encoding,
              "incoming_path": incoming_path, "confirmed": True}
    if incoming_path:
        if incoming_path not in paths:
            raise SetupError("Поле входящего отклика отсутствует в ответе.")
        values = [x.strip() for x in input("Значения признака входящего отклика через запятую (включая обработанные статусы): ").split(",") if x.strip()]
        if not values:
            raise SetupError("Не заданы значения признака входящего отклика.")
        schema["incoming_values"] = values
    else:
        if input("Подтверждено, что API не содержит приглашений/исходящих обращений? Введите ДА: ").strip().upper() != "ДА":
            raise SetupError("Не подтверждён источник входящих откликов.")
        schema["incoming_only_confirmed"] = True
    if input("Подтверждено, что ID относится к ОТДЕЛЬНОМУ отклику, а дата — к его поступлению? Введите ДА: ").strip().upper() != "ДА":
        raise SetupError("Смысл ID и даты не подтверждён. Создание остановлено.")
    # Validate date, identity and known resume link before creating a Drive copy.
    record = response_record(sample, vacancy, schema)
    if record is None:
        raise SetupError("Образец не является входящим откликом по выбранному признаку. Нужен другой образец.")
    atomic_json(saved, schema)
    return schema


def choose_layout(sheets):
    for number, sheet in enumerate(sheets, 1):
        print(f"{number}. {sheet['properties']['title']}")
    properties = sheets[choose("Лист шаблона для записи откликов: ", len(sheets))]["properties"]
    if properties.get("sheetType", "GRID") != "GRID":
        raise SetupError("Нужен обычный GRID-лист.")
    columns = {}
    print("Укажите столбцы шаблона. Значения по умолчанию взяты из существующего core_parser.py.")
    for field, label, default in [("name", "ФИО/ссылка", "B"), ("age", "Возраст", "C"),
                                  ("phone", "Телефон", "D"), ("resume_id", "ID резюме", "Z")]:
        columns[field] = column_index(input(f"{label} [{default}]: ").strip() or default)
    if len(set(columns.values())) != len(columns):
        raise SetupError("Столбцы данных должны быть разными.")
    count = properties["gridProperties"]["columnCount"]
    if max(columns.values()) >= count:
        raise SetupError("Указанный столбец отсутствует в шаблоне. Выберите существующий столбец.")
    try:
        first_row = int(input("Первая строка данных [2]: ").strip() or "2")
    except ValueError:
        raise SetupError("Номер строки должен быть числом.") from None
    if not 2 <= first_row <= properties["gridProperties"]["rowCount"]:
        raise SetupError("Первая строка должна быть в существующей области листа и ниже заголовка.")
    columns.update({field: count + offset for offset, field in enumerate(EXTRA_FIELDS)})
    return {"sheet_id": properties["sheetId"], "sheet_title": properties["title"],
            "first_data_row": first_row, "header_row": first_row - 1, "columns": columns}


COLLECTOR_WRAPPER = '''from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from response_builder.collector import main

if __name__ == "__main__":
    raise SystemExit(main(project_dir=Path(__file__).resolve().parent))
'''

PROJECT_BAT = '''@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
:menu
cls
echo 1. TEST - один отклик, видимый браузер
echo 2. FULL - все новые отклики, headless
echo 3. Проверка Google + rabota.by / обновление входа
echo 4. Повтор ошибок - видимый браузер
echo 5. FULL - видимый браузер
echo 6. Выход
choice /c 123456 /n /m "Выберите режим: "
if errorlevel 6 exit /b 0
if errorlevel 5 (set "ARGS=--mode full --visible" & goto run)
if errorlevel 4 (set "ARGS=--mode retry --visible" & goto run)
if errorlevel 3 (set "ARGS=--mode check --visible" & goto run)
if errorlevel 2 (set "ARGS=--mode full" & goto run)
if errorlevel 1 (set "ARGS=--mode test --visible" & goto run)
goto menu
:run
if exist "%~dp0..\\_shared\\response_venv\\Scripts\\python.exe" (
  "%~dp0..\\_shared\\response_venv\\Scripts\\python.exe" "%~dp0collect_responses.py" %ARGS%
) else (
  py -3.12 "%~dp0collect_responses.py" %ARGS%
)
set "RC=%ERRORLEVEL%"
echo Код завершения: %RC%
pause
goto menu
'''


def create_launchers(project):
    for name, contents in {"collect_responses.py": COLLECTOR_WRAPPER, "start_project.bat": PROJECT_BAT}.items():
        path = project / name
        if path.exists():
            if path.read_text(encoding="utf-8") != contents:
                raise SetupError(f"Файл {name} уже изменён. Автоматическое перезаписывание запрещено.")
        else:
            with path.open("x", encoding="utf-8", newline="\r\n" if name.endswith(".bat") else "\n") as stream:
                stream.write(contents)
    for directory in ("state", "logs", "errors"):
        (project / directory).mkdir(exist_ok=True)


def verify_responses(rabota, config, require_sample=True):
    checked, keys = 0, {}
    for vacancy in config["vacancies"]:
        for candidate in islice(rabota.candidates(vacancy["vacancy_id"]), 50):
            record = response_record(candidate, vacancy, config["rabota_schema"])
            if record:
                old = keys.get(record["key"])
                signature = (record["resume_id"], record["received_at"])
                if old is not None and old != signature:
                    raise SetupError("Выбранный ID отклика повторяется с разными данными. Схема не подтверждена.")
                keys[record["key"]] = signature
                checked += 1
    if not checked and require_sample:
        raise SetupError("Нет входящего отклика для первичной проверки схемы. Проект пока не готов.")
    return checked


def finish_creation(project, config, drive, shared, retry_copy=False):
    if config.get("spreadsheet_id") == config["template_id"]:
        raise SetupError("Запись в таблицу-шаблон запрещена.")
    allow_create = not config.get("copy_requested") and not config.get("spreadsheet_id")
    if retry_copy:
        if config.get("spreadsheet_id"):
            raise SetupError("ID копии уже сохранён. Повторное копирование запрещено.")
        print("Повтор запроса может создать вторую копию, если предыдущая ещё не появилась в списке Drive.")
        print("Продолжайте только после проверки, что таблица с этим именем не создана.")
        if input("Для разрешения повторного запроса введите полное имя проекта: ") != config["project_name"]:
            raise SetupError("Повторное копирование не подтверждено.")
        allow_create = True
    config["copy_requested"] = True
    atomic_json(project / "project_config.json", config)  # persist intent BEFORE the non-idempotent Drive call
    identifier = drive.copy(config["template_id"], config["project_name"], config["creation_attempt"], allow_create=allow_create)
    if config.get("spreadsheet_id") and config["spreadsheet_id"] != identifier:
        raise SetupError("Конфиг указывает не на копию этой попытки. Продолжение запрещено.")
    config["spreadsheet_id"] = identifier
    atomic_json(project / "project_config.json", config)  # retain copy ID before any next operation
    drive.grant_service_account(identifier, service_credentials(shared).service_account_email)
    sheets = Sheets(shared, config)
    sheets.initialize()
    create_launchers(project)
    sheets.check()
    with Rabota(shared, visible=True, interactive=True) as rabota:
        verify_responses(rabota, config)
    config["status"] = "ready"
    config.pop("creation_error", None)
    atomic_json(project / "project_config.json", config)
    print(f"Проект готов: {project}\nЗапуск: start_project.bat\nGoogle spreadsheet ID: {identifier}")


def create(root: Path, resume=None, retry_copy=False):
    if retry_copy and not resume:
        raise SetupError("--retry-copy допускается только вместе с --resume.")
    root.mkdir(parents=True, exist_ok=True)
    shared = root / "_shared"
    shared.mkdir(exist_ok=True)
    # Validate required credentials before creating a project or a spreadsheet.
    service_credentials(shared)
    with file_lock(shared / ".response-creation.lock"):
        drive = Drive(shared)
        if resume:
            project = root / project_name(resume)
            config = read_json(project / "project_config.json")
            if (config.get("builder_version") != 1 or config.get("status") not in {"creating", "creation_failed"}
                    or not config.get("creation_attempt") or config.get("project_name") != project.name):
                raise SetupError("Можно возобновить только незавершённый проект этого мастера. Существующие проекты не изменяются.")
            drive.template(config["template_id"])
        else:
            while True:
                name = project_name(input("Название нового проекта: "))
                project = root / name
                if project.exists():
                    print("Папка уже существует. Она не будет изменена.")
                elif drive.named(name):
                    print("Google-таблица с этим названием уже существует. Вторая копия автоматически не создаётся.")
                else:
                    break
                print("1 — выбрать другое имя; 2 — отмена (незавершённый проект: отдельная команда --resume).")
                if choose("Действие: ", 2) == 1:
                    raise SetupError("Создание отменено.")
            template_id = google_id(input("ID или ссылка таблицы-шаблона «Тест ИИ»: "))
            template_sheets = drive.template(template_id)
            layout = choose_layout(template_sheets)
            with Rabota(shared, visible=True, interactive=True) as rabota:
                vacancies = choose_vacancies(rabota.vacancies())
                samples = []
                for vacancy in vacancies:
                    for candidate in islice(rabota.candidates(vacancy["vacancy_id"]), 20):
                        samples.append((candidate, vacancy))
                if not samples:
                    raise SetupError("В выбранных вакансиях нет образца отклика для проверки ID/даты. Таблица не создана.")
                print("Выберите известный ВХОДЯЩИЙ отклик для проверки схемы (персональные данные скрыты):")
                for number, (candidate, vacancy) in enumerate(samples, 1):
                    print(f"{number}. vacancy_id={vacancy['vacancy_id']}; resumeId={candidate.get('resumeId', 'нет')}; "
                          f"employerState={candidate.get('employerState', 'нет')}")
                sample, sample_vacancy = samples[choose("Образец: ", len(samples))]
                schema = select_schema(sample, sample_vacancy, shared)
                config = {"builder_version": 1, "project_name": name, "status": "creating",
                          "creation_attempt": uuid.uuid4().hex, "template_id": template_id,
                          "vacancies": vacancies, "layout": layout, "rabota_schema": schema,
                          "created_at": datetime.now(timezone.utc).isoformat()}
                verify_responses(rabota, config)
            # mkdir is exclusive; no existing project is overwritten, including a racing creator.
            project.mkdir()
            atomic_json(project / "project_config.json", config)
        with file_lock(project / "state" / ".run.lock"):
            try:
                finish_creation(project, config, drive, shared, retry_copy=retry_copy)
            except BaseException as error:
                config["status"] = "creation_failed"
                config["creation_error"] = safe_failure(error)
                atomic_json(project / "project_config.json", config)
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Создание проекта сбора откликов rabota.by")
    parser.add_argument("--root", type=Path, default=Path(r"C:\HR UD\Конкурсы"))
    parser.add_argument("--resume", help="Имя незавершённого проекта этого мастера")
    parser.add_argument("--retry-copy", action="store_true", help="Явное повторное копирование после проверки отсутствия копии в Drive")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "win32" and str(args.root).startswith("C:"):
            raise SetupError("На Linux задайте --root явно. Стандартная установка рассчитана на Windows.")
        from .credentials_setup import ensure_google_credentials
        ensure_google_credentials(args.root.resolve() / "_shared")
        create(args.root.resolve(), args.resume, args.retry_copy)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(safe_failure(error))
        print("Проект не помечен готовым. При созданной копии её ID сохранён в project_config.json; используйте --resume.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
