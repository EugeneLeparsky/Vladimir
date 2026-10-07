from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from response_builder.collector import process_queue, validate_config
from response_builder.common import SetupError, atomic_json, file_lock, project_name, safe_failure, sheet_range
from response_builder.create_project import create, create_launchers
from response_builder.google_client import Drive, EXTRA_FIELDS, HEADERS, Sheets
from response_builder.schema import response_record, scalar_paths
from response_builder.state import State


def fixture(vacancy_id="100", response_id="response-1", resume_id="resume-1"):
    # Synthetic schema, NOT a claim about live rabota.by field names.
    vacancy = {"vacancy_id": vacancy_id, "name": "Test vacancy", "url": "https://rabota.by/vacancy/" + vacancy_id}
    candidate = {"resumeId": resume_id, "response": {"id": response_id, "received": "2026-09-01T10:30:00+03:00"},
                 "origin": "incoming", "hasNewMessages": False,
                 "negotiationLinks": {"changeTopic": {"defaultLink": "https://rabota.by/example?r=abcdef123"}}}
    schema = {"confirmed": True, "response_id_path": "response.id", "received_at_path": "response.received",
              "time_encoding": "iso8601", "incoming_path": "origin", "incoming_values": ["incoming"]}
    return response_record(candidate, vacancy, schema), candidate, vacancy, schema


def configuration():
    columns = {"name": 1, "age": 2, "phone": 3, "resume_id": 25}
    columns.update({name: 26 + i for i, name in enumerate(EXTRA_FIELDS)})
    record, _, vacancy, schema = fixture()
    return {"builder_version": 1, "status": "ready", "spreadsheet_id": "copy-id", "template_id": "template-id",
            "vacancies": [vacancy], "rabota_schema": schema,
            "layout": {"sheet_id": 7, "sheet_title": "Отклики 'команды'", "first_data_row": 2,
                       "header_row": 1, "columns": columns}}


class Request:
    def __init__(self, function):
        self.function = function

    def execute(self, **_):
        return self.function()


class MemoryAPI:
    """Fake server exercising real Sheets requests including a lost write acknowledgement."""
    def __init__(self, config):
        self.config = config
        self.cells = {(2, 0): "template selector", (1, 1): "ФИО"}
        self.row_count = 20
        self.column_count = 26
        self.data_writes = 0
        self.lose_ack = False
        self.requests = []
        self.template_touched = False

    def spreadsheets(self):
        return self

    def values(self):
        return ValuesAPI(self)

    def get(self, spreadsheetId, **kwargs):
        self.assert_copy(spreadsheetId)
        if kwargs.get("includeGridData"):
            return Request(lambda: {"sheets": [{"data": [{"startRow": 1,
                "rowData": [{"values": [{"userEnteredFormat": {"backgroundColor": {"red": 1}}}]}]}]}]})
        return Request(lambda: {"sheets": [{"properties": {"sheetId": 7,
            "title": self.config["layout"]["sheet_title"],
            "gridProperties": {"rowCount": self.row_count, "columnCount": self.column_count}}}]})

    def assert_copy(self, spreadsheet_id):
        if spreadsheet_id == "template-id":
            self.template_touched = True
            raise AssertionError("template modified/read by writer")
        assert spreadsheet_id == "copy-id"

    def batchUpdate(self, spreadsheetId, body):
        self.assert_copy(spreadsheetId)
        def apply():
            is_data = False
            self.requests.extend(body["requests"])
            for request in body["requests"]:
                if "appendDimension" in request:
                    info = request["appendDimension"]
                    if info["dimension"] == "COLUMNS":
                        self.column_count += info["length"]
                    else:
                        self.row_count += info["length"]
                if "updateCells" in request:
                    update = request["updateCells"]
                    row = update["range"]["startRowIndex"] + 1
                    col = update["range"]["startColumnIndex"]
                    self.cells[row, col] = update["rows"][0]["values"][0]["userEnteredValue"]["stringValue"]
                    is_data |= row >= 2
            if is_data:
                self.data_writes += 1
                if self.lose_ack:
                    self.lose_ack = False
                    raise TimeoutError("access_token=DO-NOT-LOG")
            return {}
        return Request(apply)


