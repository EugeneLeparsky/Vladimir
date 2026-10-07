import json
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECTS_DIR = BASE_DIR / "projects"


def ask(prompt, default=None):
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value if value else default


def ask_yes_no(prompt, default_yes=True):
    default_text = "Y/n" if default_yes else "y/N"
    answer = input(f"{prompt} ({default_text}): ").strip().lower()

    if not answer:
        return default_yes

    return answer.startswith("y") or answer.startswith("д")


def main():
    print("=" * 60)
    print("Добавление нового проекта")
    print("=" * 60)

    project_name = ask("Название проекта (например, '5 миль')")

    if not project_name:
        print("Название проекта обязательно. Прерываю.")
        return

    project_dir = PROJECTS_DIR / project_name

    if project_dir.exists():
        print(f"\nПапка уже существует: {project_dir}")
        if not ask_yes_no("Продолжить и дополнить конфиг в ней?", default_yes=False):
            return
    else:
        project_dir.mkdir(parents=True)
        print(f"Создана папка: {project_dir}")

    spreadsheet_id = ask("ID Google-таблицы (из ссылки docs.google.com/spreadsheets/d/...)")

    show_browser_input = ask_yes_no("Показывать браузер во время сбора?", default_yes=True)

    vacancies = []

    print("\nТеперь добавим вакансии для этого проекта.")
    print("Оставь ID вакансии пустым, чтобы закончить добавление.\n")

    while True:
        vacancy_id = ask("ID вакансии (число из ссылки vacancy/...)")

        if not vacancy_id:
            break

        vacancy_name = ask("Название вакансии (для вывода в консоли)")
        sheet_name = ask("Название листа в Google Sheets для этой вакансии")

        vacancies.append(
            {
                "vacancy_id": vacancy_id,
                "name": vacancy_name,
                "sheet": sheet_name,
            }
        )

        print(f"  Добавлено: {vacancy_name} -> лист '{sheet_name}'\n")

    if not vacancies:
        print("Не добавлено ни одной вакансии. Прерываю.")
        return

    config = {
        "project_name": project_name,
        "spreadsheet_id": spreadsheet_id,
        "show_browser": show_browser_input,
        "vacancies": vacancies,
    }

    config_path = project_dir / "config.json"

    with open(config_path, "w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)

    print(f"\nСоздан файл: {config_path}")

    run_bat_path = project_dir / "run.bat"

    run_bat_content = (
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f"title HR Parser — {project_name}\r\n"
        "\r\n"
        f'cd /d "{BASE_DIR}"\r\n'
        "\r\n"
        f'py -3.12 core_parser.py "{config_path}"\r\n'
        "\r\n"
        "echo.\r\n"
        "echo ==============================================================\r\n"
        "echo Код завершения: %errorlevel%\r\n"
        "echo ==============================================================\r\n"
        "pause\r\n"
    )

    with open(run_bat_path, "w", encoding="utf-8") as file:
        file.write(run_bat_content)

    print(f"Создан файл: {run_bat_path}")

    auth_path = project_dir / "rabota_auth.json"

    print("\n" + "=" * 60)
    print(f"Осталось создать файл авторизации: {auth_path}")
    print("=" * 60)

    if ask_yes_no("Открыть браузер сейчас для входа в аккаунт rabota.by?", default_yes=True):
        print("\nОткрываю браузер. Войди в нужный аккаунт rabota.by,")
        print("дождись полной загрузки личного кабинета, затем закрой окно браузера.\n")

        subprocess.run(
            [
                sys.executable,
                "-m",
                "playwright",
                "codegen",
                f"--save-storage={auth_path}",
                "https://rabota.by/",
            ]
        )

        if auth_path.exists():
            print(f"\nФайл авторизации сохранён: {auth_path}")
        else:
            print(
                "\nФайл авторизации не найден. "
                "Похоже, браузер был закрыт раньше времени."
            )
    else:
        print(
            "\nНе забудь создать rabota_auth.json вручную командой:\n"
            f'py -3.12 -m playwright codegen --save-storage="{auth_path}" https://rabota.by/'
        )

    print("\n" + "=" * 60)
    print("ПРОЕКТ ГОТОВ")
    print(f"Папка: {project_dir}")
    print(f"Запуск: {run_bat_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
