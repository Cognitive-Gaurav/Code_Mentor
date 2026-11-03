"""
CodeMentor - End-to-End Integration & Acceptance Test Suite
Runs in-process HTTP simulation of server.CodeMentorHandler, testing the full
API pipeline, database persistence, sandbox execution, and DNA tracking without
requiring external socket permissions.
"""

import io
import sys
import os
import json
import time
import logging
import unittest
import urllib.error
from http.cookies import SimpleCookie
from unittest.mock import patch

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import database
import code_executor
import analyzer
import tutor_engine
import server

class MockSocket:
    def __init__(self, data_bytes=b""):
        self.rfile = io.BytesIO(data_bytes)
        self.wfile = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if "b" in mode:
            if "r" in mode:
                return self.rfile
            return self.wfile
        raise ValueError("Only binary mode supported")

    def sendall(self, b):
        self.wfile.write(b)

class MockServer:
    def __init__(self):
        pass

TEST_SESSION_TOKENS = {}

def invoke_http(method: str, path: str, payload: dict = None, as_user_id=None, authenticated=True):
    body_bytes = json.dumps(payload).encode("utf-8") if payload is not None else b""
    if as_user_id is None:
        parts_path = path.split("?", 1)[0].strip("/").split("/")
        if len(parts_path) > 2 and parts_path[1] in ("dna", "insights", "replay") and parts_path[2].isdigit():
            as_user_id = int(parts_path[2])
        elif len(parts_path) > 2 and parts_path[1] == "attempts" and parts_path[2].isdigit():
            conn = database.get_connection()
            owner = conn.execute("SELECT user_id FROM attempts WHERE id=?", (int(parts_path[2]),)).fetchone()
            conn.close()
            as_user_id = owner["user_id"] if owner else 1
        elif path.split("?", 1)[0] == "/api/auth/logout":
            as_user_id = 1
        else:
            as_user_id = (payload or {}).get("user_id", 1)
    cookie_value = TEST_SESSION_TOKENS.get(as_user_id, "") if authenticated else ""
    headers = [
        f"{method} {path} HTTP/1.1",
        "Host: localhost",
        f"Content-Length: {len(body_bytes)}",
        "Content-Type: application/json",
    ]
    if cookie_value:
        headers.append(f"Cookie: {server.SESSION_COOKIE}={cookie_value}")
    headers.extend(["", ""])
    raw_request = "\r\n".join(headers).encode("utf-8") + body_bytes
    mock_sock = MockSocket(raw_request)

    # Instantiate handler with mock socket
    class SubHandler(server.CodeMentorHandler):
        def __init__(self, request, client_address, srv):
            self.request = request
            self.client_address = client_address
            self.server = srv
            self.setup()
            try:
                self.handle()
            finally:
                self.finish()

    handler = SubHandler(mock_sock, ("127.0.0.1", 12345), MockServer())
    raw_response = mock_sock.wfile.getvalue()

    # Parse response
    parts = raw_response.split(b"\r\n\r\n", 1)
    header_part = parts[0].decode("utf-8", errors="replace")
    body_part = parts[1] if len(parts) > 1 else b""

    status_line = header_part.split("\r\n")[0]
    status_code = int(status_line.split(" ")[1])

    try:
        data = json.loads(body_part.decode("utf-8", errors="replace"))
    except Exception:
        data = body_part.decode("utf-8", errors="replace")

    cookie_header = next((line.split(":", 1)[1].strip() for line in header_part.split("\r\n") if line.lower().startswith("set-cookie:")), None)
    if cookie_header:
        cookie = SimpleCookie(); cookie.load(cookie_header)
        if server.SESSION_COOKIE in cookie and cookie[server.SESSION_COOKIE].value:
            user_id = data.get("user", {}).get("id") if isinstance(data, dict) else None
            if user_id:
                TEST_SESSION_TOKENS[user_id] = cookie[server.SESSION_COOKIE].value
    return status_code, data