class ValuesAPI:
    def __init__(self, api):
        self.api = api

    def get(self, spreadsheetId, range, **_):
        import re
        self.api.assert_copy(spreadsheetId)
        cell_range = range.rsplit("!", 1)[1]
        match = re.fullmatch(r"A(\d*):[A-Z]+(\d*)", cell_range)
        start = int(match[1]) if match[1] else 1
        end = int(match[2]) if match[2] else max((r for r, _ in self.api.cells), default=1)
        def result():
            rows = [[self.api.cells.get((row, col), "") for col in range_builtin(self.api.column_count)]
                    for row in range_builtin(start, end + 1)]
            while rows and not any(rows[-1]):
                rows.pop()
            return {"values": rows}
        return Request(result)


range_builtin = range


class Browser:
    def __init__(self):
        self.calls = []
        self.fail_first = False

    def extract(self, record):
        self.calls.append(record["key"])
        if self.fail_first and len(self.calls) == 1:
            raise TimeoutError("Cookie: SECRET")
        return dict(record, name="=untrusted name", age="30", phone="", email="", resume_text="=untrusted text")


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        for folder in ("state", "logs", "errors"):
            (self.project / folder).mkdir()
        self.config = configuration()
        self.api = MemoryAPI(self.config)
        self.sheets = Sheets(self.project, self.config, api=self.api)
        self.sheets.initialize()
        self.state = State(self.project / "state/responses.sqlite3")
        self.browser = Browser()

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def run_queue(self, **kwargs):
        return process_queue(self.project, self.state, self.sheets, self.browser, pause=lambda _: None, **kwargs)

    def test_same_resume_in_two_vacancies_and_repeat_run(self):
        for vacancy in ("100", "200"):
            self.state.enqueue(fixture(vacancy)[0])
        self.assertEqual(self.run_queue()["saved"], 2)
        self.assertEqual(self.api.data_writes, 2)
        self.assertEqual(self.run_queue()["saved"], 0)
        self.assertEqual(len(self.sheets.index()), 2)

    def test_two_responses_to_same_vacancy_are_distinct(self):
        for response in ("first", "second"):
            self.state.enqueue(fixture(response_id=response)[0])
        self.assertEqual(self.run_queue()["saved"], 2)

    def test_lost_google_acknowledgement_is_reconciled_without_duplicate(self):
        record = fixture()[0]
        self.state.enqueue(record)
        self.api.lose_ack = True
        self.assertEqual(self.run_queue()["errors"], 1)
        self.assertEqual(self.api.data_writes, 1)
        # Restart the process, not just the loop.
        self.state.close()
        self.state = State(self.project / "state/responses.sqlite3")
        self.run_queue(retry=True)
        item = self.state.db.execute("SELECT * FROM responses").fetchone()
        self.assertEqual(item["status"], "saved")
        self.assertEqual(self.api.data_writes, 1)
        self.assertEqual(len(self.browser.calls), 1)

    def test_crash_after_reservation_resumes_exact_row_and_payload(self):
        record = fixture()[0]
        self.state.enqueue(record)
        self.state.reserve(record["key"], 2, dict(record, name="Test"))
        self.state.close()
        self.state = State(self.project / "state/responses.sqlite3")
        self.assertEqual(self.run_queue()["saved"], 1)
        self.assertEqual(self.sheets.index()[record["key"]], 2)
        self.assertFalse(self.browser.calls)

    def test_single_extraction_error_continues_and_retry_works(self):
        self.state.enqueue(fixture(response_id="a")[0])
        self.state.enqueue(fixture(response_id="b")[0])
        self.browser.fail_first = True
        result = self.run_queue()
        self.assertEqual((result["saved"], result["errors"]), (1, 1))
        self.assertEqual(self.run_queue(retry=True)["saved"], 1)
        self.assertEqual(len(self.sheets.index()), 2)
        self.assertNotIn("SECRET", (self.project / "errors/records.jsonl").read_text())

    def test_test_mode_never_attempts_second_response_after_error(self):
        self.state.enqueue(fixture(response_id="a")[0])
        self.state.enqueue(fixture(response_id="b")[0])
        self.browser.fail_first = True
        result = self.run_queue(test=True)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(len(self.browser.calls), 1)
        self.assertEqual(len(self.state.items()), 1)

    def test_occupied_reserved_row_is_not_overwritten(self):
        record = fixture()[0]
        self.state.enqueue(record)
        self.state.reserve(record["key"], 2, dict(record, name="Test"))
        self.api.cells[2, 1] = "User data"
        self.assertEqual(self.run_queue()["errors"], 1)
        self.assertEqual(self.api.cells[2, 1], "User data")
        self.assertEqual(self.api.data_writes, 0)

    def test_template_values_preserved_and_data_is_not_formula(self):
        self.state.enqueue(fixture()[0])
        self.run_queue()
        self.assertEqual(self.api.cells[2, 0], "template selector")
        self.assertEqual(self.api.cells[1, 1], "ФИО")
        self.assertEqual(self.api.cells[2, 1], "=untrusted name")
        self.assertFalse(self.api.template_touched)
        self.assertTrue(any("textFormatRuns" in update.get("updateCells", {}).get("fields", "")
                            for update in self.api.requests))

    def test_missing_saved_sheet_key_blocks_rewrite(self):
        record = fixture()[0]
        self.state.enqueue(record)
        self.run_queue()
        del self.api.cells[2, self.config["layout"]["columns"]["key"]]
        with self.assertRaises(SetupError):
            self.run_queue()
        self.assertEqual(self.api.data_writes, 1)

    def test_conflicting_response_identity_is_not_silently_deduplicated(self):
        self.state.enqueue(fixture()[0])
        with self.assertRaises(SetupError):
            self.state.enqueue(fixture(resume_id="different-resume")[0])


