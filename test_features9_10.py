"""Isolated tests for the standalone Features 9/10 read-only dashboard."""
import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date
from http.server import ThreadingHTTPServer

import database
import server as feature9_10
import server


def make_db(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript("""
    CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT, display_name TEXT);
    CREATE TABLE auth_sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER, created_at INTEGER, expires_at INTEGER);
    CREATE TABLE problems(id INTEGER PRIMARY KEY, title TEXT, concept TEXT, difficulty TEXT, description TEXT);
    CREATE TABLE attempts(id INTEGER PRIMARY KEY, user_id INTEGER, problem_id INTEGER, custom_title TEXT,
      language TEXT, mistake_categories_json TEXT, hints_used INTEGER, is_solved INTEGER,
      solved_independently INTEGER, created_at TEXT, updated_at TEXT);
    CREATE TABLE hint_logs(id INTEGER PRIMARY KEY, attempt_id INTEGER, hint_type TEXT, context_json TEXT);
    """)
    c.execute("INSERT INTO users VALUES(1,'alice','Alice')")
    c.execute("INSERT INTO users VALUES(2,'bob','Bob')")
    problems = [(1,"Loop practice","Loops","Easy","Trace iterations"), (2,"Array practice","Arrays","Medium","Check an index")]
    c.executemany("INSERT INTO problems VALUES(?,?,?,?,?)", problems)
    return c


class Feature910Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "fixture.sqlite3")
        self.conn = make_db(self.path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_attempt(self, user, solved, day, categories, problem=1, explain=False, hints=0):
        cur = self.conn.execute("""INSERT INTO attempts
          (user_id,problem_id,custom_title,language,mistake_categories_json,hints_used,is_solved,
           solved_independently,created_at,updated_at) VALUES(?,?,NULL,'Java',?,?,?,?,?,?)""",
          (user, problem, json.dumps(categories), hints, int(solved), int(solved), day + " 10:00:00", day + " 10:00:00"))
        if explain:
            self.conn.execute("INSERT INTO hint_logs VALUES(NULL,?,'explain_fix',?)",
                              (cur.lastrowid, json.dumps({"evaluation_status":"correct"})))
        return cur.lastrowid

    def test_heatmap_status_and_thresholds_use_saved_evidence(self):
        today = date.today().isoformat()
        self.add_attempt(1, False, today, ["loop_logic"], explain=False, hints=2)
        self.add_attempt(1, True, today, ["loop_logic"], explain=True)
        self.add_attempt(1, True, today, ["loop_logic"], explain=True)
        self.add_attempt(1, True, today, ["loop_logic"], explain=True)
        self.add_attempt(2, False, today, ["array_indexing"], problem=2)
        result = feature9_10.build_overview(self.conn, 1, today=date.fromisoformat(today))
        loops = next(x for x in result["heatmap"] if x["id"] == "loops")
        arrays = next(x for x in result["heatmap"] if x["id"] == "arrays")
        self.assertEqual((loops["attempts"], loops["solved"], loops["accuracy"], loops["hints"]), (4, 3, 75, 2))
        self.assertEqual(loops["status"], "improving")
        self.assertEqual(arrays["attempts"], 0, "another user's attempt must not enter this heatmap")
        self.assertIn("strong", result["thresholds"])

    def test_skill_tree_requires_verified_solves_and_correct_explanation(self):
        today = date.today().isoformat()
        self.add_attempt(1, True, today, ["variable_scope"], explain=False)
        self.add_attempt(1, True, today, ["variable_scope"], explain=False)
        result = feature9_10.build_overview(self.conn, 1)
        skill = next(x for x in result["skill_tree"] if x["id"] == "variables")
        self.assertEqual(skill["status"], "in_progress")
        self.add_attempt(1, True, today, ["variable_scope"], explain=True)
        result = feature9_10.build_overview(self.conn, 1)
        skill = next(x for x in result["skill_tree"] if x["id"] == "variables")
        self.assertEqual(skill["status"], "completed")

    def test_empty_and_low_sample_data_are_never_called_strong(self):
        result = feature9_10.build_overview(self.conn, 1)
        self.assertEqual(result["total_attempts"], 0)
        self.assertEqual(result["study_plan"], [])
        self.assertTrue(all(t["status"] == "insufficient_data" for t in result["heatmap"]))

    def test_authenticated_api_is_owner_scoped_and_rejects_missing_session(self):
        today = date.today().isoformat()
        self.add_attempt(1, True, today, ["loop_logic"], explain=True)
        self.add_attempt(2, False, today, ["array_indexing"], problem=2)
        self.conn.execute("INSERT INTO auth_sessions VALUES(?,?,?,?)", (hashlib.sha256(b"alice-token").hexdigest(), 1, int(time.time()), int(time.time())+3600))
        self.conn.execute("INSERT INTO auth_sessions VALUES(?,?,?,?)", (hashlib.sha256(b"bob-token").hexdigest(), 2, int(time.time()), int(time.time())+3600))
        self.conn.commit()
        original = database.DB_PATH
        database.DB_PATH = self.path
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.CodeMentorHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            def get(token=None):
                client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
                headers = {"Cookie": "codementor_session="+token} if token else {}
                client.request("GET", "/api/features9-10/overview", headers=headers)
                response = client.getresponse()
                body = json.loads(response.read().decode())
                status = response.status
                client.close()
                return status, body
            self.assertEqual(get()[0], 401)
            status, alice = get("alice-token")
            self.assertEqual(status, 200)
            self.assertEqual(alice["total_attempts"], 1)
            self.assertEqual(next(t for t in alice["heatmap"] if t["id"] == "arrays")["attempts"], 0)
            status, bob = get("bob-token")
            self.assertEqual(status, 200)
            self.assertEqual(bob["total_attempts"], 1)
            self.assertEqual(next(t for t in bob["heatmap"] if t["id"] == "loops")["attempts"], 0)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            database.DB_PATH = original

    def test_main_server_serves_features9_10_inside_the_existing_app_and_session(self):
        today = date.today().isoformat()
        self.add_attempt(1, True, today, ["loop_logic"], explain=True)
        self.add_attempt(2, False, today, ["array_indexing"], problem=2)
        self.conn.execute("INSERT INTO auth_sessions VALUES(?,?,?,?)", (hashlib.sha256(b"main-alice-token").hexdigest(), 1, int(time.time()), int(time.time())+3600))
        self.conn.commit()
        original = database.DB_PATH
        database.DB_PATH = self.path
        httpd = server.ThreadedHTTPServer(("127.0.0.1", 0), server.CodeMentorHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            def get(token=None, path="/api/features9-10/overview"):
                client = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
                headers = {"Cookie": "codementor_session="+token} if token else {}
                client.request("GET", path, headers=headers)
                response = client.getresponse()
                body = response.read().decode("utf-8")
                status = response.status
                client.close()
                return status, body

            self.assertEqual(get()[0], 401)
            status, body = get("main-alice-token")
            data = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(data["total_attempts"], 1)
            self.assertEqual(next(t for t in data["heatmap"] if t["id"] == "arrays")["attempts"], 0)
            page_status, page = get("main-alice-token", "/")
            self.assertEqual(page_status, 200)
            self.assertIn('data-view="insights"', page)
            self.assertIn('id="view-insights"', page)
            self.assertIn('src="features9_10.js"', page)
            self.assertEqual(get("main-alice-token", "/features9_10.html")[0], 302)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)
            database.DB_PATH = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