def run_tests():
    print(f"\n=======================================================")
    print(f"Running CodeMentor Full Verification Test Suite")
    print(f"=======================================================\n")

    passed_count = 0
    total_count = 0

    def assert_test(name: str, condition: bool, extra: str = ""):
        nonlocal passed_count, total_count
        total_count += 1
        if condition:
            passed_count += 1
            print(f"  PASS: {name} {extra}")
        else:
            print(f"  FAIL: {name} - {extra}")
            raise AssertionError(f"Test failed: {name} - {extra}")

    # Initialize / reset DB for clean test run
    conn = database.get_connection()
    c = conn.cursor()
    c.execute("DELETE FROM hint_logs")
    c.execute("DELETE FROM attempts WHERE user_id >= 3")
    conn.commit()
    conn.close()
    database.init_db()
    TEST_SESSION_TOKENS.clear()
    conn = database.get_connection()
    for user_id in (1, 2, 3):
        token = f"isolated-test-session-{user_id}"
        token_hash = __import__("hashlib").sha256(token.encode()).hexdigest()
        conn.execute("INSERT OR REPLACE INTO auth_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)", (token_hash, user_id, int(time.time()), int(time.time()) + 3600))
        TEST_SESSION_TOKENS[user_id] = token
    conn.commit(); conn.close()

    # 1. Health Endpoint
    status, private_without_session = invoke_http("GET", "/api/dna/1", authenticated=False)
    assert_test("Private profile API rejects unauthenticated requests", status == 401)
    status, private_dashboard_without_session = invoke_http("GET", "/api/insights/1", authenticated=False)
    assert_test("Private dashboard history rejects unauthenticated requests", status == 401)
    status, cross_user = invoke_http("GET", "/api/dna/3", as_user_id=1)
    assert_test("Authenticated account cannot read another student's DNA", status == 404)
    status, _ = invoke_http("GET", "/api/auth/me", as_user_id=3)
    assert_test("Session cookie is accepted on the current-user endpoint", status == 200)

    # 1. Health Endpoint
    status, health = invoke_http("GET", "/api/health")
    assert_test("Health Endpoint Returns 200", status == 200)
    assert_test("Health Service Identified", health.get("service") == "CodeMentor AI Learning Platform")
    assert_test("Python Compiler Available", health["compilers"]["python"] is True)
    assert_test("GCC C Compiler Checked", "gcc_c" in health["compilers"])

    # 2. Users Endpoint
    status, users_res = invoke_http("GET", "/api/users")
    assert_test("Users Endpoint Returns 200", status == 200)
    assert_test("Users Endpoint Returns Only Authenticated Account", len(users_res.get("users", [])) == 1 and users_res["users"][0]["id"] == 1)

    # 3. DNA Profile for Alex Sharma (User ID 1)
    status, alex_dna = invoke_http("GET", "/api/dna/1")
    assert_test("Alex DNA Returns 200", status == 200)
    assert_test("Alex Is Not Empty State", alex_dna["empty_state"] is False)
    assert_test("Alex Real Solve Rate Calculated", alex_dna["summary"]["solve_rate"] > 0)
    assert_test("Independent Solve Rate Is Calculated", 0 <= alex_dna["summary"]["independent_solve_rate"] <= 100)
    assert_test("Independent Solve Count Is Present", alex_dna["summary"]["independent_solves"] >= 0)
    assert_test("Alex Real Insights Present", len(alex_dna["personalized_insights"]) > 0)
    print(f"    -> Alex Solve Rate: {alex_dna['summary']['solve_rate']}%, Top Mistake: {alex_dna['mistake_distribution'][0]['name']}")

    # 4. DNA Profile for Brand New Learner Rohan Verma (User ID 3)
    status, rohan_dna = invoke_http("GET", "/api/dna/3")
    assert_test("Rohan DNA Returns 200", status == 200)
    assert_test("Rohan Empty State True (Zero Fabricated Stats)", rohan_dna["empty_state"] is True)
    assert_test("Rohan Attempts Exactly 0", rohan_dna["summary"]["total_attempts"] == 0)
    assert_test("Empty DNA reports zero completion/run metrics and insufficient evidence", rohan_dna["summary"]["completed_challenges"] == 0 and rohan_dna["summary"]["average_attempts_per_successful_solve"] == 0 and rohan_dna["sufficient_data"] is False)
    assert_test("Empty Profile Independent Solve Rate Is Zero", rohan_dna["summary"]["independent_solve_rate"] == 0.0)
    assert_test("Rohan Welcome Insight Generated", "Welcome to CodeMentor" in rohan_dna["personalized_insights"][0]["text"])
    unauth_dna_pdf_status, _ = invoke_http("GET", "/api/dna/report.pdf", authenticated=False)
    assert_test("DNA PDF download rejects unauthenticated access", unauth_dna_pdf_status == 401)
    dna_pdf_status, dna_pdf = invoke_http("GET", "/api/dna/report.pdf", as_user_id=1)
    assert_test("DNA PDF contains the signed-in student's real profile and generation date", dna_pdf_status == 200 and dna_pdf.startswith("%PDF-1.4") and "Student: Alex Sharma" in dna_pdf and "Generated:" in dna_pdf)
    _, empty_dna_pdf = invoke_http("GET", "/api/dna/report.pdf", as_user_id=3)
    assert_test("New student DNA PDF handles an empty profile", empty_dna_pdf.startswith("%PDF-1.4") and "No activity yet" in empty_dna_pdf and "Recorded attempts: 0" in empty_dna_pdf)
    _, global_board_json = invoke_http("GET", "/api/competition/leaderboard")
    global_csv_status, global_board_csv = invoke_http("GET", "/api/competition/leaderboard.csv")
    assert_test("Global leaderboard CSV exports the API's ranked saved rows", global_csv_status == 200 and global_board_csv.lstrip("\ufeff").startswith("Rank,Student,Solved,Hints,Duration seconds") and all(f"{row['rank']},{row['student']}" in global_board_csv for row in global_board_json.get("leaderboard", [])))
    global_pdf_status, global_board_pdf = invoke_http("GET", "/api/competition/leaderboard.pdf")
    assert_test("Global leaderboard PDF is labeled and valid PDF output", global_pdf_status == 200 and global_board_pdf.startswith("%PDF-1.4") and "Global Timed Practice Leaderboard" in global_board_pdf and "Scope: Global timed-practice leaderboard" in global_board_pdf)

    # 5. Problems Catalog
    status, probs_res = invoke_http("GET", "/api/problems")
    assert_test("Problems Catalog Returns 200", status == 200)
    assert_test("Problems Catalog Contains Curated Items", len(probs_res["problems"]) >= 5)

    # 6. End-to-End Array Boundary Bug Flow in C (Problem ID 1)
    print("\n--- Testing End-to-End Array Boundary Debugging Flow in C ---")
    start_payload = {
        "user_id": 3, # Use Rohan Verma to test progression from empty state
        "problem_id": 1,
        "lang": "en"
    }
    status, start_res = invoke_http("POST", "/api/attempts/start", start_payload)
    assert_test("Start Attempt Returns 201 Created", status == 201)
    attempt_id = start_res["attempt_id"]
    assert_test("Attempt ID Generated", attempt_id > 0)
    assert_test("Bug Identified in Array Indexing or Off-by-One", start_res["classification"]["primary_category"] in ("array_indexing", "off_by_one"))
    assert_test("Array Indexing Present in Detected Categories", "array_indexing" in start_res["classification"]["categories"])
    assert_test("Initial Stage is Stage 1 (Understand)", start_res["stage"]["stage_number"] == 1)
    conn = database.get_connection()
    owner = conn.execute("SELECT user_id,is_completed FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    conn.close()
    assert_test("Forged payload user_id cannot change attempt ownership", owner["user_id"] == 3 and owner["is_completed"] == 0)
    status, cross_attempt = invoke_http("GET", f"/api/attempts/{attempt_id}", as_user_id=1)
    assert_test("A different account cannot read or operate another user's attempt", status == 404)
    status, cross_hint = invoke_http("POST", f"/api/attempts/{attempt_id}/interact", {"action": "request_hint"}, as_user_id=1)
    assert_test("A different account cannot request hints for another user's attempt", status == 404)
    assert_test("Initial stage contains automated diagnosis evidence", isinstance(start_res["stage"].get("diagnostics"), list) and len(start_res["stage"]["diagnostics"]) > 0)
    assert_test("Initial stage no longer asks for an error prediction", "Predict Program Output" not in open(os.path.join(os.path.dirname(__file__), "static", "app.js"), encoding="utf-8").read())
    assert_test("Initial stage does not ask students to guess a suspicious line", "Identify Suspicious Code Line" not in open(os.path.join(os.path.dirname(__file__), "static", "app.js"), encoding="utf-8").read())
    assert_test("Old prediction and line-guess handlers are removed", "selectPredictionOption" not in open(os.path.join(os.path.dirname(__file__), "static", "app.js"), encoding="utf-8").read())
    assert_test("Timed challenge diagnosis has a direct code-attempt action", "Start timed code attempt" in open(os.path.join(os.path.dirname(__file__), "static", "app.js"), encoding="utf-8").read())

    # 7. Stage 1 Interaction: Submit Prediction
    predict_payload = {
        "action": "submit_prediction",
        "user_input": "Selected Option 0: Accesses memory outside array",
        "lang": "en"
    }
    status, stage2 = invoke_http("POST", f"/api/attempts/{attempt_id}/interact", predict_payload)
    assert_test("Submit Prediction Advances to Stage 2", stage2["stage_number"] == 2)

    # 8. Request Progressive Hint
    hint_payload = {
        "action": "request_hint",
        "user_input": "Need a hint",
        "lang": "en"
    }
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        status, stage3 = invoke_http("POST", f"/api/attempts/{attempt_id}/interact", hint_payload)
    assert_test("Hint Request Advances to Stage 3", stage3["stage_number"] == 3)
    assert_test("Hint Count Incremented to 1", stage3["hints_used"] == 1)

    # 9. Verify Hinglish Multi-Language Support
    status, stage3_hi = invoke_http("GET", f"/api/attempts/{attempt_id}?stage=3&lang=hinglish")
    assert_test("Hinglish level 1 gives conceptual guidance without revealing a line", "loop ki boundary" in stage3_hi["attempt_stage"]["hint"].lower() and "line 4" not in stage3_hi["attempt_stage"]["hint"].lower())
    print(f"    -> Hinglish Hint Preview: {stage3_hi['attempt_stage']['hint'][:60]}...")

    # 10. Request Stronger Hint
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        status, stage4 = invoke_http("POST", f"/api/attempts/{attempt_id}/interact", hint_payload)
    assert_test("Second Hint Request Advances to Stage 4", stage4["stage_number"] == 4)
    assert_test("Hint Count Incremented to 2", stage4["hints_used"] == 2)
    saved_context = tutor_engine._previous_debug_context(attempt_id)
    assert_test("Later hint context contains the actual earlier clue", any(item["type"] == "hint_request" and item["hint_or_prompt"] == stage3["hint"] for item in saved_context))
    status, replayed_hint = invoke_http("GET", f"/api/attempts/{attempt_id}?stage=3&lang=en")
    assert_test("Reopening a hint returns the exact saved content", status == 200 and replayed_hint["attempt_stage"]["hint"] == stage3["hint"])
    conn = database.get_connection()
    saved_hint_rows = conn.execute("SELECT hint_level, prompt_shown, explanation_lang, created_at FROM hint_logs WHERE attempt_id=? AND hint_type='hint_request' AND hint_level=1", (attempt_id,)).fetchall()
    conn.close()
    assert_test("Hint record persists with level, text, language, and timestamp", len(saved_hint_rows) == 2 and all(row["prompt_shown"] and row["created_at"] for row in saved_hint_rows))

    _, skip_attempt = invoke_http("POST", "/api/attempts/start", {"user_id": 1, "problem_id": 3, "lang": "en"})
    status, skipped_level = invoke_http("POST", f"/api/attempts/{skip_attempt['attempt_id']}/stage", {"stage": 4, "lang": "en"})
    assert_test("Backend refuses direct access to an unearned hint level", status == 409 and "hint" in skipped_level.get("error", "").lower())
    status, premature_solution = invoke_http("POST", f"/api/attempts/{skip_attempt['attempt_id']}/stage", {"stage": 8, "lang": "en"})
    assert_test("Stage 8 stays unavailable until explanation verification completes", status == 409 and "explanation verification" in premature_solution.get("error", "").lower())
    conn = database.get_connection()
    skip_solution = conn.execute("SELECT solution_code FROM problems WHERE id=3").fetchone()[0]
    conn.close()
    _, skipped_but_solved = invoke_http("POST", f"/api/attempts/{skip_attempt['attempt_id']}/verify", {"code": skip_solution, "lang": "en"})
    _, skipped_but_explained = invoke_http("POST", f"/api/attempts/{skip_attempt['attempt_id']}/explain", {"explanation": "The loop range excluded n because the endpoint was not included. I included n so the loop adds every required value.", "lang": "en"})
    assert_test("Bypass test has genuinely verified code and a correct explanation", skipped_but_solved.get("is_solved") is True and skipped_but_explained.get("is_completed") is True)
    status, premature_unlock = invoke_http("POST", f"/api/attempts/{skip_attempt['attempt_id']}/interact", {"action": "unlock_solution"})
    assert_test("Backend refuses solution unlock before Stage 2 and First Hint evidence", status == 200 and "stage 2" in premature_unlock.get("error", "").lower())

    # 11. Submit Incorrect Attempt (Still with <= size bug)
    wrong_code = """#include <stdio.h>
void printArray(int arr[], int size) {
    for (int i = 0; i <= size; i++) {
        printf("%d ", arr[i]);
    }
    printf("\\n");
}
int main() {
    int numbers[5] = {10, 20, 30, 40, 50};
    printArray(numbers, 5);
    return 0;
}
"""
    status, verify_fail = invoke_http("POST", f"/api/attempts/{attempt_id}/verify", {"code": wrong_code, "lang": "en"})
    assert_test("Incorrect Submission Tested in Sandbox", verify_fail["is_solved"] is False)
    assert_test("Verification Status Marked as Failed", verify_fail["execution_result"]["all_passed"] is False)

    # 12. Submit Correct Attempt (Fixed loop condition to i < size)
    fixed_code = """#include <stdio.h>
void printArray(int arr[], int size) {
    for (int i = 0; i < size; i++) {
        printf("%d ", arr[i]);
    }
    printf("\\n");
}
int main() {
    int numbers[5] = {10, 20, 30, 40, 50};
    printArray(numbers, 5);
    return 0;
}
"""
    status, verify_pass = invoke_http("POST", f"/api/attempts/{attempt_id}/verify", {"code": fixed_code, "lang": "en"})
    assert_test("Correct Submission Verified in Sandbox", verify_pass["is_solved"] is True)
    assert_test("All Sandbox Tests Passed", verify_pass["execution_result"]["all_passed"] is True)
    assert_test("Successful code verification opens Stage 6 results before Explanation", verify_pass["next_stage_data"]["stage_number"] == 6 and verify_pass["next_stage_data"]["verification_status"] == "verified")
    stale_after_verify_status, stale_after_verify = invoke_http("POST", f"/api/attempts/{attempt_id}/draft", {"code": wrong_code, "revision": 1})
    _, verified_code_after_stale = invoke_http("GET", f"/api/attempts/{attempt_id}?lang=en")
    assert_test("An older draft cannot overwrite the latest verified code", stale_after_verify_status == 200 and stale_after_verify.get("saved") is False and verified_code_after_stale["attempt_stage"]["current_code"] == fixed_code)
    status, owner_restore = invoke_http("GET", f"/api/attempts/{attempt_id}?lang=en")
    assert_test("Owner can restore the saved Stage 6 verification results after refresh", status == 200 and owner_restore["attempt_stage"]["stage_number"] == 6 and owner_restore["attempt_stage"]["current_code"] == fixed_code)
    foreign_restore_status, _ = invoke_http("GET", f"/api/attempts/{attempt_id}", as_user_id=2)
    assert_test("A different user cannot restore another user's attempt ID", foreign_restore_status == 404)
    status, replay_events = invoke_http("GET", "/api/replay/3")
    replay_attempt = next((item for item in replay_events.get("attempts", []) if item["attempt_id"] == attempt_id), {})
    assert_test("Error Replay saves failed and successful submitted code versions", status == 200 and len([event for event in replay_attempt.get("events", []) if event["type"] == "verification"]) == 2)
    assert_test("Error Replay records the actual progressive hint text", any(event["type"] == "hint_request" and event.get("hint") for event in replay_attempt.get("events", [])))

    # 13. Verify Persistent Debugging DNA Update for Rohan (User ID 3)
    status, rohan_dna_updated = invoke_http("GET", "/api/dna/3")
    assert_test("Rohan DNA No Longer Empty State", rohan_dna_updated["empty_state"] is False)
    assert_test("Rohan Has Exactly 1 Attempt Recorded", rohan_dna_updated["summary"]["total_attempts"] == 1)
    assert_test("Rohan Has Exactly 1 Solved Attempt", rohan_dna_updated["summary"]["solved_count"] == 1)
    assert_test("Rohan Solve Rate Is Exactly 100.0%", rohan_dna_updated["summary"]["solve_rate"] == 100.0)
    assert_test("Rohan Hints Recorded as 2", rohan_dna_updated["summary"]["total_hints_used"] == 2)
    rohan_slugs = [d["slug"] for d in rohan_dna_updated["mistake_distribution"]]
    assert_test("Rohan DNA Category Shows Array Indexing", "array_indexing" in rohan_slugs)
    assert_test("Rohan DNA Category Shows Off-by-One", "off_by_one" in rohan_slugs)
    assert_test("DNA refresh adds the new attempt to topic and difficulty performance", rohan_dna_updated["sufficient_data"] is False and sum(item["attempts"] for item in rohan_dna_updated["topic_performance"]) == 1 and sum(item["attempts"] for item in rohan_dna_updated["difficulty_performance"]) == 1 and rohan_dna_updated["topic_performance"][0]["evidence_status"] == "insufficient_data")

    print("\n--- Testing Four-Level Evidence-Based Hint Ladder ---")
    _, hint_attempt = invoke_http("POST", "/api/attempts/start", {"user_id": 1, "problem_id": 3, "lang": "en"})
    failed_run = invoke_http("POST", f"/api/attempts/{hint_attempt['attempt_id']}/verify", {"code": hint_attempt["problem"]["buggy_code"], "lang": "en"})
    failed_verification = failed_run[1]
    assert_test("Hint evidence is sourced from an actual failed execution", failed_run[0] == 200 and failed_verification.get("is_solved") is False and failed_verification.get("execution_result", {}).get("all_passed") is False)
    hint_texts = []
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        for hint_level in range(1, 5):
            status, hint_response = invoke_http("POST", f"/api/attempts/{hint_attempt['attempt_id']}/interact", {"action": "request_hint", "user_input": "next evidence-based hint", "lang": "en"})
            hint_texts.append(hint_response.get("hint", ""))
            assert_test(f"Hint level {hint_level} is served in sequence", status == 200 and hint_response.get("hints_used") == hint_level and f"{hint_level}/4" in hint_response.get("hint", ""))
    assert_test("First hint references the real expected and actual test output", "Sum = 15" in hint_texts[0] and "Sum = 10" in hint_texts[0])
    assert_test("Four hint levels contain distinct stored clue text", len(set(hint_texts)) == 4)
    status, exhausted_hint = invoke_http("POST", f"/api/attempts/{hint_attempt['attempt_id']}/interact", {"action": "request_hint", "user_input": "one more", "lang": "en"})
    assert_test("Backend refuses a fifth hint", status == 409 and "complete" in exhausted_hint.get("error", "").lower())

    # Regression: a student's incorrect correction must change the next hint.
    _, c_hint_attempt = invoke_http("POST", "/api/attempts/start", {"user_id": 1, "problem_id": 1, "lang": "en"})
    c_attempt_id = c_hint_attempt["attempt_id"]
    invoke_http("POST", f"/api/attempts/{c_attempt_id}/interact", {"action": "request_hint", "user_input": "first clue", "lang": "en"})
    invoke_http("POST", f"/api/attempts/{c_attempt_id}/stage", {"stage": 5, "lang": "en"})
    conn = database.get_connection()
    c_reference_solution = conn.execute("SELECT solution_code FROM problems WHERE id=1").fetchone()[0]
    conn.close()
    wrong_boundary_fix = c_reference_solution.replace("i < size", "i < size - 1")
    c_failed_run = invoke_http("POST", f"/api/attempts/{c_attempt_id}/verify", {"code": wrong_boundary_fix, "lang": "en"})[1]
    assert_test("A wrong boundary correction is detected from test output", c_failed_run.get("is_solved") is False, f"submitted_condition={next((line.strip() for line in wrong_boundary_fix.splitlines() if 'for (int i' in line), 'missing')} actual={((c_failed_run.get('execution_result') or {}).get('test_results') or [{}])[0].get('actual_output')}")
    invoke_http("POST", f"/api/attempts/{c_attempt_id}/interact", {"action": "request_hint", "user_input": "second clue", "lang": "en"})
    invoke_http("POST", f"/api/attempts/{c_attempt_id}/interact", {"action": "request_hint", "user_input": "third clue", "lang": "en"})
    _, c_fourth_hint = invoke_http("POST", f"/api/attempts/{c_attempt_id}/interact", {"action": "request_hint", "user_input": "fourth clue", "lang": "en"})
    assert_test("Strong C hint addresses the actual missing final array element", "final valid element" in c_fourth_hint.get("hint", "").lower())
    hi_status, c_hint_hi = invoke_http("GET", f"/api/attempts/{c_attempt_id}?stage=4&lang=hinglish")
    assert_test("Evidence-based strong hint is also available in Hinglish", hi_status == 200 and "aakhri valid element" in c_hint_hi.get("attempt_stage", {}).get("hint", "").lower())

    print("\n--- Testing Hint Diagnosis Across Languages and Bug Types ---")
    java_code = "public class Main { public static void main(String[] args) { int[] values={1,2}; System.out.println(values[2]); } }"
    java_diag = analyzer.classify_mistakes(java_code, "java", error_message="ArrayIndexOutOfBoundsException: Index 2 out of bounds for length 2")
    java_hint = tutor_engine._local_hint({"current_code": java_code, "language": "java", "description": "Read values from an array"}, java_diag["diagnostics"][0], 4, "en")
    assert_test("Java array exception receives array-boundary guidance", java_diag["primary_category"] == "array_indexing" and "valid indices" in java_hint.lower())

    c_compile = analyzer.classify_mistakes("int main() { return 0 }", "c", compiler_error="main.c:1:20: error: expected ';' before '}'")
    c_compile_hint = tutor_engine._local_hint({"current_code": "int main() { return 0 }", "language": "c", "description": ""}, c_compile["diagnostics"][0], 2, "en")
    assert_test("C compiler error hint uses its verified source line", c_compile["primary_category"] == "syntax_errors" and "line 1" in c_compile_hint.lower())

    parity_code = "def is_even(n):\n    return n % 2 == 1"
    parity_diag = analyzer.classify_mistakes(parity_code, "python", error_message="The function should return true for even numbers")
    parity_hint = tutor_engine._local_hint({"current_code": parity_code, "language": "python", "description": "Return true only for even numbers"}, parity_diag["diagnostics"][0], 3, "en")
    assert_test("Wrong even/odd rule receives condition-specific guidance", parity_diag["primary_category"] == "incorrect_conditions" and "condition" in parity_hint.lower())

    factorial_code = "def factorial(n):\n    if n == 0: return 0\n    return n * factorial(n - 1)"
    factorial_diag = analyzer.classify_mistakes(factorial_code, "python")
    factorial_hint = tutor_engine._local_hint({"current_code": factorial_code, "language": "python", "description": "Calculate factorial recursively"}, factorial_diag["diagnostics"][0], 4, "en")
    assert_test("Factorial recursion hint focuses on the base case", factorial_diag["primary_category"] == "recursion_misunderstanding" and "base-case" in factorial_hint.lower())

    type_error = {"current_code": "total = 1 + '2'", "language": "python", "description": "Add two values", "last_test_results_json": json.dumps({"test_results": [{"passed": False, "error": "TypeError: unsupported operand type(s) for +", "actual_output": "", "expected_output": "3"}]})}
    type_diag = analyzer.classify_mistakes(type_error["current_code"], "python", error_message="TypeError: unsupported operand type(s) for +")
    type_hint = tutor_engine._local_hint(type_error, type_diag["diagnostics"][0], 4, "en")
    assert_test("Python TypeError guidance uses the actual runtime message", "operands" in type_hint.lower())

    pointer_code = "int main(){ int *ptr = 0; return *ptr; }"
    pointer_diag = analyzer.classify_mistakes(pointer_code, "c", error_message="Segmentation fault")
    pointer_attempt = {"current_code": pointer_code, "language": "c", "description": "Read through a pointer", "last_test_results_json": json.dumps({"test_results": [{"passed": False, "error": "Segmentation fault"}]})}
    pointer_hint = tutor_engine._local_hint(pointer_attempt, pointer_diag["diagnostics"][0], 4, "en")
    assert_test("C pointer crash is not mislabeled as an array-index bug", pointer_diag["primary_category"] != "array_indexing" and "pointer" in pointer_hint.lower())

    multi_code = "for (int i=0; i<=size; i++) { if (i = 2) total += arr[i]; }"
    multi_diag = analyzer.classify_mistakes(multi_code, "c", error_message="array index out of bounds")
    assert_test("Multiple supported bug signals remain visible in diagnostics", len(multi_diag["categories"]) >= 2)

    no_bug_code = "def add(a, b):\n    return a + b"
    no_bug_diag = analyzer.classify_mistakes(no_bug_code, "python")
    no_bug_hint = tutor_engine._local_hint({"current_code": no_bug_code, "language": "python", "description": ""}, no_bug_diag["diagnostics"][0], 1, "en")
    assert_test("No obvious bug and missing requirements ask for expected behavior", no_bug_diag["overall_confidence"] < 0.65 and "expected output" in no_bug_hint.lower())

    print("\n--- Testing Hint AI Validation and Local Fallback ---")
    _, ai_hint_attempt = invoke_http("POST", "/api/attempts/start", {"user_id": 1, "problem_id": 3, "lang": "en"})
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-key", "GROQ_API_KEY": ""}):
        with patch("tutor_engine.generate_ai", return_value={"text": "Check the array indexes; change the operator on line 99.", "provider": "openai", "model": "test-model", "error": None}):
            ai_status, rejected_hint = invoke_http("POST", f"/api/attempts/{ai_hint_attempt['attempt_id']}/interact", {"action": "request_hint", "user_input": "first hint", "lang": "en"})
    assert_test("Irrelevant AI hint and unsupported line number are rejected", ai_status == 200 and rejected_hint["ai_provider"] == "local" and "array index" not in rejected_hint["hint"].lower())

    _, valid_ai_attempt = invoke_http("POST", "/api/attempts/start", {"user_id": 1, "problem_id": 3, "lang": "en"})
    valid_ai_text = "Python range stops before its endpoint. Trace range with n=5 to see which value is omitted."
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-key", "GROQ_API_KEY": ""}):
        with patch("tutor_engine.generate_ai", return_value={"text": valid_ai_text, "provider": "openai", "model": "test-model", "error": None}):
            valid_ai_status, accepted_hint = invoke_http("POST", f"/api/attempts/{valid_ai_attempt['attempt_id']}/interact", {"action": "request_hint", "user_input": "first hint", "lang": "en"})
        with patch("tutor_engine.generate_ai", return_value={"text": valid_ai_text, "provider": "openai", "model": "test-model", "error": None}):
            repeated_status, repeated_ai_hint = invoke_http("POST", f"/api/attempts/{valid_ai_attempt['attempt_id']}/interact", {"action": "request_hint", "user_input": "second hint", "lang": "en"})
    assert_test("Grounded AI hint is labeled with its real mocked provider", valid_ai_status == 200 and accepted_hint["ai_provider"] == "openai" and accepted_hint["hint"] == valid_ai_text)
    assert_test("Repeated AI hint is rejected in favor of a distinct local clue", repeated_status == 200 and repeated_ai_hint["ai_provider"] == "local" and repeated_ai_hint["hint"] != valid_ai_text)

    print("\n--- Testing OpenAI to Local Provider Transition Diagnostics ---")
    openai_payload = json.dumps({"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "OpenAI response"}]}]}).encode("utf-8")
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "FREE_ONLY_MODE": "true", "OPENAI_API_KEY": "test-key", "GROQ_API_KEY": ""}):
        with unittest.TestCase().assertLogs("codementor.ai", level="INFO") as ai_logs:
            with patch("tutor_engine.urllib.request.urlopen", side_effect=[io.BytesIO(openai_payload), TimeoutError(), TimeoutError()]):
                first_provider = tutor_engine.generate_ai("safe test prompt")
                next_provider = tutor_engine.generate_ai("safe test prompt")
    log_text = "\n".join(ai_logs.output)
    assert_test("Consecutive provider results report OpenAI success then bounded Local timeout fallback",
        first_provider.get("provider") == "openai" and next_provider.get("provider") == "local"
        and next_provider.get("response_status") == "timeout" and len(next_provider.get("provider_attempts", [])) == 2)
    assert_test("Provider diagnostics record selection and timeout without logging prompt or key",
        "ai_provider_attempt provider=openai" in log_text and "outcome=timeout" in log_text
        and "safe test prompt" not in log_text and "test-key" not in log_text)

    print("\n--- Testing Java Compiler Evidence Classification and Tutor Guidance ---")
    compiler_code = "class Main {\n  public static void main(String[] args) {\n    System.out.println);\n  }\n}"
    compiler_output = "Main.java:3: error: not a statement\n    System.out.println);\n                      ^\nMain.java:3: error: ';' expected"
    compiler_diag = analyzer.classify_mistakes(compiler_code, "java", compiler_error=compiler_output)["diagnostics"][0]
    compiler_attempt = {"language": "java", "current_code": compiler_code,
        "last_test_results_json": json.dumps({"is_compiled": False, "compile_error": compiler_output,
            "test_results": [{"passed": False, "expected_output": "ready", "actual_output": "", "error": compiler_output}]})}
    compiler_assessment, compiler_reply = tutor_engine._local_debug_response("I don't know.", compiler_attempt, compiler_diag, "en", [])
    assert_test("Java 'not a statement' and semicolon diagnostics classify as syntax on the compiler line",
        compiler_diag.get("category") == "syntax_errors" and compiler_diag.get("suspected_lines") == [3])
    assert_test("Compiler evidence takes priority over expected output and prompts one focused question",
        compiler_assessment == "unsure" and "line 3" in compiler_reply.lower()
        and "complete java statement" in compiler_reply.lower() and "which part" in compiler_reply.lower()
        and "expected `ready`" not in compiler_reply.lower())
    no_line_error = "error: not a statement\nerror: ';' expected"
    no_line_attempt = {**compiler_attempt, "last_test_results_json": json.dumps({"is_compiled": False,
        "compile_error": no_line_error, "test_results": []})}
    no_line_reply = tutor_engine._friendly_debug_error(no_line_attempt, no_line_error, "en")
    assert_test("Tutor does not invent a source line when the compiler omitted coordinates",
        "the reported statement" in no_line_reply.lower() and "line " not in no_line_reply.lower()
        and tutor_engine._saved_execution_errors(no_line_attempt) == no_line_error)

    # 14. Custom Code Debugging Submission
    print("\n--- Testing Custom Code Debugger ---")
    custom_payload = {
        "user_id": 1,
        "language": "python",
        "code": "def find_max(nums):\n    max_val = 0\n    for n in nums:\n        if n > max_val: max_val = n\n    return max_val",
        "description": "Fails on negative numbers [-10, -5, -20]",
        "lang": "en"
    }
    status, custom_res = invoke_http("POST", "/api/custom-debug", custom_payload)
    assert_test("Custom Code Submission Returns 201", status == 201)
    assert_test("Custom Mistake Classified into Taxonomy", "algorithm_logic" in custom_res["classification"]["categories"])
    assert_test("Custom diagnosis exposes uncertainty and source evidence", custom_res["classification"]["overall_confidence"] <= 1 and custom_res["stage"].get("diagnostics"))
    status, uncertain_res = invoke_http("POST", "/api/custom-debug", {"user_id": 1, "language": "python", "code": "value = 7", "lang": "en"})
    assert_test("Uncertain code-only diagnosis communicates low confidence", status == 201 and uncertain_res["classification"]["overall_confidence"] < 0.65)
    status, bad_lang = invoke_http("POST", "/api/custom-debug", {"user_id": 1, "language": "fortran", "code": "print(1)"})
    assert_test("Unsupported custom diagnosis language is rejected clearly", status == 400 and "Unsupported language" in bad_lang.get("error", ""))

    print("\n--- Testing Feature 3 Context-Aware Debugging Tutor ---")
    java_context = {"user_id": 1, "language": "java", "title": "Add two numbers",
        "description": "This program should add a and b and print their sum. Current output does not match that behavior.",
        "code": "public class Main { public static void main(String[] args) { int a=10, b=20; System.out.println(a - b); } }", "lang": "en"}
    conn = database.get_connection()
    conn.execute("""INSERT INTO problems (slug,title,language,difficulty,concept,mistake_category_slug,description,buggy_code,solution_code,test_cases_json,stages_config_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", ("feature3-java-addition-tutor", java_context["title"], "java", "Beginner", "Arithmetic Logic", "algorithm_logic",
        java_context["description"], java_context["code"], "public class Main { public static void main(String[] args) { int a=10, b=20; System.out.println(a + b); } }",
        json.dumps([{"input":"", "expected_output":"30", "description":"Sum of 10 and 20"}]), "{}"))
    java_problem_id = conn.execute("SELECT id FROM problems WHERE slug='feature3-java-addition-tutor'").fetchone()[0]
    conn.commit(); conn.close()
    _, java_attempt = invoke_http("POST", "/api/attempts/start", {"user_id": 1, "problem_id": java_problem_id, "lang": "en"})
    java_id = java_attempt.get("attempt_id")
    java_run_status, java_run = invoke_http("POST", f"/api/attempts/{java_id}/verify", {"code": java_context["code"], "lang": "en"})
    java_exec_output = java_run.get("execution_result", {}).get("test_results", [{}])[0].get("actual_output", "").strip()
    print(f"    Java runner output for the submitted subtraction code: {java_exec_output}")
    assert_test("Java logical bug is executed against its real expected output and saved", java_run_status == 200 and java_exec_output == "-10" and java_run.get("is_solved") is False and java_run.get("execution_result", {}).get("test_results", [{}])[0].get("expected_output") == "30")
    hint_status, _ = invoke_http("POST", f"/api/attempts/{java_id}/interact", {"action": "request_hint", "user_input": "first evidence clue", "lang": "en"})
    assert_test("Tutor session starts with the existing Hint Ladder and does not create another hint flow", hint_status == 200)
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        java_status, java_turn = invoke_http("POST", f"/api/attempts/{java_id}/interact", {"action": "debug_reply",
            "user_input": "The calculation subtracts rather than adds, so the actual result differs from the expected sum.", "lang": "en", "code_snapshot": java_context["code"]})
    print(f"    Java student: {java_context['description']}\n    Student: {java_turn.get('debug_conversation', [{}])[-1].get('student', '')}\n    Tutor: {java_turn.get('debug_reply', '')}")
    assert_test("Java logical-bug conversation evaluates the explanation using its context", java_status == 200 and java_turn.get("debug_assessment") == "correct" and "expected" in java_turn.get("debug_reply", "").lower())
    answer_status, answer_turn = invoke_http("POST", f"/api/attempts/{java_id}/interact", {"action": "debug_reply", "user_input": "Give me the complete solution code.", "lang": "en", "code_snapshot": java_context["code"]})
    assert_test("Direct answer request is guided without revealing code or bypassing policy", answer_status == 200 and answer_turn.get("debug_assessment") == "solution_request" and "unlock policy" in answer_turn.get("debug_reply", "").lower() and "solution_code" not in answer_turn)
    assert_test("Conversation history is returned in chronological order", len(answer_turn.get("debug_conversation", [])) == 2 and answer_turn["debug_conversation"][0]["student"].startswith("The calculation"))
    safe_ai_reply = json.dumps({"assessment": "partially_correct", "reply": "Your observation is relevant to the stated goal. What operation does the current expression perform, and how does that affect the output?"})
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-key", "GROQ_API_KEY": ""}):
        with patch("tutor_engine.generate_ai", return_value={"text": safe_ai_reply, "provider": "openai", "model": "test-model", "error": None}):
            ai_chat_status, ai_chat_turn = invoke_http("POST", f"/api/attempts/{java_id}/interact", {"action": "debug_reply", "user_input": "Can you guide me a bit?", "lang": "en", "code_snapshot": java_context["code"]})
    assert_test("Structured safe AI tutor response is attributed to the mocked OpenAI provider", ai_chat_status == 200 and ai_chat_turn.get("debug_provider") == "openai" and ai_chat_turn.get("debug_assessment") == "partially_correct")
    unsafe_ai_reply = json.dumps({"assessment": "correct", "reply": "Replace the expression with this complete solution: System.out.println(a + b);"})
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-key", "GROQ_API_KEY": ""}):
        with patch("tutor_engine.generate_ai", return_value={"text": unsafe_ai_reply, "provider": "openai", "model": "test-model", "error": None}):
            unsafe_chat_status, unsafe_chat_turn = invoke_http("POST", f"/api/attempts/{java_id}/interact", {"action": "debug_reply", "user_input": "What should I inspect?", "lang": "en", "code_snapshot": java_context["code"]})
    assert_test("A solution-revealing AI tutor response is discarded for local guidance", unsafe_chat_status == 200 and unsafe_chat_turn.get("debug_provider") == "local" and "System.out.println(a + b)" not in unsafe_chat_turn.get("debug_reply", ""))
    conn = database.get_connection()
    java_saved_turn = conn.execute("SELECT student_response,prompt_shown,hint_provider,context_json,created_at FROM hint_logs WHERE attempt_id=? AND hint_type='debug_conversation' ORDER BY id LIMIT 1", (java_id,)).fetchone()
    saved_context = json.loads(java_saved_turn["context_json"])
    solution_state = conn.execute("SELECT solution_unlocked FROM attempts WHERE id=?", (java_id,)).fetchone()[0]
    conn.close()
    assert_test("Tutor turn persists user reply, tutor answer, provider, timestamp, code snapshot, and prior hint level", java_saved_turn is not None and java_saved_turn["student_response"].startswith("The calculation") and java_saved_turn["prompt_shown"] and java_saved_turn["created_at"] and saved_context["code_snapshot"] == java_context["code"] and saved_context["hint_levels"] == [1])
    assert_test("Chat solution request does not alter the existing solution-unlock state", solution_state == 0)

    syntax_context = {"user_id": 1, "language": "java", "title": "Compiler error", "description": "javac reports: error: ';' expected on line 1", "code": "public class Main { public static void main(String[] args) { int total = 4 } }", "lang": "en"}
    _, syntax_attempt = invoke_http("POST", "/api/custom-debug", syntax_context)
    syntax_id = syntax_attempt.get("attempt_id")
    syntax_run_status, syntax_run = invoke_http("POST", f"/api/attempts/{syntax_id}/verify", {"code": syntax_context["code"], "lang": "en"})
    actual_compile_error = syntax_run.get("execution_result", {}).get("compile_error", "")
    print(f"    Java compiler output captured by runner: {actual_compile_error[:180]}")
    assert_test("Java compilation error is obtained from the actual compiler runner", syntax_run_status == 200 and syntax_run.get("execution_result", {}).get("is_compiled") is False and "expected" in actual_compile_error.lower())
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        syntax_status, syntax_turn = invoke_http("POST", f"/api/attempts/{syntax_id}/interact", {"action": "debug_reply", "user_input": "I don't know.", "lang": "en", "code_snapshot": syntax_context["code"]})
    print(f"    Java compile symptom: {syntax_context['description']}\n    Student: I don't know.\n    Tutor: {syntax_turn.get('debug_reply', '')}")
    assert_test("I do not know response explains Java semicolon error in beginner language", syntax_status == 200 and syntax_turn.get("debug_assessment") == "unsure" and "semicolon" in syntax_turn.get("debug_reply", "").lower() and "statement" in syntax_turn.get("debug_reply", "").lower() and "C:\\Users\\" not in syntax_turn.get("debug_reply", "") and "public class Main" not in syntax_turn.get("debug_reply", ""))
    incorrect_compile_status, incorrect_compile_turn = invoke_http("POST", f"/api/attempts/{syntax_id}/interact", {"action": "debug_reply", "user_input": "The variable color is wrong.", "lang": "en", "code_snapshot": syntax_context["code"]})
    assert_test("Incorrect explanation on compile failure gets a beginner-friendly Java clue", incorrect_compile_status == 200 and "needs a semicolon" in incorrect_compile_turn.get("debug_reply", "").lower() and "';' expected" not in incorrect_compile_turn.get("debug_reply", "") and "expected ``" not in incorrect_compile_turn.get("debug_reply", "") and "got ``" not in incorrect_compile_turn.get("debug_reply", ""))
    assert_test("Tutor no longer appends the repetitive earlier-hint sentence to every reply", "instead of repeating an earlier clue" not in incorrect_compile_turn.get("debug_reply", "").lower())

    java_quote_code = 'public class Main {\n  public static void main(String[] args) {\n    int value = 2;\n    System.out.println(value);\n    String label = "hello;\n  }\n}'
    quote_context = {"user_id": 1, "language": "java", "title": "Missing quote", "description": "A Java string is missing its closing quote.", "code": java_quote_code, "lang": "hinglish"}
    _, quote_attempt = invoke_http("POST", "/api/custom-debug", quote_context)
    quote_id = quote_attempt.get("attempt_id")
    quote_run_status, quote_run = invoke_http("POST", f"/api/attempts/{quote_id}/verify", {"code": java_quote_code, "lang": "hinglish"})
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        quote_status, quote_turn = invoke_http("POST", f"/api/attempts/{quote_id}/interact", {"action": "debug_reply", "user_input": "Samajh nahi aaya", "lang": "hinglish", "code_snapshot": java_quote_code})
    quote_reply = quote_turn.get("debug_reply", "")
    print(f"    Java error: {quote_run.get('execution_result', {}).get('compile_error', '')[:150]}\n    Hinglish tutor: {quote_reply}")
    assert_test("Java unclosed-string error points to the real line and explains matching quotes in Hinglish", quote_run_status == 200 and quote_status == 200 and "line 5" in quote_reply.lower() and "closing quote" in quote_reply.lower() and "agli line" in quote_reply.lower() and "unclosed string literal" not in quote_reply.lower() and "public class Main" not in quote_reply)

    _, unknown_attempt = invoke_http("POST", "/api/custom-debug", {"user_id": 1, "language": "python", "code": "value = 7", "lang": "en"})
    unknown_id = unknown_attempt["attempt_id"]
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        unknown_status, unknown_turn = invoke_http("POST", f"/api/attempts/{unknown_id}/interact", {"action": "debug_reply", "user_input": "What bug do you see?", "lang": "en", "code_snapshot": "value = 7"})
    assert_test("Tutor requests missing expected behavior instead of inventing a bug", unknown_status == 200 and unknown_turn.get("debug_assessment") == "uncertain" and ("intended behavior" in unknown_turn.get("debug_reply", "").lower() or "expected behavior" in unknown_turn.get("debug_reply", "").lower()))

    _, wrong_explanation_attempt = invoke_http("POST", "/api/attempts/start", {"user_id": 1, "problem_id": 3, "lang": "en"})
    wrong_explanation_id = wrong_explanation_attempt["attempt_id"]
    invoke_http("POST", f"/api/attempts/{wrong_explanation_id}/verify", {"code": wrong_explanation_attempt["problem"]["buggy_code"], "lang": "en"})
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        wrong_status, wrong_turn = invoke_http("POST", f"/api/attempts/{wrong_explanation_id}/interact", {"action": "debug_reply", "user_input": "The variable color is wrong.", "lang": "en"})
        partial_status, partial_turn = invoke_http("POST", f"/api/attempts/{wrong_explanation_id}/interact", {"action": "debug_reply", "user_input": "The loop boundary might be wrong.", "lang": "en"})
        correct_status, correct_turn = invoke_http("POST", f"/api/attempts/{wrong_explanation_id}/interact", {"action": "debug_reply", "user_input": "The range excludes n, so the total is short by n.", "lang": "en"})
        unsure_status, unsure_turn = invoke_http("POST", f"/api/attempts/{wrong_explanation_id}/interact", {"action": "debug_reply", "user_input": "I don't know.", "lang": "en"})
    print(f"    Python failed-test evidence: expected Sum = 15, actual Sum = 10\n    Incorrect answer: {wrong_turn.get('debug_reply', '')}\n    Partial answer: {partial_turn.get('debug_assessment')} — {partial_turn.get('debug_reply', '')}\n    Unsure: {unsure_turn.get('debug_reply', '')}")
    assert_test("Incorrect student explanation is checked against real test output", wrong_status == 200 and wrong_turn.get("debug_assessment") == "incorrect_or_unrelated" and "Sum = 15" in wrong_turn.get("debug_reply", "") and "Sum = 10" in wrong_turn.get("debug_reply", ""))
    assert_test("Partially correct reasoning asks for the missing causal explanation", partial_status == 200 and partial_turn.get("debug_assessment") == "partially_correct")
    assert_test("Causally correct loop explanation is recognized as correct", correct_status == 200 and correct_turn.get("debug_assessment") == "correct")
    assert_test("Unsure response uses actual expected and actual values", unsure_status == 200 and unsure_turn.get("debug_assessment") == "unsure" and "Sum = 15" in unsure_turn.get("debug_reply", "") and "Sum = 10" in unsure_turn.get("debug_reply", ""))

    unauthorized_status, unauthorized_chat = invoke_http("POST", f"/api/attempts/{java_id}/interact", {"action": "debug_reply", "user_input": "Show history"}, as_user_id=2)
    assert_test("One user cannot open another user's tutor conversation", unauthorized_status == 404 and "error" in unauthorized_chat)
    invalid_chat_status, invalid_chat = invoke_http("POST", f"/api/attempts/{java_id}/interact", {"action": "debug_reply", "user_input": ""})
    assert_test("Empty tutor messages are rejected by backend validation", invalid_chat_status == 400 and "characters" in invalid_chat.get("error", ""))

    # 15. New Student Registration
    print("\n--- Testing Student Registration & Persistence ---")
    reg_payload = {
        "email": f"judge-{int(time.time())}@example.test",
        "username": f"judge_{int(time.time())}",
        "display_name": "Hackathon Judge",
        "password": "securepassword",
        "password_confirm": "securepassword"
    }
    status, reg_res = invoke_http("POST", "/api/auth/register", reg_payload)
    assert_test("Register New Student With Username Returns 201 Created", status == 201 and reg_res["user"]["username"] == reg_payload["username"])
    judge_id = reg_res["user"]["id"]
    dup_status, _ = invoke_http("POST", "/api/auth/register", reg_payload, authenticated=False)
    assert_test("Duplicate email registration is rejected", dup_status == 409)
    dup_username_status, _ = invoke_http("POST", "/api/auth/register", {**reg_payload, "email": f"different-{int(time.time())}@example.test"}, authenticated=False)
    assert_test("Duplicate username registration is rejected", dup_username_status == 409)
    mismatch_status, _ = invoke_http("POST", "/api/auth/register", {**reg_payload, "email": f"other-{int(time.time())}@example.test", "password_confirm": "not-the-password"}, authenticated=False)
    assert_test("Registration rejects mismatched password confirmation", mismatch_status == 400)
    status, logout_res = invoke_http("POST", "/api/auth/logout", as_user_id=judge_id)
    assert_test("Logout revokes the stored browser session", status == 200)
    status, revoked = invoke_http("GET", f"/api/dna/{judge_id}", as_user_id=judge_id)
    assert_test("Revoked session can no longer access private profile data", status == 401)
    # The test account signs back in through the normal credential flow for subsequent checks.
    invalid_status, _ = invoke_http("POST", "/api/auth/login", {"username": reg_payload["email"], "password": "invalid-password"}, authenticated=False)
    assert_test("Invalid credentials are rejected", invalid_status == 401)
    status, login_res = invoke_http("POST", "/api/auth/login", {"username": reg_payload["email"], "password": reg_payload["password"]}, authenticated=False)
    assert_test("Registered account can sign in through password authentication", status == 200 and login_res.get("user", {}).get("id") == judge_id)
    status, refreshed_session = invoke_http("GET", "/api/auth/me", as_user_id=judge_id)
    assert_test("Valid server session survives a page-refresh style request", status == 200 and refreshed_session.get("user", {}).get("id") == judge_id)
    status, judge_dna = invoke_http("GET", f"/api/dna/{judge_id}")
    assert_test("Registered Student Starts with Clean Empty DNA", judge_dna["empty_state"] is True)

    # 16. Test Python Loop Boundary Problem (Problem ID 3)
    print("\n--- Testing Python Loop Boundary Problem ---")
    py_start = {
        "user_id": 1,
        "problem_id": 3,
        "lang": "en"
    }
    status, py_res = invoke_http("POST", "/api/attempts/start", py_start)
    assert_test("Start Python Loop Problem Returns 201", status == 201)
    py_attempt_id = py_res["attempt_id"]
    invoke_http("POST", f"/api/attempts/{py_attempt_id}/stage", {"stage": 2, "lang": "en"})
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        invoke_http("POST", f"/api/attempts/{py_attempt_id}/interact", {"action": "request_hint", "user_input": "I have reviewed the evidence and want the first conceptual clue.", "lang": "en"})
    invoke_http("POST", f"/api/attempts/{py_attempt_id}/stage", {"stage": 5, "lang": "en"})
    # Solve it with correct code
    py_fixed = """def sum_numbers(n):
    total = 0
    for i in range(1, n + 1):
        total += i
    return total

if __name__ == "__main__":
    result = sum_numbers(5)
    print(f"Sum = {result}")
"""
    status, py_verify = invoke_http("POST", f"/api/attempts/{py_attempt_id}/verify", {"code": py_fixed, "lang": "en"})
    assert_test("Python Problem Verified and Solved", py_verify["is_solved"] is True)
    assert_test("Python Test Result Passed", py_verify["execution_result"]["all_passed"] is True)
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        verified_chat_status, verified_chat = invoke_http("POST", f"/api/attempts/{py_attempt_id}/interact", {"action": "debug_reply", "user_input": "I don't know why the fix works.", "lang": "en", "code_snapshot": py_fixed})
        edited_chat_status, edited_chat = invoke_http("POST", f"/api/attempts/{py_attempt_id}/interact", {"action": "debug_reply", "user_input": "I don't know.", "lang": "en", "code_snapshot": py_fixed + "\n# unverified edit"})
    assert_test("Verified code receives tutor feedback grounded in the passing runner result", verified_chat_status == 200 and verified_chat.get("debug_assessment") == "verification_passed" and "passed all required tests" in verified_chat.get("debug_reply", ""))
    assert_test("Editing code after a pass prevents tutor from reusing stale success claims", edited_chat_status == 200 and "passed all required tests" not in edited_chat.get("debug_reply", ""))
    conn = database.get_connection()
    completion = conn.execute("SELECT is_completed FROM attempts WHERE id=?", (py_attempt_id,)).fetchone()[0]
    conn.close()
    assert_test("Passing code remains incomplete until Explain Your Fix is submitted", completion == 0)

    print("\n--- Testing learning intelligence and safe AI fallback ---")
    status, advanced = invoke_http("GET", "/api/insights/1")
    assert_test("Advanced insights returns real-data IQ and heatmap", status == 200 and advanced["debugging_iq"] >= 0 and isinstance(advanced["confusion_heatmap"], list))
    status, dna_score = invoke_http("GET", "/api/dna/1")
    assert_test("Practice score explains its real-data formula and sample size", "average of solve rate" in dna_score["summary"]["debugging_iq_label"] and "saved attempt(s)" in dna_score["summary"]["debugging_iq_label"])
    conn = database.get_connection()
    owner_attempt_counts = conn.execute("SELECT COUNT(*) AS total,SUM(is_solved) AS solved,SUM(solved_independently) AS independent FROM attempts WHERE user_id=1").fetchone()
    correct_explanation_rows = conn.execute("""SELECT h.attempt_id,h.context_json FROM hint_logs h JOIN attempts a ON a.id=h.attempt_id
        WHERE a.user_id=1 AND h.hint_type='explain_fix'""").fetchall()
    correct_explanation_ids = set()
    for saved in correct_explanation_rows:
        try:
            if json.loads(saved["context_json"] or "{}").get("evaluation_status") == "correct": correct_explanation_ids.add(saved["attempt_id"])
        except (TypeError, ValueError): pass
    completed_f5_ids = {row[0] for row in conn.execute("SELECT attempt_id FROM bug_challenge_progress WHERE user_id=1 AND status='completed'").fetchall()}
    owner_completion_count = len(correct_explanation_ids | completed_f5_ids)
    unsolved_legacy_completion_flags = conn.execute("SELECT COUNT(*) FROM attempts WHERE user_id=1 AND is_completed=1 AND is_solved=0").fetchone()[0]
    conn.close()
    assert_test("Dashboard counts each saved attempt once and counts only evidence-backed completions", dna_score["summary"]["total_attempts"] == owner_attempt_counts["total"] and dna_score["summary"]["solved_count"] == owner_attempt_counts["solved"] and dna_score["summary"]["independent_solves"] == owner_attempt_counts["independent"] and dna_score["summary"]["completed_challenges"] == owner_completion_count)
    assert_test("Legacy completed flags on unsolved records do not inflate completed-challenge metrics", unsolved_legacy_completion_flags == 0 or dna_score["summary"]["completed_challenges"] < owner_attempt_counts["total"])
    assert_test("DNA detects repeated mistakes only from repeated saved records", all(item["attempts"] >= 2 for item in advanced["repeated_mistakes"]) and bool(advanced["repeated_mistakes"]))
    assert_test("Dashboard provides topic and difficulty accuracy from stored attempts", bool(dna_score["topic_performance"]) and bool(dna_score["difficulty_performance"]) and all(row["accuracy"] == round(100 * row["solved"] / row["attempts"], 1) for row in dna_score["topic_performance"] + dna_score["difficulty_performance"]))
    assert_test("Advanced dashboard includes real recent activity and a sample-size status", bool(advanced["recent_activity"]) and advanced["summary"]["data_status"])
    assert_test("Skill tree uses recorded evidence", all("evidence_count" in item for item in advanced["skill_tree"]))
    conn = database.get_connection()
    taxonomy_size = conn.execute("SELECT COUNT(*) FROM mistake_categories").fetchone()[0]
    conn.close()
    assert_test("Skill tree includes every taxonomy concept and locked concepts without inventing mastery", len(advanced["skill_tree"]) == taxonomy_size and any(item["status"] == "locked" for item in advanced["skill_tree"]))
    status, replay = invoke_http("GET", "/api/replay/1")
    assert_test("Error replay returns saved attempts", status == 200 and len(replay["attempts"]) > 0)
    status, diagnosis = invoke_http("POST", "/api/diagnose", {"code": "for i in range(1, n):\n total += i", "language": "python", "error": "wrong sum"})
    assert_test("Editor diagnosis labels heuristic source", status == 200 and diagnosis["source"] == "local_rule_engine")
    _, incomplete_explanation = invoke_http("POST", f"/api/attempts/{py_attempt_id}/explain", {"explanation": "The loop is wrong.", "lang": "en"})
    assert_test("Short Explain Your Fix response is classified as incomplete without completing the attempt", incomplete_explanation.get("evaluation_status") == "incomplete" and incomplete_explanation.get("is_completed") is False)
    empty_explanation_status, _ = invoke_http("POST", f"/api/attempts/{py_attempt_id}/explain", {"explanation": "", "lang": "en"})
    assert_test("Empty Explain Your Fix response is rejected by backend validation", empty_explanation_status == 400)
    _, partial_explanation = invoke_http("POST", f"/api/attempts/{py_attempt_id}/explain", {"explanation": "The loop appears wrong in my current code.", "lang": "en"})
    assert_test("Partially correct explanation is distinguished and permits another try", partial_explanation.get("evaluation_status") == "partially_correct" and partial_explanation.get("is_completed") is False)
    _, incorrect_explanation = invoke_http("POST", f"/api/attempts/{py_attempt_id}/explain", {"explanation": "The variable name caused it, and I renamed that variable in the program.", "lang": "en"})
    assert_test("Incorrect explanation is distinguished and does not complete the attempt", incorrect_explanation.get("evaluation_status") == "incorrect" and incorrect_explanation.get("is_completed") is False)
    wrong_owner_status, _ = invoke_http("POST", f"/api/attempts/{py_attempt_id}/explain", {"explanation": "The loop range excludes n because the endpoint is not included.", "lang": "en"}, as_user_id=2)
    assert_test("Explain Your Fix rejects a different user's attempt", wrong_owner_status == 404)
    status, explanation = invoke_http("POST", f"/api/attempts/{py_attempt_id}/explain", {"explanation": "The loop range excludes n. I changed the boundary so the loop includes n and adds every value.", "lang": "en"})
    assert_test("Explain Your Fix verifies contextual bug-to-fix understanding", status == 200 and explanation.get("evaluation_status") == "correct" and explanation.get("status_label") == "Correct Understanding" and 0 <= explanation["score"] <= 100 and explanation["feedback_source"] in ("local_rule_engine", "openai", "groq"))
    _, repeated_explanation = invoke_http("POST", f"/api/attempts/{py_attempt_id}/explain", {"explanation": "A different explanation after verification should not replace it.", "lang": "en"})
    assert_test("A completed explanation remains verified and is not repeatedly reevaluated", repeated_explanation.get("already_verified") is True and repeated_explanation.get("is_completed") is True)
    conn = database.get_connection()
    completion = conn.execute("SELECT is_completed FROM attempts WHERE id=?", (py_attempt_id,)).fetchone()[0]
    saved_explanation = conn.execute("SELECT student_response,context_json FROM hint_logs WHERE attempt_id=? AND hint_type='explain_fix' ORDER BY id DESC LIMIT 1", (py_attempt_id,)).fetchone()
    conn.close()
    assert_test("Only a correct explanation completes the verified attempt and its evaluation persists", completion == 1 and explanation.get("is_completed") is True and saved_explanation is not None and json.loads(saved_explanation["context_json"]).get("evaluation_status") == "correct")
    status, locked_answer = invoke_http("POST", f"/api/attempts/{py_attempt_id}/stage", {"stage": 8, "lang": "en"})
    assert_test("Stage 8 Answer completes without exposing code before explicit unlock", status == 200 and locked_answer.get("stage_name") == "Answer" and locked_answer.get("solution_unlocked") is False and locked_answer.get("solution_code") is None and locked_answer.get("total_stages") == 8)
    status, unlocked_answer = invoke_http("POST", f"/api/attempts/{py_attempt_id}/interact", {"action": "unlock_solution", "lang": "en"})
    assert_test("Complete Solution unlock returns the saved corrected code after Stage 8", status == 200 and unlocked_answer.get("solution_unlocked") is True and bool(unlocked_answer.get("solution_code")) and bool(unlocked_answer.get("original_code")))
    status, solution_refresh = invoke_http("GET", f"/api/attempts/{py_attempt_id}?stage=8&lang=en")
    saved_walkthrough = solution_refresh.get("attempt_stage", {}).get("solution_walkthrough", [])
    assert_test("Unlocked solution, line-by-line walkthrough, and completion persist on refresh", status == 200 and solution_refresh["attempt_stage"].get("solution_unlocked") is True and bool(solution_refresh["attempt_stage"].get("solution_code")) and len(saved_walkthrough) == len([line for line in solution_refresh["attempt_stage"]["solution_code"].splitlines() if line.strip()]) and all(item.get("explanation") for item in saved_walkthrough))
    foreign_solution_status, _ = invoke_http("GET", f"/api/attempts/{py_attempt_id}?stage=8&lang=en", as_user_id=2)
    assert_test("Another user cannot read an unlocked solution session", foreign_solution_status == 404)
    _, restored_fix_stage = invoke_http("GET", f"/api/attempts/{py_attempt_id}?stage=7&lang=en")
    assert_test("Saved explanation evaluation is restored when reopening the stage", restored_fix_stage.get("attempt_stage", {}).get("explain_evaluation", {}).get("evaluation_status") == "correct" and restored_fix_stage.get("attempt_stage", {}).get("explain_evaluation", {}).get("student_explanation") == saved_explanation["student_response"])
    status, restored_current = invoke_http("GET", f"/api/attempts/{py_attempt_id}?lang=en")
    restored_review = restored_current.get("attempt_stage", {}).get("explain_evaluation", {})
    assert_test("Completed attempt refresh restores explanation, score, status and feedback", status == 200 and restored_current["attempt_stage"]["stage_number"] == 7 and restored_current["attempt_stage"]["is_completed"] and restored_review.get("student_explanation") == saved_explanation["student_response"] and restored_review.get("score") == explanation.get("score") and restored_review.get("evaluation_status") == "correct" and bool(restored_review.get("feedback")))
    status, replay_after_explanation = invoke_http("GET", "/api/replay/1")
    replay_completed = next((item for item in replay_after_explanation.get("attempts", []) if item["attempt_id"] == py_attempt_id), {})
    explain_event = next((event for event in reversed(replay_completed.get("events", [])) if event["type"] == "explain_fix"), {})
    assert_test("Replay exposes completed stage and saved explanation evaluation", status == 200 and replay_completed.get("is_completed") and replay_completed.get("current_stage") == 7 and explain_event.get("score") == explanation.get("score") and explain_event.get("evaluation_status") == "correct" and bool(explain_event.get("feedback")))
    replay_event_types = [event.get("type") for event in replay_completed.get("events", [])]
    timed_replay_events = [event["created_at"] for event in replay_completed.get("events", []) if event.get("timestamp_available") and event.get("created_at")]
    assert_test("Replay includes saved tutor questions and student responses in chronological order", "debug_conversation" in replay_event_types and all(a <= b for a,b in zip(timed_replay_events,timed_replay_events[1:])) and any(event.get("student_note") and event.get("tutor") for event in replay_completed.get("events", [])))
    assert_test("Replay labels unsaved editor draft history honestly", "drafts between verifications were not recorded" in (replay_completed.get("replay_history_note") or ""))
    refreshed_dna_after_completion = invoke_http("GET", "/api/dna/1")[1]
    assert_test("DNA refresh reflects a newly completed explanation attempt", refreshed_dna_after_completion["summary"]["completed_challenges"] >= 1 and refreshed_dna_after_completion["summary"]["total_attempts"] == dna_score["summary"]["total_attempts"])
    status, mode_start = invoke_http("POST", "/api/attempts/start", {"user_id": 1, "problem_id": 3, "mode": "competition", "lang": "en"})
    assert_test("Competition mode creates a persistent challenge record", status == 201 and mode_start["mode"] == "competition")
    draft_id = mode_start["attempt_id"]
    draft_one, draft_two = "print('older draft')", "print('latest saved draft')"
    status, _ = invoke_http("POST", f"/api/attempts/{draft_id}/draft", {"code": draft_one, "revision": 1})
    status2, draft_saved = invoke_http("POST", f"/api/attempts/{draft_id}/draft", {"code": draft_two, "revision": 2})
    _, stale_draft = invoke_http("POST", f"/api/attempts/{draft_id}/draft", {"code": draft_one, "revision": 1})
    _, restored_draft = invoke_http("GET", f"/api/attempts/{draft_id}?lang=en")
    assert_test("Draft autosave restores the latest revision and ignores older writes", status == 200 and status2 == 200 and draft_saved.get("saved") and stale_draft.get("saved") is False and restored_draft["attempt_stage"]["current_code"] == draft_two)
    foreign_draft_status, _ = invoke_http("POST", f"/api/attempts/{draft_id}/draft", {"code": "foreign edit", "revision": 3}, as_user_id=2)
    assert_test("Draft autosave cannot write another user's attempt", foreign_draft_status == 404)
    unauthenticated_draft_status, _ = invoke_http("POST", f"/api/attempts/{draft_id}/draft", {"code": "guest edit", "revision": 4}, authenticated=False)
    assert_test("Draft autosave rejects unauthenticated writes", unauthenticated_draft_status == 401)
    status, blocked_hint = invoke_http("POST", f"/api/attempts/{mode_start['attempt_id']}/interact", {"action":"request_hint", "user_input":"hint", "lang":"en"})
    assert_test("Timed mode blocks hint requests on the backend", status == 409 and "disabled" in blocked_hint.get("error", ""))
    status, blocked_solution = invoke_http("GET", f"/api/attempts/{mode_start['attempt_id']}?stage=8")
    assert_test("Timed mode blocks direct solution reveal routes", status == 404 and "disabled" in blocked_solution.get("error", ""))
    _, mode_stage2 = invoke_http("POST", f"/api/attempts/{mode_start['attempt_id']}/interact", {"action":"submit_prediction", "user_input":"prediction", "lang":"en"})
    _, mode_stage5 = invoke_http("POST", f"/api/attempts/{mode_start['attempt_id']}/interact", {"action":"submit_investigation", "user_input":"line 3", "lang":"en"})
    assert_test("Timed competition flow reaches code attempt without revealing a hint", mode_stage2["stage_number"] == 2 and mode_stage5["stage_number"] == 5)
    status, board = invoke_http("GET", "/api/competition/leaderboard")
    assert_test("Competition scoreboard reads saved competition attempts", status == 200 and any(r["attempt_id"] == mode_start["attempt_id"] for r in board["leaderboard"]))
    conn = database.get_connection()
    conn.execute("UPDATE attempts SET created_at=datetime('now','-6 minutes') WHERE id=?", (mode_start["attempt_id"],))
    conn.commit(); conn.close()
    status, timed_out = invoke_http("POST", f"/api/attempts/{mode_start['attempt_id']}/verify", {"code":"print('too late')", "lang":"en"})
    assert_test("Expired competition attempts are rejected by the backend", status == 410 and "expired" in timed_out.get("error", "").lower())
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        for challenge_language in ("python", "c", "java"):
            for challenge_difficulty in ("Easy", "Medium", "Hard"):
                local_spec = server._local_bug_challenge(challenge_language, challenge_difficulty)
                if challenge_language in ("java", "c"):
                    solution_code = server._format_semicolon_challenge_code(local_spec["solution_code"], challenge_language)
                    buggy_code = server._format_semicolon_challenge_code(local_spec["buggy_code"], challenge_language)
                else:
                    solution_code = server._format_python_challenge_code(local_spec["solution_code"])
                    buggy_code = server._format_python_challenge_code(local_spec["buggy_code"])
                reference_result = code_executor.execute_code(solution_code, challenge_language, local_spec["test_cases"])
                buggy_result = code_executor.execute_code(buggy_code, challenge_language, local_spec["test_cases"])
                assert_test(f"{challenge_language} {challenge_difficulty} local challenge has passing reference and a compiled failing bug",
                    reference_result.get("is_compiled") is True and reference_result.get("all_passed") is True and buggy_result.get("is_compiled") is True and buggy_result.get("all_passed") is False)
                with patch("tutor_engine.generate_ai", return_value={"text": "", "provider": "local", "model": "local-fallback", "error": "test fallback"}):
                    status, challenge = invoke_http("POST", "/api/challenges/generate", {"language": challenge_language, "difficulty": challenge_difficulty})
                category = challenge.get("challenge", {}).get("category")
                bug_compiled = challenge.get("challenge", {}).get("validation", {}).get("buggy_code_compiled")
                assert_test(f"{challenge_language} {challenge_difficulty} challenge selection persists as an honestly labeled local challenge",
                    status == 200 and challenge["challenge"]["language"] == challenge_language and challenge["challenge"]["difficulty"] == challenge_difficulty and challenge["challenge"]["source"] == "local_curated" and challenge["challenge"]["validation"].get("reference_passed") is True and bug_compiled is (category != "syntax_errors") and challenge["challenge"]["validation"].get("at_least_one_test_failed") is True and challenge["challenge"]["validation"].get("intended_failure_observed") is True,
                    json.dumps({"status": status, "challenge": challenge.get("challenge"), "error": challenge.get("error")}))
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        status, challenge = invoke_http("POST", "/api/challenges/generate", {"problem_id": 1})
    assert_test("AI challenge falls back honestly to a validated curated local challenge", status == 200 and challenge["challenge"]["is_ai_generated"] is False and challenge["challenge"]["source"] == "local_curated" and challenge["challenge"]["validation"]["reference_passed"] is True)

    with patch("tutor_engine.generate_ai", return_value={"text": None, "provider": "local", "configured_provider": "openai", "response_status": "invalid_response", "provider_attempts": [{"provider": "openai", "outcome": "invalid_response", "parse_failure": "response_not_completed:incomplete:max_output_tokens"}]}):
        incomplete_challenge, incomplete_challenge_status = server._generate_diverse_bug_challenge(999001, {"language": "python", "difficulty": "Easy", "category": "off_by_one"})
    assert_test("Incomplete AI output is distinguished from validation failure and uses the validated fallback", incomplete_challenge_status == 200 and incomplete_challenge.get("challenge", {}).get("source") == "local_curated" and incomplete_challenge.get("challenge", {}).get("fallback_reason") == "AI returned an incomplete or unusable response" and incomplete_challenge.get("challenge", {}).get("validation", {}).get("reference_passed") is True, json.dumps({"status": incomplete_challenge_status, "challenge": incomplete_challenge.get("challenge"), "error": incomplete_challenge.get("error")}))

    ai_variant = {"title": "AI Variant Test", "category": "off_by_one", "buggy_code": "def sum_numbers(n):\n    return n\n\nprint('Sum = 5')\n"}
    with patch("tutor_engine.generate_ai", return_value={"text": json.dumps(ai_variant), "provider":"openai", "model":"test-model", "error":None}):
        status, generated_challenge = invoke_http("POST", "/api/challenges/generate", {"problem_id": 3})
    generated_validation = generated_challenge["challenge"].get("validation", {})
    assert_test("AI challenge is stored only after its category, compilation, and failing test validate", status == 200 and generated_challenge["challenge"]["is_ai_generated"] is True and generated_challenge["challenge"]["source"] == "openai" and generated_validation.get("reference_passed") is True and generated_validation.get("buggy_code_compiled") is True and generated_validation.get("at_least_one_test_failed") is True and generated_validation.get("intended_failure_observed") is True)
    status, generated_problem = invoke_http("GET", f"/api/problems/{generated_challenge['challenge']['id']}")
    assert_test("Verified generated challenge can be loaded into the existing curriculum", status == 200 and generated_problem["problem"]["title"] == "AI Variant Test")

    broken_variant = {"title": "Invalid AI Challenge", "category": "off_by_one", "buggy_code": "print(1)\n"}
    with patch("tutor_engine.generate_ai", return_value={"text": json.dumps(broken_variant), "provider":"openai", "model":"test-model", "error":None}):
        invalid_status, invalid_challenge = invoke_http("POST", "/api/challenges/generate", {"language":"java", "difficulty":"Easy"})
    assert_test("Incomplete AI challenge data is rejected and local validated fallback is labeled honestly", invalid_status == 200 and invalid_challenge["challenge"]["source"] == "local_curated" and invalid_challenge["challenge"]["fallback_reason"] == "AI challenge did not pass validation")

    print("\n--- Testing Personalized AI Challenge Diversity and Owner-Scoped Deduplication ---")
    parity_challenge = {"title": "Odd or Even Classification", "description": "Read an integer and print Even when divisible by two, otherwise print Odd.",
        "topic": "conditional logic", "category": "incorrect_conditions",
        "buggy_code": "n = int(input())\nif n % 2 == 1:\n    print('Even')\nelse:\n    print('Odd')\n",
        "solution_code": "n = int(input())\nif n % 2 == 0:\n    print('Even')\nelse:\n    print('Odd')\n",
        "test_cases": [{"input": "4\n", "expected_output": "Even"}, {"input": "5\n", "expected_output": "Odd"}]}
    with patch("tutor_engine.generate_ai", return_value={"text": json.dumps(parity_challenge), "provider": "openai", "model": "test-model", "error": None}):
        diversity_first_status, diversity_first = invoke_http("POST", "/api/challenges/generate", {"language": "python", "difficulty": "Easy", "topic": "incorrect_conditions"})
    first_challenge = diversity_first.get("challenge", {})
    first_start_status, first_start = invoke_http("POST", "/api/bug-challenges/start", {"challenge_id": first_challenge.get("id"), "lang": "en"})
    assert_test("AI generates a new topic-specific challenge only after reference and buggy execution validation",
        diversity_first_status == 200 and first_challenge.get("source") == "openai" and first_challenge.get("is_ai_generated") is True
        and first_challenge.get("category") == "incorrect_conditions" and first_challenge.get("validation", {}).get("reference_tests_passed") == 2
        and first_start_status == 201)

    delivery_challenge = {"title": "Distance Based Delivery Charge", "description": "Read a delivery distance and print the charge: four units below ten, six units from ten onwards.",
        "topic": "conditional logic", "category": "incorrect_conditions",
        "buggy_code": "distance = int(input())\ncharge = 4\nif distance > 10:\n    charge += 2\nprint(charge)\n",
        "solution_code": "distance = int(input())\ncharge = 4\nif distance >= 10:\n    charge += 2\nprint(charge)\n",
        "test_cases": [{"input": "10\n", "expected_output": "6"}, {"input": "12\n", "expected_output": "6"}, {"input": "5\n", "expected_output": "4"}]}
    with patch("tutor_engine.generate_ai", side_effect=[
        {"text": json.dumps(parity_challenge), "provider": "openai", "model": "test-model", "error": None},
        {"text": json.dumps(delivery_challenge), "provider": "openai", "model": "test-model", "error": None}]) as varied_ai:
        diversity_second_status, diversity_second = invoke_http("POST", "/api/challenges/generate", {"language": "python", "difficulty": "Easy", "category": "incorrect_conditions"})
    second_challenge = diversity_second.get("challenge", {})
    assert_test("A recent same-user duplicate is rejected and regenerated with a different statement, bug pattern, and structure",
        diversity_second_status == 200 and varied_ai.call_count == 2 and second_challenge.get("source") == "openai"
        and second_challenge.get("title") == "Distance Based Delivery Charge" and second_challenge.get("id") != first_challenge.get("id"))

    with patch("tutor_engine.generate_ai", return_value={"text": json.dumps(delivery_challenge), "provider": "openai", "model": "test-model", "error": None}):
        other_owner_status, other_owner_challenge = invoke_http("POST", "/api/challenges/generate", {"language": "python", "difficulty": "Easy", "category": "incorrect_conditions"}, as_user_id=2)
    assert_test("Challenge duplicate history is scoped to the authenticated owner", other_owner_status == 200
        and other_owner_challenge.get("challenge", {}).get("source") == "openai"
        and other_owner_challenge.get("challenge", {}).get("title") == "Distance Based Delivery Charge")

    array_challenge = {"title": "Select a Shelf Item", "description": "Read a zero based position and print the item stored at that position in a three item shelf.",
        "topic": "arrays", "category": "array_indexing",
        "buggy_code": "items = [9, 4, 7]\nposition = int(input())\nprint(items[position + 1])\n",
        "solution_code": "items = [9, 4, 7]\nposition = int(input())\nprint(items[position])\n",
        "test_cases": [{"input": "0\n", "expected_output": "9"}, {"input": "1\n", "expected_output": "4"}]}
    with patch("tutor_engine.generate_ai", return_value={"text": json.dumps(array_challenge), "provider": "groq", "model": "test-model", "error": None}):
        array_ai_status, array_ai_challenge = invoke_http("POST", "/api/challenges/generate", {"language": "python", "difficulty": "Easy", "category": "array_indexing"})
    assert_test("AI challenge generation supports a distinct selected bug category and structure", array_ai_status == 200
        and array_ai_challenge.get("challenge", {}).get("category") == "array_indexing"
        and array_ai_challenge.get("challenge", {}).get("source") == "groq"
        and array_ai_challenge.get("challenge", {}).get("validation", {}).get("reference_passed") is True)

    java_ai_challenge = {"title": "Compare Two Scores", "description": "Read two scores and print the larger score.",
        "topic": "comparison conditions", "category": "incorrect_conditions",
        "buggy_code": "import java.util.Scanner;\npublic class Main {\n public static void main(String[] args) {\n  Scanner s = new Scanner(System.in);\n  int a = s.nextInt();\n  int b = s.nextInt();\n  System.out.println(a < b ? a : b);\n }\n}\n",
        "solution_code": "import java.util.Scanner;\npublic class Main {\n public static void main(String[] args) {\n  Scanner s = new Scanner(System.in);\n  int a = s.nextInt();\n  int b = s.nextInt();\n  System.out.println(a > b ? a : b);\n }\n}\n",
        "test_cases": [{"input": "4 7\n", "expected_output": "7"}, {"input": "9 3\n", "expected_output": "9"}]}
    with patch("tutor_engine.generate_ai", return_value={"text": json.dumps(java_ai_challenge), "provider": "openai", "model": "test-model", "error": None}):
        java_ai_status, java_ai_result = invoke_http("POST", "/api/challenges/generate", {"language": "java", "difficulty": "Easy", "topic": "incorrect_conditions"}, as_user_id=2)
    assert_test("AI-generated Java challenge preserves the selected language and validates its reference and tests",
        java_ai_status == 200 and java_ai_result.get("challenge", {}).get("language") == "java"
        and java_ai_result.get("challenge", {}).get("source") == "openai"
        and java_ai_result.get("challenge", {}).get("validation", {}).get("reference_tests_passed") == 2)

    with patch("tutor_engine.generate_ai", return_value={"text": "not valid json", "provider": "openai", "model": "test-model", "error": None}) as malformed_ai:
        malformed_status, malformed_fallback = invoke_http("POST", "/api/challenges/generate", {"language": "java", "difficulty": "Easy", "category": "syntax_errors"})
    assert_test("Invalid AI challenge responses use bounded regeneration then honestly labeled validated local fallback",
        malformed_status == 200 and malformed_ai.call_count == 3 and malformed_fallback.get("challenge", {}).get("source") == "local_curated"
        and malformed_fallback.get("challenge", {}).get("is_ai_generated") is False
        and malformed_fallback.get("challenge", {}).get("validation", {}).get("reference_passed") is True)

    bad_selection_status, _ = invoke_http("POST", "/api/challenges/generate", {"language":"ruby", "difficulty":"Easy"})
    assert_test("Bug challenge endpoint rejects unsupported programming languages", bad_selection_status == 400)

    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}):
        flow_status, flow_challenge = invoke_http("POST", "/api/challenges/generate", {"language":"java", "difficulty":"Medium", "category":"off_by_one"})
    flow_id = flow_challenge["challenge"]["id"]
    flow_start_status, flow_attempt = invoke_http("POST", "/api/attempts/start", {"user_id":1, "problem_id":flow_id, "lang":"en"})
    flow_attempt_id = flow_attempt.get("attempt_id")
    buggy_program = flow_attempt.get("problem", {}).get("buggy_code", "")
    flow_bad_status, flow_bad = invoke_http("POST", f"/api/attempts/{flow_attempt_id}/verify", {"code":buggy_program, "lang":"en"})
    conn = database.get_connection()
    completed_before_explanation = conn.execute("SELECT is_completed FROM attempts WHERE id=?", (flow_attempt_id,)).fetchone()[0]
    conn.close()
    assert_test("Bug challenge starts a real persisted attempt; its injected version fails and remains incomplete", flow_status == 200 and flow_start_status == 201 and flow_bad_status == 200 and flow_bad.get("is_solved") is False and completed_before_explanation == 0)
    conn = database.get_connection()
    flow_solution = conn.execute("SELECT solution_code FROM problems WHERE id=?", (flow_id,)).fetchone()["solution_code"]
    conn.close()
    flow_good_status, flow_good = invoke_http("POST", f"/api/attempts/{flow_attempt_id}/verify", {"code":flow_solution, "lang":"en"})
    _, flow_explanation = invoke_http("POST", f"/api/attempts/{flow_attempt_id}/explain", {"explanation":"The loop stopped before n, so the last value was skipped. Extending the boundary includes it, and the tests verify the expected sum.", "lang":"en"})
    conn = database.get_connection()
    flow_persisted = conn.execute("SELECT is_completed FROM attempts WHERE id=?", (flow_attempt_id,)).fetchone()[0]
    conn.close()
    assert_test("Bug challenge flow reaches Explain Your Fix and completes only after correct explanation", flow_good_status == 200 and flow_good.get("is_solved") is True and flow_explanation.get("evaluation_status") == "correct" and flow_explanation.get("is_completed") is True and flow_persisted == 1)

    # Feature 5's dedicated prediction, bug-identification, scoring, and replay flow.
    with patch("tutor_engine.generate_ai", return_value={"text": "", "provider": "local", "model": "local-fallback", "error": "offline"}):
        topic_status, topic_challenge = invoke_http("POST", "/api/challenges/generate", {"language": "java", "difficulty": "Medium", "category": "data_structure_errors"})
    assert_test("Feature 5 can select a Java data-structure topic without a live AI provider", topic_status == 200 and topic_challenge["challenge"]["language"] == "java" and topic_challenge["challenge"]["category"] == "data_structure_errors" and topic_challenge["challenge"]["source"] == "local_curated")
    with patch("server.secrets.choice", return_value="recursion_misunderstanding"), patch("tutor_engine.generate_ai", return_value={"text": "", "provider": "local", "model": "local-fallback", "error": "offline"}):
        random_status, random_challenge = invoke_http("POST", "/api/challenges/generate", {"language": "java", "difficulty": "Hard", "category": "random"})
    assert_test("Feature 5 random selection chooses and validates a topic from the local template set", random_status == 200 and random_challenge["challenge"]["category"] == "recursion_misunderstanding" and random_challenge["challenge"]["source"] == "local_curated" and random_challenge["challenge"]["validation"]["reference_passed"] is True)
    f5_start_status, f5_start = invoke_http("POST", "/api/bug-challenges/start", {"challenge_id": topic_challenge["challenge"]["id"], "lang": "en"})
    f5_id = f5_start.get("attempt_id")
    assert_test("Feature 5 starts an owner-linked attempt and returns no answer or hidden test data", f5_start_status == 201 and f5_start.get("is_bug_challenge") is True and "solution_code" not in f5_start and "expected_prediction" not in json.dumps(f5_start) and "test_cases" not in f5_start)
    formatted_java = server._format_semicolon_challenge_code('class Main { void run() { for (int i=0; i<2; i++) { System.out.println("a;b"); } // ; comment\n int value=1; } }', "java")
    assert_test("Java challenge formatting separates statements but preserves for headers, strings, and comments", 'for (int i=0; i<2; i++)' in formatted_java and '"a;b");\n' in formatted_java and '// ; comment\n' in formatted_java and 'int value=1;\n' in formatted_java)
    formatted_c = server._format_semicolon_challenge_code('int main(void) { for (int i=0; i<2; i++) { printf("%d;%d", i, i); } // ; comment\n return 0; }', "c")
    assert_test("C challenge formatting separates statements but preserves for headers, strings, and comments", 'for (int i=0; i<2; i++)' in formatted_c and 'printf("%d;%d", i, i);\n' in formatted_c and '// ; comment\n' in formatted_c and 'return 0;\n' in formatted_c)
    formatted_python = server._format_python_challenge_code("if n % 2 == 0: print('Even'); print('Odd')")
    invalid_python = "if n > 0\n    print(n)"
    commented_python = "n = 2; print(n)  # keep this comment\n"
    assert_test("Python challenge formatting expands compact valid suites without deleting comments or changing invalid syntax", formatted_python == "if n % 2 == 0:\n    print('Even')\n    print('Odd')" and server._format_python_challenge_code(invalid_python) == invalid_python and server._format_python_challenge_code(commented_python) == commented_python)
    f5_java_buggy = f5_start.get("problem", {}).get("buggy_code", "")
    assert_test("Generated Java challenge code opens on multiple lines", f5_start_status == 201 and f5_java_buggy.count("\n") >= 4 and "queue.add(10);\n" in f5_java_buggy)
    with patch.dict(os.environ, {"AI_PROVIDER": "local", "OPENAI_API_KEY": "", "GROQ_API_KEY": ""}), patch("tutor_engine.generate_ai", return_value={"text": "", "provider": "local", "model": "local-fallback", "error": "offline"}):
        c_format_status, c_format_challenge = invoke_http("POST", "/api/challenges/generate", {"language": "c", "difficulty": "Easy", "category": "incorrect_conditions"}, as_user_id=2)
        py_format_status, py_format_challenge = invoke_http("POST", "/api/challenges/generate", {"language": "python", "difficulty": "Easy", "category": "incorrect_conditions"}, as_user_id=2)
    c_format_start_status, c_format_start = invoke_http("POST", "/api/bug-challenges/start", {"challenge_id": c_format_challenge["challenge"]["id"], "lang": "en"})
    py_format_start_status, py_format_start = invoke_http("POST", "/api/bug-challenges/start", {"challenge_id": py_format_challenge["challenge"]["id"], "lang": "en"})
    c_format_code = c_format_start.get("problem", {}).get("buggy_code", "")
    py_format_code = py_format_start.get("problem", {}).get("buggy_code", "")
    assert_test("Generated C challenge code starts new lines after statements", c_format_status == 200 and c_format_start_status == 201 and 'printf("Even\\n");\n' in c_format_code and c_format_code.count("\n") >= 4)
    assert_test("Generated Python challenge code stays on readable separate statements", py_format_status == 200 and py_format_start_status == 201 and py_format_code.count("\n") >= 1 and "n = int(input())\n" in py_format_code)
    _, f5_premature_verify = invoke_http("POST", f"/api/attempts/{f5_id}/verify", {"code": f5_start["problem"]["buggy_code"], "lang": "en"})
    assert_test("Feature 5 requires prediction and bug identification before code verification", "correct behavior prediction" in f5_premature_verify.get("error", ""))
    wrong_prediction_status, wrong_prediction = invoke_http("POST", f"/api/bug-challenges/{f5_id}/prediction", {"prediction": "not the observed result"})
    assert_test("Wrong prediction is rejected without revealing expected output", wrong_prediction_status == 200 and wrong_prediction.get("correct") is False and "10" not in json.dumps(wrong_prediction))
    correct_prediction_status, correct_prediction = invoke_http("POST", f"/api/bug-challenges/{f5_id}/prediction", {"prediction": "30"})
    assert_test("Correct output prediction is accepted", correct_prediction_status == 200 and correct_prediction.get("correct") is True)
    wrong_identification_status, wrong_identification = invoke_http("POST", f"/api/bug-challenges/{f5_id}/identification", {"line": 1, "explanation": "This import line is what changes the result."})
    assert_test("Incorrect faulty-line identification is not accepted", wrong_identification_status == 200 and wrong_identification.get("correct") is False)
    correct_identification_status, correct_identification = invoke_http("POST", f"/api/bug-challenges/{f5_id}/identification", {"line": 6, "explanation": "The queue removes the newest item instead of the first item, so the output order is wrong."})
    assert_test("Correct faulty-line identification and reasoning are saved", correct_identification_status == 200 and correct_identification.get("correct") is True)
    with patch("tutor_engine.generate_ai", return_value={"text": "", "provider": "local", "model": "local-fallback", "error": "offline"}):
        _, f5_hint = invoke_http("POST", f"/api/attempts/{f5_id}/interact", {"action": "request_hint", "user_input": "Requested progressive hint", "lang": "en"})
    assert_test("Feature 5 keeps the existing Hint Ladder available", f5_hint.get("stage_number") == 3 and f5_hint.get("hints_used") == 1 and f5_hint.get("hint"))
    java_medium = server._local_bug_challenge("java", "Medium", "data_structure_errors")
    f5_bad_status, f5_bad = invoke_http("POST", f"/api/attempts/{f5_id}/verify", {"code": java_medium["buggy_code"], "lang": "en"})
    assert_test("Feature 5 records a wrong code attempt and does not complete on compile alone", f5_bad_status == 200 and f5_bad.get("is_solved") is False and f5_bad.get("challenge_result", {}).get("status") == "in_progress" and f5_bad["challenge_result"].get("code_attempts") == 1)
    f5_good_status, f5_good = invoke_http("POST", f"/api/attempts/{f5_id}/verify", {"code": java_medium["solution_code"], "lang": "en"})
    f5_result = f5_good.get("challenge_result", {})
    assert_test("Feature 5 completes only after every saved test passes and calculates a transparent score", f5_good_status == 200 and f5_good.get("is_solved") is True and f5_result.get("status") == "completed" and f5_result.get("verification_passed") is True and f5_result.get("passed_tests") == f5_result.get("total_tests") and f5_result.get("score") == 87 and "formula" in f5_result.get("score_breakdown", {}))
    f5_replay_status, f5_replay = invoke_http("GET", f"/api/bug-challenges/{f5_id}")
    f5_history_status, f5_history = invoke_http("GET", "/api/bug-challenges/history")
    f5_history_entry = next((item for item in f5_history.get("attempts", []) if item.get("attempt_id") == f5_id), {})
    assert_test("Feature 5 challenge and submitted code survive refresh and appear in owner history", f5_replay_status == 200 and f5_history_status == 200 and f5_replay.get("status") == "completed" and f5_replay.get("current_code") == java_medium["solution_code"] and f5_history_entry.get("score") == 87 and f5_history_entry.get("bug_identification_correct") is True)
    f5_insights_status, f5_insights = invoke_http("GET", "/api/insights/1")
    f5_story = next((item for item in f5_insights.get("error_replay", []) if item.get("attempt_id") == f5_id), {})
    f5_story_events = f5_story.get("events", [])
    f5_story_times = [event["created_at"] for event in f5_story_events if event.get("timestamp_available") and event.get("created_at")]
    assert_test("Error Replay includes Feature 5 prediction, identification, score, and final code", f5_insights_status == 200 and f5_story.get("challenge", {}).get("status") == "completed" and f5_story.get("challenge", {}).get("score") == 87 and f5_story.get("challenge", {}).get("final_code") == java_medium["solution_code"] and sum(event.get("type") == "challenge_prediction" for event in f5_story_events) == 2 and sum(event.get("type") == "challenge_identification" for event in f5_story_events) == 2)
    assert_test("Feature 5 replay events are returned chronologically", all(a <= b for a,b in zip(f5_story_times,f5_story_times[1:])))
    conn = database.get_connection()
    f5_user_attempt_count = conn.execute("SELECT COUNT(*) FROM attempts WHERE user_id=1").fetchone()[0]
    conn.close()
    assert_test("Feature 5 dashboard counts each linked challenge as one attempt", f5_insights.get("summary", {}).get("total_attempts") == f5_user_attempt_count)
    foreign_f5_status, _ = invoke_http("GET", f"/api/bug-challenges/{f5_id}", as_user_id=2)
    foreign_f5_prediction_status, _ = invoke_http("POST", f"/api/bug-challenges/{f5_id}/prediction", {"prediction": "10"}, as_user_id=2)
    foreign_insights_status, _ = invoke_http("GET", "/api/insights/1", as_user_id=2)
    assert_test("Feature 5 attempt, prediction, and replay data reject other users", foreign_f5_status == 404 and foreign_f5_prediction_status == 404 and foreign_insights_status == 404)
    feature5_html = open(os.path.join(os.path.dirname(__file__), "static", "index.html"), encoding="utf-8").read()
    feature5_js = open(os.path.join(os.path.dirname(__file__), "static", "feature5.js"), encoding="utf-8").read()
    assert_test("Feature 5 UI is loaded in the existing catalog and workspace", 'id="btn-ai-challenge"' in feature5_html and 'id="feature5-challenge-panel"' in feature5_html and 'id="bug-challenge-history"' in feature5_html and 'src="feature5.js"' in feature5_html)
    assert_test("Feature 5 UI calls owner-scoped prediction, identification, history and replay flows", all(route in feature5_js for route in ("/prediction", "/identification", "/api/bug-challenges/history", "resumeFeature5Attempt")))
    dashboard_html = open(os.path.join(os.path.dirname(__file__), "static", "index.html"), encoding="utf-8").read()
    dashboard_js = open(os.path.join(os.path.dirname(__file__), "static", "app.js"), encoding="utf-8").read()
    assert_test("Features 6 and 8 expose measured strengths, topic/difficulty, recent-activity and completion metrics", all(element in dashboard_html for element in ('id="dna-strengths"', 'id="topic-performance"', 'id="difficulty-performance"', 'id="recent-activity"', 'id="val-completed-challenges"', 'id="val-average-runs-solved"')) and "renderStrengths(dna.strengths" in dashboard_js)
    assert_test("Feature 7 replay renders saved tutor replies and routes Feature 5 attempts to challenge restore", "event.tutor" in dashboard_js and "window.resumeFeature5Attempt" in dashboard_js and "Original code" in dashboard_js)
    f1112_js = open(os.path.join(os.path.dirname(__file__), "static", "features11_12.js"), encoding="utf-8").read()
    logout_js = dashboard_js.split("async function handleLogout()", 1)[1].split("// ==================== FEATURE 1", 1)[0]
    assert_test("Account sign-up, sign-in, and sign-out reload account-scoped browser state while preserving attempt pointers",
                dashboard_js.count("window.location.reload()") >= 3 and "localStorage.removeItem(STATE.activeAttemptStorageKey)" not in logout_js)
    assert_test("Shared practice leaderboard is clearly labeled global and interview replies name their actual provider",
                "Global Timed Practice Leaderboard" in dashboard_html and "OpenAI" in f1112_js and "Groq AI" in f1112_js and "Local Mode" in f1112_js)

    class FakeResponse:
        def __init__(self, payload, status=200): self.payload = payload; self.status = status
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return self.payload

    # Request-level provider diagnostics and bounded-retry regression checks.
    openai_ok = b'{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"OpenAI retry reply"}]}]}'
    retry_counts = {"429": 0, "503": 0, "timeout": 0}
    def retryable_provider_mock(kind):
        def respond(request, timeout=None):
            if kind in ("429", "503") and retry_counts[kind] == 0:
                retry_counts[kind] += 1
                status = int(kind)
                raise urllib.error.HTTPError(request.full_url, status, "mocked", {}, None)
            if kind == "timeout" and retry_counts[kind] == 0:
                retry_counts[kind] += 1
                raise urllib.error.URLError(TimeoutError("mocked timeout"))
            return FakeResponse(openai_ok)
        return respond

    for retry_kind in ("429", "503", "timeout"):
        retry_counts[retry_kind] = 0
        with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret-never-log", "GROQ_API_KEY": "", "FREE_ONLY_MODE": "true"}):
            with patch("tutor_engine.urllib.request.urlopen", side_effect=retryable_provider_mock(retry_kind)) as retry_mock:
                retry_result = tutor_engine.generate_ai("safe test prompt")
        assert_test(f"OpenAI {retry_kind} gets one bounded retry and preserves provider attribution",
                    retry_result["provider"] == "openai" and retry_result["text"] == "OpenAI retry reply"
                    and len(retry_result["provider_attempts"]) == 2 and retry_mock.call_count == 2)

    for auth_status in (401, 403):
        with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret-never-log", "GROQ_API_KEY": "", "FREE_ONLY_MODE": "true"}):
            with patch("tutor_engine.urllib.request.urlopen", side_effect=lambda request, timeout=None, status=auth_status: (_ for _ in ()).throw(urllib.error.HTTPError(request.full_url, status, "mocked", {}, None))) as auth_mock:
                auth_result = tutor_engine.generate_ai("safe test prompt")
        assert_test(f"OpenAI HTTP {auth_status} is not retried and reports Local as final provider",
                    auth_result["provider"] == "local" and len(auth_result["provider_attempts"]) == 1
                    and auth_result["provider_attempts"][0]["http_status"] == auth_status and auth_mock.call_count == 1)

    diagnostic_log = io.StringIO()
    diagnostic_handler = logging.StreamHandler(diagnostic_log)
    ai_logger = logging.getLogger("codementor.ai")
    ai_logger.addHandler(diagnostic_handler)
    try:
        with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret-never-log", "GROQ_API_KEY": "", "FREE_ONLY_MODE": "true"}):
            with patch("tutor_engine.urllib.request.urlopen", return_value=FakeResponse(b"not-json")):
                invalid_api_result = tutor_engine.generate_ai("safe test prompt", requested_feature="test_malformed_response")
    finally:
        ai_logger.removeHandler(diagnostic_handler)
    diagnostic_text = diagnostic_log.getvalue()
    assert_test("Malformed provider response is distinguished from transport failure and logs contain no secret",
                invalid_api_result["provider"] == "local" and invalid_api_result["response_status"] == "invalid_response"
                and invalid_api_result["provider_attempts"][0]["outcome"] == "invalid_response"
                and invalid_api_result["final_provider"] == "local"
                and invalid_api_result["requested_feature"] == "test_malformed_response"
                and invalid_api_result["request_timestamp"] and invalid_api_result["duration_ms"] >= 0
                and invalid_api_result["provider_attempts"][0]["parse_failure"].startswith("malformed_json")
                and "invalid_response" in diagnostic_text and "test-secret-never-log" not in diagnostic_text)

    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret", "GROQ_API_KEY": "", "FREE_ONLY_MODE": "true"}):
        with patch("tutor_engine.urllib.request.urlopen", return_value=FakeResponse(b'{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":""}]}]}')):
            empty_api_result = tutor_engine.generate_ai("safe test prompt", requested_feature="test_empty_response")
    assert_test("Empty OpenAI content is distinguished and safely falls back to Local",
                empty_api_result["provider"] == "local" and empty_api_result["response_status"] == "invalid_response"
                and empty_api_result["provider_attempts"][0]["parse_failure"] == "empty_text_response")

    def openai_rate_limited(request, timeout=None):
        if "api.openai.com" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 429, "limited", {}, None)
        return FakeResponse(b'{"choices":[{"message":{"content":"Groq test reply"}}]}')

    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "unit-test", "GROQ_API_KEY": "unit-test", "FREE_ONLY_MODE": "true"}):
        with patch("tutor_engine.urllib.request.urlopen", side_effect=openai_rate_limited):
            fallback = tutor_engine.generate_ai("test prompt", "test system")
    assert_test("OpenAI 429 falls back to Groq with truthful attribution", fallback["text"] == "Groq test reply" and fallback["provider"] == "groq")

    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "unit-test", "GROQ_API_KEY": "unit-test", "FREE_ONLY_MODE": "true"}):
        with patch("tutor_engine.urllib.request.urlopen", side_effect=urllib.error.URLError("offline")):
            failed = tutor_engine.generate_ai("test prompt", "test system")
    assert_test("Both AI providers failing reports local fallback, not live AI", failed["text"] is None and failed["provider"] == "local")

    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "unit-test", "GROQ_API_KEY": "unit-test", "FREE_ONLY_MODE": "false"}):
        with patch("tutor_engine.urllib.request.urlopen") as disabled_network:
            disabled = tutor_engine.generate_ai("test prompt", "test system")
    assert_test("Disabling free-only mode disables external calls", disabled["provider"] == "local" and disabled["text"] is None and not disabled_network.called)

    captured_openai_requests = []
    captured_openai_timeouts = []
    def capture_openai_request(request, timeout=None):
        captured_openai_requests.append(request)
        captured_openai_timeouts.append(timeout)
        return FakeResponse(b'{"status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"OpenAI API test reply"}]}]}')
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret-header-only", "GROQ_API_KEY": "", "FREE_ONLY_MODE": "true"}):
        with patch("tutor_engine.urllib.request.urlopen", side_effect=capture_openai_request):
            header_test = tutor_engine.generate_ai("connectivity check", "test tutor")
    openai_request = captured_openai_requests[0] if captured_openai_requests else None
    assert_test("OpenAI API request reads a real candidate and sends bearer auth and parses Responses API output", header_test.get("provider") == "openai" and header_test.get("text") == "OpenAI API test reply" and openai_request is not None and openai_request.get_header("Authorization") == "Bearer test-secret-header-only")
    openai_payload = json.loads(openai_request.data.decode("utf-8")) if openai_request is not None else {}
    assert_test("OpenAI Responses request uses configured model, instructions, input, and disables response storage",
        openai_request is not None and openai_request.full_url == "https://api.openai.com/v1/responses"
        and openai_payload.get("model") == tutor_engine.OPENAI_MODEL
        and openai_payload.get("instructions") == "test tutor"
        and openai_payload.get("input") == "connectivity check"
        and openai_payload.get("max_output_tokens") == 600 and openai_payload.get("store") is False)
    assert_test("OpenAI API key is absent from the request URL", openai_request is not None and "test-secret-header-only" not in openai_request.full_url and "?key=" not in openai_request.full_url)
    assert_test("AI provider request and total generation deadline are bounded at 60 seconds", tutor_engine.AI_TIMEOUT_SECONDS == 60 and len(captured_openai_timeouts) == 1 and 59.0 <= captured_openai_timeouts[0] <= 60.0)
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret-header-only", "GROQ_API_KEY": "", "FREE_ONLY_MODE": "true"}):
        with patch("tutor_engine.urllib.request.urlopen", side_effect=capture_openai_request):
            challenge_budget_result = tutor_engine.generate_ai("Return one small validated challenge as JSON", requested_feature="ai_challenge_generation")
    challenge_budget_request = captured_openai_requests[-1] if captured_openai_requests else None
    challenge_budget_payload = json.loads(challenge_budget_request.data.decode("utf-8")) if challenge_budget_request is not None else {}
    assert_test("Challenge generation receives a larger bounded output budget than ordinary tutor replies",
        challenge_budget_result.get("provider") == "openai" and challenge_budget_request is not None
        and challenge_budget_payload.get("max_output_tokens") == tutor_engine.AI_CHALLENGE_MAX_OUTPUT_TOKENS
        and challenge_budget_payload.get("max_output_tokens") > 600)
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "test-secret", "GROQ_API_KEY": "", "FREE_ONLY_MODE": "true"}):
        with patch("tutor_engine.urllib.request.urlopen", return_value=FakeResponse(b'{"status":"incomplete","incomplete_details":{"reason":"max_output_tokens"},"output":[{"type":"message","content":[{"type":"output_text","text":"partial answer"}]}]}')):
            incomplete_result = tutor_engine.generate_ai("safe test prompt", requested_feature="test_incomplete_response")
    assert_test("Incomplete OpenAI Responses output is rejected with its safe incomplete reason", incomplete_result["provider"] == "local" and incomplete_result["response_status"] == "invalid_response" and incomplete_result["provider_attempts"][0]["parse_failure"] == "response_not_completed:incomplete:max_output_tokens")
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "configured-test-key", "GROQ_API_KEY": "configured-groq-key", "FREE_ONLY_MODE": "true"}):
        openai_first_order = tutor_engine._provider_order()
    with patch.dict(os.environ, {"AI_PROVIDER": "groq", "OPENAI_API_KEY": "configured-test-key", "GROQ_API_KEY": "configured-groq-key", "FREE_ONLY_MODE": "true"}):
        groq_first_order = tutor_engine._provider_order()
    assert_test("OpenAI is primary by default and Groq remains optional fallback", openai_first_order == ["openai", "groq"] and groq_first_order == ["groq", "openai"])
    with patch.dict(os.environ, {"AI_PROVIDER": "openai", "OPENAI_API_KEY": "your_openai_api_key_here", "GROQ_API_KEY": "your_groq_api_key_here", "FREE_ONLY_MODE": "true"}):
        placeholders = tutor_engine.get_ai_provider_status()
        with patch("tutor_engine.urllib.request.urlopen") as placeholder_network:
            placeholder_result = tutor_engine.generate_ai("test", "test")
    assert_test(".env.example placeholders are not treated as configured provider credentials", placeholders["configured"] is False and placeholders["providers"]["openai"]["configured"] is False and placeholder_result["provider"] == "local" and not placeholder_network.called)

    new_hash = database.hash_password("strong-password")
    assert_test("New passwords use PBKDF2 and verify correctly", new_hash.startswith("pbkdf2_sha256$") and database.verify_password("strong-password", new_hash))
    assert_test("Existing SHA-256 passwords remain verifiable", database.verify_password("password123", __import__("hashlib").sha256(b"password123").hexdigest()))

    print(f"\n=======================================================")
    print(f"ALL {passed_count}/{total_count} INTEGRATION TESTS PASSED PERFECTLY!")
    print(f"=======================================================\n")
    return True

if __name__ == "__main__":
    run_tests()