class SchemaTests(unittest.TestCase):
    def test_read_messages_are_still_collected(self):
        record, candidate, _, _ = fixture()
        self.assertFalse(candidate["hasNewMessages"])
        self.assertIsNotNone(record)
        self.assertEqual(record["received_at"], "2026-09-01T07:30:00+00:00")

    def test_missing_response_id_is_blocker_not_synthetic_key(self):
        _, candidate, vacancy, schema = fixture()
        del candidate["response"]
        with self.assertRaises(SetupError):
            response_record(candidate, vacancy, schema)

    def test_resume_id_is_not_accepted_as_response_id(self):
        _, candidate, vacancy, schema = fixture()
        schema["response_id_path"] = "resumeId"
        with self.assertRaises(SetupError):
            response_record(candidate, vacancy, schema)

    def test_outgoing_is_excluded(self):
        _, candidate, vacancy, schema = fixture()
        candidate["origin"] = "outgoing"
        self.assertIsNone(response_record(candidate, vacancy, schema))

    def test_time_without_zone_is_not_guessed(self):
        _, candidate, vacancy, schema = fixture()
        candidate["response"]["received"] = "2026-09-01T10:30:00"
        with self.assertRaises(SetupError):
            response_record(candidate, vacancy, schema)

    def test_probe_does_not_expose_values_or_secret_fields(self):
        self.assertEqual(scalar_paths({"response": {"id": "private-value"}, "access_token": "SECRET"}), {"response.id": "str"})


