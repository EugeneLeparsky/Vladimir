import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from response_builder.common import SetupError
from response_builder.credentials_setup import ensure_google_credentials, hidden_input, import_credentials, validate_oauth_client


class CredentialImportTests(unittest.TestCase):
    def test_hidden_input_refuses_getpass_echo_fallback(self):
        import getpass
        import warnings
        def fallback(prompt):
            warnings.warn("Cannot disable echo", getpass.GetPassWarning)
            raise AssertionError("must stop before fallback reads any input")
        with patch("getpass.getpass", side_effect=fallback), self.assertRaises(SetupError):
            hidden_input("Google JSON: ")

    def test_file_import_is_central_and_secret_is_not_logged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "_shared"
            shared.mkdir()
            source = root / "download.json"
            source.write_text(json.dumps({"secret": "DO-NOT-LOG"}))
            output = io.StringIO()
            with patch("builtins.input", return_value="1"), patch("response_builder.credentials_setup.choose_json_file", return_value=source), contextlib.redirect_stdout(output):
                path = import_credentials(shared, "credentials.json", "Google", lambda data: None)
            self.assertEqual(path.parent, shared)
            self.assertEqual(json.loads(path.read_text())["secret"], "DO-NOT-LOG")
            self.assertNotIn("DO-NOT-LOG", output.getvalue())
            self.assertEqual(json.loads(source.read_text())["secret"], "DO-NOT-LOG")

    def test_hidden_json_input_and_existing_valid_file_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory)
            with patch("builtins.input", return_value="2"), patch("getpass.getpass", return_value='{"secret":"DO-NOT-LOG"}'), contextlib.redirect_stdout(io.StringIO()):
                path = import_credentials(shared, "credentials.json", "Google", lambda data: None)
            with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                self.assertEqual(import_credentials(shared, "credentials.json", "Google", lambda data: None), path)

    def test_failed_replacement_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory)
            old = shared / "credentials.json"
            old.write_text('{"old":"keep"}')
            original = old.read_bytes()
            def reject(data): raise SetupError("Invalid credentials")
            with patch("builtins.input", side_effect=["ДА", "2"]), patch("getpass.getpass", return_value='{"new":"SECRET"}'), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SetupError):
                    import_credentials(shared, old.name, "Google", reject)
            self.assertEqual(old.read_bytes(), original)

    def test_cancel_does_not_create_credential_file(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory)
            with patch("builtins.input", return_value=""), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SetupError):
                    import_credentials(shared, "credentials.json", "Google", lambda data: None)
            self.assertFalse((shared / "credentials.json").exists())

    def test_oauth_rejects_web_client_and_custom_token_destination(self):
        for data in [{"web": {"client_secret": "SECRET"}}, {"installed": {"client_id": "x", "client_secret": "SECRET", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://example.com/token", "redirect_uris": ["http://localhost"]}}]:
            with self.assertRaises(SetupError) as error:
                validate_oauth_client(data)
            self.assertNotIn("SECRET", str(error.exception))

    def test_project_launcher_needs_no_oauth_and_reuses_legacy_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory)
            legacy = shared / "google_credentials.json"
            legacy.write_text('{}')
            with patch("response_builder.credentials_setup.validate_service_account"), patch("builtins.input", side_effect=AssertionError("must not prompt")):
                ensure_google_credentials(shared, need_oauth=False)
            self.assertFalse((shared / "google_oauth_client.json").exists())
            self.assertFalse((shared / "google_service_account.json").exists())


if __name__ == "__main__":
    unittest.main()
