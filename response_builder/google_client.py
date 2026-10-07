from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .common import SetupError, atomic_json, column_name, file_lock, sheet_range


DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets.readonly"]
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MIME = "application/vnd.google-apps.spreadsheet"
EXTRA_FIELDS = ["key", "response_id", "vacancy_id", "vacancy_name", "vacancy_url",
                "received_at", "collected_at", "email", "resume_text"]
HEADERS = ["Ключ отклика", "ID отклика", "ID вакансии", "Вакансия", "URL вакансии",
           "Дата отклика (UTC)", "Дата сбора (UTC)", "Email", "Текст резюме"]


def execute(request):
    try:
        return request.execute(num_retries=0)
    except HttpError as error:
        raise SetupError(f"Google API: HTTP {error.resp.status}. Проверьте права, квоты и API в Google Cloud.") from None
    except Exception:
        raise SetupError("Google API недоступен или результат запроса не подтверждён. Проверьте сеть и повторите проверку.") from None


def service_credentials(shared: Path):
    path = shared / "google_service_account.json"
    if not path.exists():
        path = shared / "google_credentials.json"  # supported existing filename, only in _shared
    if not path.is_file():
        raise SetupError("Поместите Google service-account credentials в _shared/google_service_account.json.")
    try:
        return ServiceCredentials.from_service_account_file(str(path), scopes=SHEETS_SCOPES)
    except Exception:
        raise SetupError("Не удалось прочитать общие service-account credentials.") from None


def oauth_credentials(shared: Path):
    from google_auth_oauthlib.flow import InstalledAppFlow
    shared.mkdir(parents=True, exist_ok=True)
    token = shared / "google_oauth_token.json"
    client = shared / "google_oauth_client.json"
    with file_lock(shared / ".google-oauth.lock"):
        credentials = None
        if token.exists():
            try:
                credentials = UserCredentials.from_authorized_user_file(str(token))
                if not credentials.has_scopes(DRIVE_SCOPES):
                    credentials = None
            except Exception:
                credentials = None
        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as error:
                details = error.args[1] if len(error.args) > 1 and isinstance(error.args[1], dict) else {}
                if details.get("error") == "invalid_grant":
                    credentials = None
                else:
                    raise SetupError("Не удалось обновить Google OAuth. Проверьте сеть/доступ; сохранённый token не заменён.") from None
            except Exception:
                raise SetupError("Не удалось связаться с Google OAuth. Проверьте сеть; сохранённый token не заменён.") from None
        if not credentials or not credentials.valid:
            if not client.is_file():
                raise SetupError("Нужен OAuth client типа Desktop app: _shared/google_oauth_client.json.")
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(client), DRIVE_SCOPES)
                credentials = flow.run_local_server(
                    port=0, access_type="offline", prompt="consent",
                    authorization_prompt_message="Откройте браузер для входа в Google (ссылка не выводится в лог).",
                    success_message="Вход выполнен. Можно закрыть вкладку.")
            except Exception:
                raise SetupError("Не завершён интерактивный вход Google OAuth. Проверьте Desktop client и доступ приложения.") from None
        if not credentials.refresh_token:
            raise SetupError("Google не выдал refresh token; повторите OAuth-вход с offline-доступом.")
        atomic_json(token, json.loads(credentials.to_json()))
        return credentials