class ProtectionTests(unittest.TestCase):
    def test_reserved_windows_names_and_path_escape_rejected(self):
        for name in ("..", "../other", "CON", "nul.txt", "_shared", "demo&erase", "test%PATH%", "demo."):
            with self.subTest(name=name), self.assertRaises(SetupError):
                project_name(name)
        self.assertEqual(project_name("Команда 2026"), "Команда 2026")

    def test_unready_project_and_template_destination_rejected(self):
        config = configuration()
        config["status"] = "creation_failed"
        with self.assertRaises(SetupError):
            validate_config(config)
        config["status"] = "ready"
        config["spreadsheet_id"] = config["template_id"]
        with self.assertRaises(SetupError):
            validate_config(config)

    def test_duplicate_drive_name_does_not_copy(self):
        drive = object.__new__(Drive)
        drive.attempt_files = lambda _: []
        drive.named = lambda _: [{"id": "existing"}]
        with self.assertRaises(SetupError):
            drive.copy("template-id", "Existing", "attempt")

    def test_uncertain_drive_copy_recovers_by_attempt_without_another_copy(self):
        drive = object.__new__(Drive)
        drive.attempt_files = lambda _: [{"id": "copy-id", "name": "New", "mimeType": "application/vnd.google-apps.spreadsheet",
                                         "appProperties": {"response_builder_template": "template-id"}}]
        self.assertEqual(drive.copy("template-id", "New", "attempt"), "copy-id")

    def test_uncertain_drive_copy_not_repeated_when_no_copy_yet_visible(self):
        drive = object.__new__(Drive)
        drive.attempt_files = lambda _: []
        with self.assertRaises(SetupError):
            drive.copy("template-id", "New", "attempt", allow_create=False)

    def test_copy_id_retained_when_creation_check_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "New"
            project.mkdir()
            config = configuration()
            config.update(status="creating", project_name="New", creation_attempt="attempt")
            config.pop("spreadsheet_id")
            atomic_json(project / "project_config.json", config)
            class FakeDrive:
                def __init__(self, shared): pass
                def template(self, identifier): return []
                def copy(self, *args, **kwargs): return "copy-id"
                def grant_service_account(self, *args): raise SetupError("No access")
            class Credentials:
                service_account_email = "test@example.com"
            with patch("response_builder.create_project.Drive", FakeDrive), patch("response_builder.create_project.service_credentials", return_value=Credentials()):
                with self.assertRaises(SetupError):
                    create(root, resume="New")
            saved = json.loads((project / "project_config.json").read_text())
            self.assertEqual(saved["status"], "creation_failed")
            self.assertEqual(saved["spreadsheet_id"], "copy-id")
            self.assertFalse((project / "start_project.bat").exists())

    def test_existing_folder_cancel_does_not_change_user_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "Existing").mkdir()
            sentinel = root / "Existing/user.txt"
            sentinel.write_text("keep")
            with patch("response_builder.create_project.service_credentials"), patch("response_builder.create_project.Drive"), patch("builtins.input", side_effect=["Existing", "2"]), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SetupError):
                    create(root)
            self.assertEqual(sentinel.read_text(), "keep")
            self.assertEqual([p.name for p in sentinel.parent.iterdir()], ["user.txt"])

    def test_launchers_repeatable_but_user_edits_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            create_launchers(root)
            create_launchers(root)
            self.assertIn("--mode retry", (root / "start_project.bat").read_text())
            path = root / "collect_responses.py"
            path.write_text("user code")
            with self.assertRaises(SetupError):
                create_launchers(root)
            self.assertEqual(path.read_text(), "user code")

    def test_lock_blocks_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".lock"
            with file_lock(path):
                with self.assertRaises(SetupError):
                    with file_lock(path):
                        pass
            with file_lock(path):
                pass

    def test_sheet_title_escaped_and_remote_error_redacted(self):
        self.assertEqual(sheet_range("a'b", "A:Z"), "'a''b'!A:Z")
        self.assertNotIn("SECRET", safe_failure(RuntimeError("Authorization: SECRET")))


if __name__ == "__main__":
    unittest.main()
