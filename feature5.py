"""Isolated persistence and scoring helpers for Feature 5 bug challenges."""

import difflib
import json
import sqlite3

import code_executor
import database
import analyzer
import tutor_engine


def _changed_line_range(buggy_code, reference_code):
    buggy = str(buggy_code or "").splitlines()
    reference = str(reference_code or "").splitlines()
    changed = []
    for tag, start, end, ref_start, ref_end in difflib.SequenceMatcher(a=buggy, b=reference).get_opcodes():
        if tag != "equal":
            if start == end:
                changed.append(max(1, start))
            else:
                changed.extend(range(start + 1, end + 1))
    line = min(changed) if changed else 1
    return line, max(changed) if changed else line


def _predictable_result(execution_result):
    if not execution_result.get("is_compiled"):
        return "compiler error"
    tests = execution_result.get("test_results") or []
    if not tests:
        return ""
    first = tests[0]
    output = code_executor.normalize_output(first.get("actual_output") or "")
    if first.get("error") and not output:
        return "runtime error"
    return output


def _public_session(row):
    return {
        "attempt_id": row["attempt_id"], "challenge_id": row["challenge_id"],
        "title": row["title"], "description": row["description"], "language": row["language"],
        "difficulty": row["difficulty"], "topic": row["topic"], "bug_category": row["bug_category"],
        "original_code": row["original_code"], "current_code": row["current_code"],
        "prediction": row["prediction"], "prediction_correct": bool(row["prediction_correct"]),
        "prediction_attempts": row["prediction_attempts"], "bug_line": row["bug_line"] if row["status"] == "completed" else None,
        "bug_identification_correct": None if row["bug_line_correct"] is None else bool(row["bug_line_correct"]),
        "bug_explanation": row["bug_explanation"], "code_attempts": row["code_attempts"],
        "hints_used": row["hints_used"], "verification_passed": bool(row["verification_passed"]),
        "score": row["score"], "status": row["status"], "created_at": row["created_at"],
        "completed_at": row["completed_at"], "time_taken_seconds": row["time_taken_seconds"],
        "score_breakdown": json.loads(row["score_breakdown_json"] or "{}"),
    }


