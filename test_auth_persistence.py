"""Authentication and SQLite persistence tests using a disposable database."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import database
import test_suite


class AuthPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tempdir = tempfile.TemporaryDirectory(prefix="codementor_auth_persistence_")
        cls._original_db_path = database.DB_PATH
        cls._db_path = str(Path(cls._tempdir.name) / "accounts.sqlite3")
        database.DB_PATH = cls._db_path
        database.init_db()
        test_suite.TEST_SESSION_TOKENS.clear()

    @classmethod
    def tearDownClass(cls):
        database.DB_PATH = cls._original_db_path
        test_suite.TEST_SESSION_TOKENS.clear()
        cls._tempdir.cleanup()

    def registration_payload(self, username=None):
        key = uuid4().hex[:10]
        return {
            "email": f"{key}@example.test",
            "username": username or f"student_{key}",
            "display_name": "Persistence Test Learner",
            "password": "TestOnly-Passphrase-2026",
            "password_confirm": "TestOnly-Passphrase-2026",
        }

    @staticmethod
    def register(payload):
        return test_suite.invoke_http("POST", "/api/auth/register", payload, authenticated=False)

    @staticmethod
    def login(username, password):
        return test_suite.invoke_http("POST", "/api/auth/login", {
            "username": username,
            "password": password,
        }, authenticated=False)

    def test_signup_then_login_succeeds(self):
        payload = self.registration_payload()
        status, created = self.register(payload)
        self.assertEqual(status, 201)
        self.assertEqual(created["user"]["username"], payload["username"].lower())

        login_status, logged_in = self.login(payload["username"], payload["password"])
        self.assertEqual(login_status, 200)
        self.assertEqual(logged_in["user"]["id"], created["user"]["id"])

    def test_same_username_cannot_register_twice_with_different_email(self):
        payload = self.registration_payload("CaseSensitiveLearner")
        first_status, _ = self.register(payload)
        duplicate = {**payload, "email": f"other-{uuid4().hex[:10]}@example.test", "username": "CASESENSITIVELEARNER"}
        second_status, result = self.register(duplicate)
        self.assertEqual(first_status, 201)
        self.assertEqual(second_status, 409)
        self.assertIn("already exists", result["error"])

    def test_incorrect_password_is_rejected(self):
        payload = self.registration_payload()
        status, _ = self.register(payload)
        wrong_status, wrong_result = self.login(payload["username"], "Not-The-Test-Password")
        self.assertEqual(status, 201)
        self.assertEqual(wrong_status, 401)
        self.assertEqual(wrong_result["error"], "Invalid username or password")

    def test_signup_and_login_normalize_username_consistently(self):
        payload = self.registration_payload("  Mixed.Name_Student  ")
        payload["email"] = f"  {payload['email'].upper()}  "
        status, created = self.register(payload)
        login_status, logged_in = self.login("  MIXED.NAME_STUDENT  ", payload["password"])
        self.assertEqual(status, 201)
        self.assertEqual(created["user"]["username"], "mixed.name_student")
        self.assertEqual(created["user"]["email"], payload["email"].strip().lower())
        self.assertEqual(login_status, 200)
        self.assertEqual(logged_in["user"]["id"], created["user"]["id"])

    def test_account_survives_database_reopen_and_schema_initialization(self):
        payload = self.registration_payload()
        status, created = self.register(payload)
        self.assertEqual(status, 201)

        conn = database.get_connection()
        conn.close()
        database.init_db()
        login_status, logged_in = self.login(payload["username"], payload["password"])
        self.assertEqual(login_status, 200)
        self.assertEqual(logged_in["user"]["id"], created["user"]["id"])

    def test_account_survives_simulated_process_restart_on_same_database_path(self):
        payload = self.registration_payload()
        status, created = self.register(payload)
        self.assertEqual(status, 201)

        restart_script = r'''import contextlib, io, json, sys
request = json.loads(sys.stdin.read())
with contextlib.redirect_stdout(io.StringIO()):
    import database
    database.init_db()
    import test_suite
status, result = test_suite.invoke_http("POST", "/api/auth/login", {
    "username": request["username"], "password": request["password"]
}, authenticated=False)
print(json.dumps({"status": status, "user_id": result.get("user", {}).get("id")}))
'''
        child_env = os.environ.copy()
        child_env.update({
            "DATABASE_PATH": self._db_path,
            "AI_PROVIDER": "local",
            "OPENAI_API_KEY": "",
            "GROQ_API_KEY": "",
        })
        result = subprocess.run(
            [sys.executable, "-c", restart_script],
            input=json.dumps({"username": payload["username"], "password": payload["password"]}),
            text=True,
            capture_output=True,
            cwd=str(Path(__file__).resolve().parent),
            env=child_env,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, "restart subprocess failed")
        restarted_login = json.loads(result.stdout)
        self.assertEqual(restarted_login["status"], 200)
        self.assertEqual(restarted_login["user_id"], created["user"]["id"])


if __name__ == "__main__":
    unittest.main()