def quote_query(value):
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    def __init__(self, shared):
        credentials = oauth_credentials(shared)
        self.api = build("drive", "v3", credentials=credentials, cache_discovery=False)
        self.sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)

    def files(self, query):
        result, token = [], None
        while True:
            response = execute(self.api.files().list(q="trashed=false and (" + query + ")",
                spaces="drive", pageSize=100, pageToken=token,
                fields="nextPageToken,incompleteSearch,files(id,name,mimeType,appProperties)", supportsAllDrives=True,
                includeItemsFromAllDrives=True))
            if response.get("incompleteSearch"):
                raise SetupError("Google Drive вернул неполный поиск. Проверка конфликтов имени не завершена.")
            result.extend(response.get("files", []))
            token = response.get("nextPageToken")
            if not token:
                return result

    def named(self, name):
        return self.files(f"name='{quote_query(name)}' and mimeType='{MIME}'")

    def template(self, identifier):
        file = execute(self.api.files().get(fileId=identifier, fields="id,name,mimeType,trashed", supportsAllDrives=True))
        if file.get("trashed") or file.get("mimeType") != MIME:
            raise SetupError("Шаблон не является доступной Google-таблицей.")
        if file["name"] != "Тест ИИ":
            raise SetupError("Указанная таблица не называется «Тест ИИ». Копирование остановлено.")
        metadata = execute(self.sheets.spreadsheets().get(spreadsheetId=identifier,
            fields="spreadsheetId,sheets(properties)"))
        return metadata["sheets"]

    def attempt_files(self, attempt):
        return self.files("appProperties has { key='response_builder_attempt' and value='" + quote_query(attempt) + "' }")

    def copy(self, template_id, name, attempt, allow_create=True):
        recovered = self.attempt_files(attempt)
        if len(recovered) > 1:
            raise SetupError("Для попытки создания найдено несколько копий. Автоматическое продолжение запрещено.")
        if recovered:
            file = recovered[0]
            if file.get("mimeType") != MIME or file.get("name") != name:
                raise SetupError("Копия попытки создания не соответствует проекту.")
            if file.get("appProperties", {}).get("response_builder_template") != template_id:
                raise SetupError("Не подтверждён исходный шаблон копии этой попытки.")
            return file["id"]
        if not allow_create:
            raise SetupError("Результат предыдущего копирования Drive неизвестен, копия пока не найдена. "
                             "Новый запрос не отправлен. Проверьте Drive; повторите --resume позже. "
                             "Если копии действительно нет, используйте --resume с --retry-copy и явным подтверждением.")
        if self.named(name):
            raise SetupError("Таблица с выбранным названием уже существует. Вернитесь к выбору другого имени.")
        # NEVER automatically repeat files.copy after an uncertain response.
        return execute(self.api.files().copy(fileId=template_id,
            body={"name": name, "appProperties": {"response_builder_attempt": attempt,
                  "response_builder_template": template_id}},
            fields="id", supportsAllDrives=True))["id"]

    def grant_service_account(self, identifier, email):
        token, matches = None, []
        while True:
            result = execute(self.api.permissions().list(fileId=identifier, pageToken=token,
                fields="nextPageToken,permissions(id,emailAddress,role)", supportsAllDrives=True))
            matches.extend(p for p in result.get("permissions", []) if p.get("emailAddress") == email)
            token = result.get("nextPageToken")
            if not token:
                break
        if matches:
            if matches[0].get("role") not in {"writer", "owner", "organizer", "fileOrganizer"}:
                execute(self.api.permissions().update(fileId=identifier, permissionId=matches[0]["id"],
                    body={"role": "writer"}, supportsAllDrives=True))
        else:
            execute(self.api.permissions().create(fileId=identifier,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=False, supportsAllDrives=True, fields="id"))