def start_challenge(user_id, challenge_id, lang="en"):
    try:
        challenge_id = int(challenge_id)
    except (TypeError, ValueError):
        return {"error": "Choose a valid challenge."}, 400
    conn = database.get_connection()
    problem = conn.execute("SELECT * FROM problems WHERE id=?", (challenge_id,)).fetchone()
    if not problem:
        conn.close()
        return {"error": "Challenge not found."}, 404
    problem = dict(problem)
    try:
        tests = json.loads(problem.get("test_cases_json") or "[]")
    except (TypeError, json.JSONDecodeError):
        tests = []
    if not tests or not isinstance(tests, list):
        conn.close()
        return {"error": "This challenge has no validated test cases."}, 409
    reference_result = code_executor.execute_code(problem["solution_code"], problem["language"], tests)
    buggy_result = code_executor.execute_code(problem["buggy_code"], problem["language"], tests)
    if not reference_result.get("is_compiled") or not reference_result.get("all_passed"):
        conn.close()
        return {"error": "The reference solution did not pass its saved tests."}, 503
    is_syntax = problem["mistake_category_slug"] == "syntax_errors"
    if buggy_result.get("all_passed") or (is_syntax and buggy_result.get("is_compiled")) or (not is_syntax and not buggy_result.get("is_compiled")):
        conn.close()
        return {"error": "The selected challenge no longer reproduces its intended bug."}, 503

    classification = analyzer.classify_mistakes(problem["buggy_code"], problem["language"], expected_category=problem["mistake_category_slug"])
    categories_json = json.dumps(classification.get("categories", []))
    start, end = _changed_line_range(problem["buggy_code"], problem["solution_code"])
    expected_prediction = _predictable_result(buggy_result)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("""INSERT INTO attempts (user_id,problem_id,language,initial_code,current_code,
            mistake_categories_json,current_stage,hints_used,is_solved,solved_independently,solution_unlocked,
            verification_status,is_completed) VALUES (?,?,?,?,?,?,1,0,0,0,0,'unverified',0)""",
            (user_id, challenge_id, problem["language"], problem["buggy_code"], problem["buggy_code"], categories_json))
        attempt_id = cursor.lastrowid
        conn.execute("""INSERT INTO bug_challenge_progress
            (user_id,attempt_id,challenge_id,difficulty,topic,bug_category,original_code,
             expected_prediction,bug_line,bug_line_end)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (user_id, attempt_id, challenge_id, problem["difficulty"], problem["concept"],
             problem["mistake_category_slug"], problem["buggy_code"], expected_prediction, start, end))
        if lang in ("en", "hinglish"):
            conn.execute("INSERT INTO hint_logs (attempt_id,stage_number,hint_type,prompt_shown,student_response,explanation_lang) VALUES (?,1,'mode','Mode: bug_challenge','',?)", (attempt_id, lang))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    stage = tutor_engine.get_stage_content(attempt_id, 1, lang=lang)
    safe_challenge = {key: problem[key] for key in ("id", "title", "language", "difficulty", "concept", "mistake_category_slug", "description", "buggy_code")}
    return {"attempt_id": attempt_id, "problem": safe_challenge, "stage": stage,
        "mode": "practice", "is_bug_challenge": True,
        "challenge": {"attempt_id": attempt_id, "title": problem["title"], "description": problem["description"],
            "language": problem["language"], "difficulty": problem["difficulty"], "topic": problem["concept"],
            "bug_category": problem["mistake_category_slug"], "original_code": problem["buggy_code"],
            "sample_input": tests[0].get("input", ""), "prediction_type": "compiler error" if not buggy_result.get("is_compiled") else ("runtime error" if (buggy_result.get("test_results") or [{}])[0].get("error") and not (buggy_result.get("test_results") or [{}])[0].get("actual_output") else "program output")}}, 201


def get_session(user_id, attempt_id):
    conn = database.get_connection()
    row = conn.execute("""SELECT b.*,p.title,p.description,p.language,a.current_code,a.hints_used,
        CAST(strftime('%s',COALESCE(b.completed_at,CURRENT_TIMESTAMP))-strftime('%s',b.created_at) AS INTEGER) AS time_taken_seconds
        FROM bug_challenge_progress b JOIN problems p ON p.id=b.challenge_id
        JOIN attempts a ON a.id=b.attempt_id
        WHERE b.attempt_id=? AND b.user_id=?""", (attempt_id, user_id)).fetchone()
    conn.close()
    if not row:
        return {"error": "Challenge attempt not found."}, 404
    return _public_session(row), 200


def list_history(user_id):
    conn = database.get_connection()
    rows = conn.execute("""SELECT b.*,p.title,p.description,p.language,a.current_code,a.hints_used,
        CAST(strftime('%s',COALESCE(b.completed_at,CURRENT_TIMESTAMP))-strftime('%s',b.created_at) AS INTEGER) AS time_taken_seconds
        FROM bug_challenge_progress b JOIN problems p ON p.id=b.challenge_id
        JOIN attempts a ON a.id=b.attempt_id WHERE b.user_id=? ORDER BY b.created_at DESC,b.id DESC""", (user_id,)).fetchall()
    conn.close()
    return [_public_session(row) for row in rows]


def submit_prediction(user_id, attempt_id, prediction):
    if not isinstance(prediction, str) or not prediction.strip() or len(prediction) > 2000:
        return {"error": "Enter a prediction up to 2,000 characters."}, 400
    conn = database.get_connection()
    row = conn.execute("SELECT expected_prediction,status FROM bug_challenge_progress WHERE attempt_id=? AND user_id=?", (attempt_id, user_id)).fetchone()
    if not row:
        conn.close(); return {"error": "Challenge attempt not found."}, 404
    if row["status"] == "completed":
        conn.close(); return {"error": "This challenge is already complete."}, 409
    norm = lambda value: code_executor.normalize_output(str(value or "")).casefold()
    correct = norm(prediction) == norm(row["expected_prediction"])
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE bug_challenge_progress SET prediction=?,prediction_correct=?,prediction_attempts=prediction_attempts+1,updated_at=CURRENT_TIMESTAMP WHERE attempt_id=? AND user_id=?",
        (prediction.strip(), int(correct), attempt_id, user_id))
    conn.execute("""INSERT INTO bug_challenge_events (user_id,attempt_id,event_type,prediction,is_correct)
        VALUES (?,?,'prediction',?,?)""", (user_id, attempt_id, prediction.strip(), int(correct)))
    conn.commit(); conn.close()
    return {"correct": correct, "feedback": "Prediction matches the program behavior. Continue to identify the bug." if correct else "That prediction does not match the program behavior. Check whether it compiles, then trace the sample input. The expected result is still hidden."}, 200


def submit_identification(user_id, attempt_id, line, explanation):
    if not isinstance(line, int) or line < 1:
        return {"error": "Enter a valid line number."}, 400
    if not isinstance(explanation, str) or len(explanation.strip()) < 10 or len(explanation) > 1500:
        return {"error": "Explain why the line may be wrong in 10 to 1,500 characters."}, 400
    conn = database.get_connection()
    row = conn.execute("SELECT bug_line,bug_line_end,status,original_code FROM bug_challenge_progress WHERE attempt_id=? AND user_id=?", (attempt_id, user_id)).fetchone()
    if not row:
        conn.close(); return {"error": "Challenge attempt not found."}, 404
    if row["status"] == "completed":
        conn.close(); return {"error": "This challenge is already complete."}, 409
    line_count = max(1, len((row["original_code"] or "").splitlines()))
    if line > line_count:
        conn.close(); return {"error": f"Choose a line from 1 to {line_count}."}, 400
    correct = row["bug_line"] <= line <= row["bug_line_end"]
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE bug_challenge_progress SET identified_line=?,bug_line_correct=?,bug_explanation=?,updated_at=CURRENT_TIMESTAMP WHERE attempt_id=? AND user_id=?",
        (line, int(correct), explanation.strip(), attempt_id, user_id))
    conn.execute("""INSERT INTO bug_challenge_events (user_id,attempt_id,event_type,identified_line,explanation,is_correct)
        VALUES (?,?,'identification',?,?,?)""", (user_id, attempt_id, line, explanation.strip(), int(correct)))
    conn.commit(); conn.close()
    return {"correct": correct, "feedback": "You found a changed line. Now connect it to the stated behavior." if correct else "That line is not the changed behavior yet. Compare the code with the problem statement and try again."}, 200


def before_verify(user_id, attempt_id):
    conn = database.get_connection()
    row = conn.execute("SELECT status,prediction_correct,bug_line_correct FROM bug_challenge_progress WHERE attempt_id=? AND user_id=?", (attempt_id, user_id)).fetchone()
    conn.close()
    if not row:
        return None
    if row["status"] == "completed":
        return {"error": "This challenge is already complete."}
    if not row["prediction_correct"]:
        return {"error": "Submit a correct behavior prediction before verifying your fix."}
    if not row["bug_line_correct"]:
        return {"error": "Identify the faulty line and explain your reasoning before verifying your fix."}
    return None


def record_verification(user_id, attempt_id, code, verify_result):
    conn = database.get_connection()
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM bug_challenge_progress WHERE attempt_id=? AND user_id=?", (attempt_id, user_id)).fetchone()
    if not row:
        conn.rollback(); conn.close(); return None
    attempt = conn.execute("SELECT hints_used FROM attempts WHERE id=? AND user_id=?", (attempt_id, user_id)).fetchone()
    if row["status"] == "completed":
        conn.rollback(); conn.close(); return None
    code_attempts = row["code_attempts"] + 1
    passed = bool(verify_result.get("is_solved"))
    hints = int(attempt["hints_used"] if attempt else 0)
    prediction_points = 15 if row["prediction_correct"] else 0
    identification_points = 15 if row["bug_line_correct"] else 0
    verification_points = 70 if passed else 0
    hint_penalty = min(32, hints * 8)
    retry_penalty = min(20, max(0, code_attempts - 1) * 5)
    score = max(0, min(100, prediction_points + identification_points + verification_points - hint_penalty - retry_penalty))
    breakdown = {"prediction": prediction_points, "bug_identification": identification_points,
        "verified_tests": verification_points, "hint_penalty": hint_penalty, "extra_attempt_penalty": retry_penalty,
        "formula": "15 prediction + 15 bug identification + 70 all-tests-pass - 8 per hint (max 32) - 5 per retry (max 20)"}
    status = "completed" if passed else "in_progress"
    conn.execute("""UPDATE bug_challenge_progress SET final_code=?,code_attempts=?,hints_used=?,
        verification_passed=?,score=?,score_breakdown_json=?,status=?,completed_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
        updated_at=CURRENT_TIMESTAMP WHERE attempt_id=? AND user_id=?""",
        (code, code_attempts, hints, int(passed), score, json.dumps(breakdown), status, int(passed), attempt_id, user_id))
    conn.commit()
    saved = conn.execute("""SELECT b.*,p.title,p.description,p.language,a.current_code,a.hints_used,
        CAST(strftime('%s',COALESCE(b.completed_at,CURRENT_TIMESTAMP))-strftime('%s',b.created_at) AS INTEGER) AS time_taken_seconds
        FROM bug_challenge_progress b JOIN problems p ON p.id=b.challenge_id JOIN attempts a ON a.id=b.attempt_id
        WHERE b.attempt_id=? AND b.user_id=?""", (attempt_id, user_id)).fetchone()
    conn.close()
    result = _public_session(saved)
    result["passed_tests"] = (verify_result.get("execution_result") or {}).get("passed_tests", 0)
    result["total_tests"] = (verify_result.get("execution_result") or {}).get("total_tests", 0)
    if passed:
        result["learning_takeaway"] = f"{result['topic']} code should be checked against the stated behavior and every saved test case."
        result["bug_summary"] = f"The challenge contains a {result['bug_category'].replace('_', ' ')} bug. Your fixed code passed all saved tests."
    return result
