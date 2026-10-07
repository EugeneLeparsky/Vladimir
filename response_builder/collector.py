from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from .common import SetupError, atomic_json, file_lock, read_json, safe_failure
from .google_client import Sheets
from .rabota_client import Rabota, SessionExpired
from .schema import response_record
from .state import State


def validate_config(config):
    if config.get("builder_version") != 1 or config.get("status") != "ready":
        raise SetupError("Проект не готов. Возобновите мастер через --resume; сбор не запускается.")
    if not config.get("spreadsheet_id") or config["spreadsheet_id"] == config.get("template_id"):
        raise SetupError("Нет отдельной таблицы проекта. Запись в шаблон запрещена.")
    if not config.get("vacancies") or config.get("rabota_schema", {}).get("confirmed") is not True:
        raise SetupError("Вакансии или схема откликов не подтверждены.")
    identifiers = set()
    for vacancy in config["vacancies"]:
        identifier = str(vacancy["vacancy_id"])
        if not identifier.isdigit() or identifier in identifiers or vacancy.get("url") != "https://rabota.by/vacancy/" + identifier:
            raise SetupError("Некорректная/повторная вакансия в конфиге.")
        identifiers.add(identifier)
    columns = config["layout"]["columns"]
    if len(set(columns.values())) != len(columns) or any(not isinstance(v, int) or v < 0 for v in columns.values()):
        raise SetupError("Некорректные столбцы записи.")


def event(project: Path, filename, data):
    data = dict(data, time=datetime.now(timezone.utc).isoformat())
    with (project / filename).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False) + "\n")


def process_queue(project, state, sheets, rabota, retry=False, test=False, pause=time.sleep):
    index = sheets.check()
    state.reconcile(index)
    items = state.items(retry=retry)
    if test:
        items = items[:1]  # at most ONE response attempt, even if it fails
    saved, errors = 0, 0
    for item in items:
        key = item["key"]
        try:
            # Re-read keys before retry: an uncertain prior write may already be in Sheets.
            index = sheets.index()
            if key in index:
                state.saved(key, index[key])
                continue
            payload = json.loads(item["payload"]) if item["payload"] else rabota.extract(json.loads(item["record"]))
            payload.setdefault("collected_at", datetime.now(timezone.utc).isoformat())
            row = item["sheet_row"] or sheets.choose_row(state.reserved_rows())
            state.reserve(key, row, payload)
            sheets.write(row, payload)
            state.saved(key, row)  # committed after verified Google write
            saved += 1
            event(project, "logs/events.jsonl", {"event": "saved", "key": key, "row": row})
        except SessionExpired:
            state.failed(key, "Сессия rabota.by истекла; нужна проверка авторизации.")
            raise
        except Exception as error:
            message = safe_failure(error)
            state.failed(key, message)
            errors += 1
            event(project, "errors/records.jsonl", {"key": key, "error": message})
        finally:
            pause(1.3)  # respect per-user Google read/write quotas; no concurrent collector
    return {"saved": saved, "errors": errors, "attempted": len(items)}


def collect(project, config, mode, visible):
    shared = project.parent / "_shared"
    for folder in ("state", "logs", "errors"):
        (project / folder).mkdir(exist_ok=True)
    with file_lock(project / "state" / ".run.lock"):
        sheets = Sheets(shared, config)
        index = sheets.check()
        with Rabota(shared, visible=visible or mode in {"test", "check"}, interactive=True) as rabota:
            if mode == "check":
                from .create_project import verify_responses
                count = verify_responses(rabota, config, require_sample=False)
                print(f"Google и rabota.by доступны; проверена схема {count} откликов. Записи не изменены.")
                return 0
            state = State(project / "state" / "responses.sqlite3")
            try:
                state.reconcile(index)
                discovery_errors = 0
                # Retry is exclusively for the durable error queue. Unkeyed schema errors need a new FULL scan.
                if mode != "retry":
                    stop = False
                    for vacancy in config["vacancies"]:
                        if stop:
                            break
                        try:
                            for candidate in rabota.candidates(vacancy["vacancy_id"]):
                                try:
                                    record = response_record(candidate, vacancy, config["rabota_schema"])
                                    if record:
                                        state.enqueue(record)
                                        if mode == "test" and record["key"] not in index:
                                            stop = True
                                            break
                                except SetupError as error:
                                    discovery_errors += 1
                                    event(project, "errors/discovery.jsonl", {"vacancy_id": vacancy["vacancy_id"],
                                          "error": safe_failure(error)})
                        except SessionExpired:
                            raise
                        except Exception as error:
                            discovery_errors += 1
                            event(project, "errors/discovery.jsonl", {"vacancy_id": vacancy["vacancy_id"],
                                  "error": safe_failure(error), "incomplete_pass": True})
                result = process_queue(project, state, sheets, rabota, retry=mode == "retry", test=mode == "test")
                result.update(mode=mode, discovery_errors=discovery_errors,
                              complete=result["errors"] == 0 and discovery_errors == 0,
                              pending_errors=state.db.execute("SELECT COUNT(*) FROM responses WHERE status='error'").fetchone()[0])
                atomic_json(project / "logs" / "last_run.json", result)
                print(f"Сохранено: {result['saved']}; ошибок записей: {result['errors']}; ошибок обхода/схемы: {discovery_errors}.")
                if result["pending_errors"]:
                    print(f"В очереди ошибок осталось: {result['pending_errors']}. Для их обработки выберите «Повтор ошибок».")
                if mode == "test" and not result["attempted"]:
                    print("Нет нового отклика для TEST. Функциональная запись не проверена.")
                    return 2
                return 0 if result["complete"] else 1
            finally:
                state.close()


def main(argv=None, project_dir=None):
    parser = argparse.ArgumentParser(description="Сбор входящих откликов выбранных вакансий")
    parser.add_argument("--mode", choices=["test", "full", "check", "retry"], required=True)
    parser.add_argument("--visible", action="store_true")
    if project_dir is None:
        parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args(argv)
    project = (project_dir or args.project).resolve()
    try:
        config = read_json(project / "project_config.json")
        validate_config(config)
        from .credentials_setup import ensure_google_credentials
        ensure_google_credentials(project.parent / "_shared", need_oauth=False)
        return collect(project, config, args.mode, args.visible)
    except (Exception, KeyboardInterrupt) as error:
        print(safe_failure(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