class Sheets:
    def __init__(self, shared: Path, config: dict, api=None):
        if api is None:
            api = build("sheets", "v4", credentials=service_credentials(shared), cache_discovery=False)
        self.api = api
        self.identifier = config["spreadsheet_id"]
        self.layout = config["layout"]
        self.sheet_id = self.layout["sheet_id"]
        self.title = self.layout["sheet_title"]
        self.columns = self.layout["columns"]
        self.first_row = self.layout["first_data_row"]

    def metadata(self):
        result = execute(self.api.spreadsheets().get(spreadsheetId=self.identifier, fields="sheets(properties)"))
        matches = [s["properties"] for s in result["sheets"] if s["properties"]["sheetId"] == self.sheet_id]
        if len(matches) != 1 or matches[0]["title"] != self.title:
            raise SetupError("Лист проекта удалён или переименован. Автозапись запрещена.")
        return matches[0]

    def batch(self, requests):
        execute(self.api.spreadsheets().batchUpdate(spreadsheetId=self.identifier, body={"requests": requests}))

    def cell_update(self, row, column, value, formula=False):
        return {"updateCells": {"range": {"sheetId": self.sheet_id, "startRowIndex": row - 1,
            "endRowIndex": row, "startColumnIndex": column, "endColumnIndex": column + 1},
            "rows": [{"values": [{"userEnteredValue": {"formulaValue" if formula else "stringValue": str(value)}}]}],
            "fields": "userEnteredValue"}}

    def initialize(self):
        metadata = self.metadata()
        needed = max(self.columns.values()) + 1
        current = metadata["gridProperties"]["columnCount"]
        if current < needed:
            self.batch([{"appendDimension": {"sheetId": self.sheet_id, "dimension": "COLUMNS", "length": needed - current}}])
        # Only the new copy is modified. Original headers, widths, filters and sheets stay intact.
        header_row = self.layout["header_row"]
        existing = self.values(header_row, header_row)
        for field, label in zip(EXTRA_FIELDS, HEADERS):
            column = self.columns[field]
            old = self.value_at(existing[0] if existing else [], column)
            if old not in ("", label):
                raise SetupError("Технический столбец уже занят: инициализация остановлена.")
        self.batch([self.cell_update(header_row, self.columns[field], label) for field, label in zip(EXTRA_FIELDS, HEADERS)])

    def values(self, start=None, end=None):
        final = column_name(max(self.columns.values()))
        cells = f"A{start or ''}:{final}{end or ''}"
        result = execute(self.api.spreadsheets().values().get(spreadsheetId=self.identifier,
            range=sheet_range(self.title, cells), valueRenderOption="UNFORMATTED_VALUE"))
        return result.get("values", [])

    @staticmethod
    def value_at(row, column):
        return row[column] if len(row) > column else ""

    def check(self):
        self.metadata()
        headers = self.values(self.layout["header_row"], self.layout["header_row"])
        for field, label in zip(EXTRA_FIELDS, HEADERS):
            if not headers or self.value_at(headers[0], self.columns[field]) != label:
                raise SetupError("Не подтверждены технические столбцы проекта. Выполните возобновление создания.")
        return self.index()

    def index(self):
        result = {}
        for row_number, row in enumerate(self.values(), 1):
            if row_number < self.first_row:
                continue
            key = str(self.value_at(row, self.columns["key"])).strip()
            if key:
                if key in result:
                    raise SetupError("В таблице уже есть повтор ключа отклика. Нужна ручная проверка.")
                try:
                    pair = json.loads(key)
                    if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(x, str) for x in pair):
                        raise ValueError
                except ValueError:
                    raise SetupError("Повреждён технический ключ отклика в таблице.") from None
                result[key] = row_number
        return result

    def vacant(self, row):
        rows = self.values(row, row)
        cells = rows[0] if rows else []
        return not any(str(self.value_at(cells, column)).strip() for column in self.columns.values())

    def choose_row(self, reserved):
        metadata = self.metadata()
        row_count = metadata["gridProperties"]["rowCount"]
        data = self.values()
        occupied = {n for n, row in enumerate(data, 1)
                    if any(str(self.value_at(row, c)).strip() for c in self.columns.values())}
        # Inspect formatting/validation to reuse the template's prepared rows before appending.
        grids = execute(self.api.spreadsheets().get(spreadsheetId=self.identifier, includeGridData=True,
            ranges=[sheet_range(self.title, f"A{self.first_row}:{column_name(max(self.columns.values()))}{row_count}")],
            fields="sheets(data(startRow,rowData(values(userEnteredFormat,dataValidation,userEnteredValue))))"))
        prepared = set()
        for sheet in grids.get("sheets", []):
            for grid in sheet.get("data", []):
                for offset, row in enumerate(grid.get("rowData", [])):
                    number = grid.get("startRow", 0) + offset + 1
                    if any(cell for cell in row.get("values", [])):
                        prepared.add(number)
        for row in sorted(prepared):
            if row >= self.first_row and row not in occupied | reserved:
                return row
        return max(occupied | reserved | prepared | {self.first_row - 1}) + 1

    def write(self, row, payload):
        metadata = self.metadata()
        if row > metadata["gridProperties"]["rowCount"]:
            self.batch([{"appendDimension": {"sheetId": self.sheet_id, "dimension": "ROWS",
                "length": row - metadata["gridProperties"]["rowCount"]}}])
            # Copy format/validation, never template values or formulas.
            source = {"sheetId": self.sheet_id, "startRowIndex": self.first_row - 1,
                      "endRowIndex": self.first_row, "startColumnIndex": 0,
                      "endColumnIndex": max(self.columns.values()) + 1}
            target = dict(source, startRowIndex=row - 1, endRowIndex=row)
            self.batch([{"copyPaste": {"source": source, "destination": target, "pasteType": kind}}
                        for kind in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION")])
        if not self.vacant(row):
            raise SetupError("Закреплённая строка занята. Перезапись существующих данных запрещена.")
        payload = dict(payload)
        payload.setdefault("collected_at", datetime.now(timezone.utc).isoformat())
        updates = []
        for field, column in self.columns.items():
            if field == "name":
                # A native text link avoids locale-dependent HYPERLINK separators and formula injection.
                update = self.cell_update(row, column, payload.get("name", ""))
                cell = update["updateCells"]["rows"][0]["values"][0]
                cell["textFormatRuns"] = [{"startIndex": 0, "format": {"link": {"uri": payload["resume_url"]}}}]
                update["updateCells"]["fields"] = "userEnteredValue,textFormatRuns"
                updates.append(update)
            else:
                # stringValue prevents names/text beginning with '=' from becoming formulas.
                updates.append(self.cell_update(row, column, payload.get(field, "")))
        # The key and all data are written in the same atomic Sheets batchUpdate.
        self.batch(updates)
        rows = self.values(row, row)
        if not rows or self.value_at(rows[0], self.columns["key"]) != payload["key"]:
            raise SetupError("Google не подтвердил ключ записанного отклика. Результат требует сверки.")
