"""
CodeMentor - Debugging DNA & Mistake Analysis Engine
Classifies programming errors into a structured taxonomy and computes real-time,
persistent learning profiles strictly from actual stored database records.
"""

import json
import re
from datetime import datetime
from collections import defaultdict
from typing import Dict, Any, List, Optional
import database

def classify_mistakes(code: str, language: str, error_message: str = "",
                      compiler_error: str = "", test_results: List[Dict[str, Any]] = None,
                      expected_category: str = None) -> Dict[str, Any]:
    """
    Analyzes code, runtime errors, compiler diagnostics, and test results
    to classify mistakes into standard categories with confidence levels.
    """
    language = language.lower()
    combined_err = (compiler_error or "") + "\n" + (error_message or "")
    detected = []

    # 1. Syntax Errors
    syntax_indicators = [
        "syntax error", "syntaxerror", "expected ';'", "';' expected", "not a statement",
        "illegal start of expression", "expected expression", "expected ')'", "expected ':'",
        "parse error", "indentationerror", "unexpected EOF", "invalid syntax"
    ]
    if any(ind in combined_err.lower() for ind in syntax_indicators):
        detected.append({
            "category": "syntax_errors",
            "confidence": 0.95,
            "evidence": "Compilation or syntax parser reported invalid language tokens."
        })

    # 2. Array Indexing Errors
    array_indicators = [
        "indexerror", "out of range", "index out of bounds", "segmentation fault",
        "arrayindexoutofboundsexception", "buffer overflow", "array index is out of bounds", "stack smashing"
    ]
    pointer_only_crash = bool(
        language in ("c", "cpp", "c++") and "segmentation fault" in combined_err.lower()
        and re.search(r"(?:\*\s*[A-Za-z_]\w*|\b[A-Za-z_]\w*\s*=\s*&[A-Za-z_]\w*)", code)
        and "[" not in code
    )
    has_array_err = any(ind in combined_err.lower() for ind in array_indicators) and not pointer_only_crash
    # Heuristics in code: arr[size] or arr[i <= size] or negative index
    code_has_arr_bound = bool(re.search(r'\[\s*(i|j|k|\w+)\s*\]', code) and re.search(r'<=\s*(size|len|n|\d+)', code))
    if has_array_err or (code_has_arr_bound and language in ("c", "cpp", "java")):
        detected.append({
            "category": "array_indexing",
            "confidence": 0.90 if has_array_err else 0.75,
            "evidence": "Array accessed beyond valid index boundaries (0 to N-1)."
        })

    # 3. Off-by-one Errors
    # Look for loop patterns with <= len or range(1, n)
    has_range_off = bool(re.search(r'range\s*\(\s*\d+\s*,\s*\w+\s*\)', code) and ("sum" in code or "total" in code))
    # Also catch a loop that stops before the last required element, e.g.
    # i < size - 1. This pattern is only evidence of a boundary risk; the
    # expected task behavior and actual failing tests still provide context.
    has_c_off = bool(re.search(
        r'for\s*\([^;]+;\s*\w+\s*(?:<=\s*(?:size|len|length|n)|<\s*(?:size|len|length|n)\s*-\s*1)\s*;',
        code, re.I))
    if has_range_off or has_c_off:
        detected.append({
            "category": "off_by_one",
            "confidence": 0.85,
            "evidence": "Loop boundary condition includes or excludes the endpoint incorrectly."
        })

    # 4. Recursion Misunderstandings
    rec_indicators = ["recursionerror", "maximum recursion depth", "stack overflow"]
    # Check if function calls itself
    func_match = re.search(r'def\s+(\w+)\s*\(|int\s+(\w+)\s*\(|void\s+(\w+)\s*\(', code)
    func_name = next((g for g in func_match.groups() if g), "") if func_match else ""
    is_recursive = bool(func_name and re.search(rf'\b{func_name}\s*\(', code[func_match.end():]))
    has_rec_err = any(ind in combined_err.lower() for ind in rec_indicators) or (
        is_recursive and "segmentation fault" in combined_err.lower()
    )
    # Check if base case returns 0 in product recursion
    has_bad_base = bool(is_recursive and re.search(r'return\s+0\s*;?', code) and ("*" in code or "factorial" in code))

    if has_rec_err or has_bad_base:
        detected.append({
            "category": "recursion_misunderstanding",
            "confidence": 0.90 if has_rec_err else 0.80,
            "evidence": "Flawed recursive base case or infinite recursive branch."
        })

    # 5. Loop Logic Errors
    loop_timeout = "timed out" in combined_err.lower() or "suspected infinite loop" in combined_err.lower()
    # Check loop incrementing instead of decrementing
    wrong_step = bool(re.search(r'while\s+\w+\s*>\s*0\s*:', code) and re.search(r'\+=\s*1|\+\+', code))
    if loop_timeout or wrong_step:
        detected.append({
            "category": "loop_logic",
            "confidence": 0.95 if loop_timeout or wrong_step else 0.70,
            "evidence": "Loop condition does not converge to termination, causing an infinite loop."
        })

    # 6. Variable Scope Errors
    scope_indicators = [
        "nameerror", "is not defined", "cannot find symbol", "undeclared identifier", "uninitialized", "referenced before assignment",
        "used without being initialized"
    ]
    if any(ind in combined_err.lower() for ind in scope_indicators):
        detected.append({
            "category": "variable_scope",
            "confidence": 0.90,
            "evidence": "Variable referenced outside of its declared or active lexical scope."
        })

    # 7. Incorrect Conditions
    # e.g., if (x = 5) in C
    has_assignment_in_if = bool(re.search(r'if\s*\([^=!<>]=[^=]', code))
    context_lower = combined_err.lower()
    parity_conflict = bool(
        ("even" in context_lower and re.search(r'%\s*2\s*==\s*1|%\s*2\s*!=\s*0', code)) or
        ("odd" in context_lower and re.search(r'%\s*2\s*==\s*0|%\s*2\s*!=\s*1', code))
    )
    if has_assignment_in_if or parity_conflict:
        detected.append({
            "category": "incorrect_conditions",
            "confidence": 0.90 if parity_conflict else 0.85,
            "evidence": ("The modulo condition conflicts with the stated even/odd requirement." if parity_conflict
                         else "Assignment operator '=' used inside condition where comparison '==' was intended.")
        })

    # 8. Algorithm Logic Errors
    # e.g., max_val = 0 for negative list
    has_zero_max_init = bool(re.search(r'max_val\s*=\s*0|maximum\s*=\s*0', code) and ("-10" in code or "negative" in code or "nums" in code))
    if has_zero_max_init or (test_results and any("output mismatch" in (tr.get("error") or "").lower() for tr in test_results)):
        detected.append({
            "category": "algorithm_logic",
            "confidence": 0.80 if has_zero_max_init else 0.65,
            "evidence": "Algorithm accumulator initialization or logic step does not cover edge cases."
        })

    # If the problem had an expected category and no strong match found yet, fall back with medium confidence
    if not detected and expected_category:
        detected.append({
            "category": expected_category,
            "confidence": 0.45,
            "evidence": "The problem tag suggests this concept, but the submitted code and runtime evidence do not confirm a specific bug."
        })
    elif not detected:
        detected.append({
            "category": "algorithm_logic",
            "confidence": 0.25,
            "evidence": "No strong bug pattern was found from the submitted code and available error details. Runtime evidence is needed to narrow this down."
        })

    # Deduplicate and sort by confidence
    unique_map = {}
    for d in detected:
        cat = d["category"]
        if cat not in unique_map or d["confidence"] > unique_map[cat]["confidence"]:
            unique_map[cat] = d

    final_list = sorted(unique_map.values(), key=lambda x: x["confidence"], reverse=True)
    line_patterns = {
        "syntax_errors": r"", "array_indexing": r"\[[^\]]+\]", "off_by_one": r"for\s*\(.*;|for\s+.*in\s+range",
        "recursion_misunderstanding": r"return|\w+\s*\(", "loop_logic": r"while\s|for\s*\(",
        "variable_scope": r"", "incorrect_conditions": r"\bif\b", "algorithm_logic": r"max(?:imum)?\s*_?val\s*=|return"
    }
    source_lines = code.splitlines()
    for item in final_list:
        pat = line_patterns.get(item["category"])
        found = [idx for idx, source_line in enumerate(source_lines, 1) if pat and re.search(pat, source_line, re.I)]
        if item["category"] == "syntax_errors" and not found:
            compiler_line = re.search(r"(?:\.java|\.c|\.cpp|\.py)?:(\d+)(?::\d+)?:\s*(?:error|warning)|\bline\s+(\d+)\b", combined_err, re.I)
            line_number = next((int(value) for value in compiler_line.groups() if value), None) if compiler_line else None
            if line_number and 1 <= line_number <= len(source_lines):
                found = [line_number]
        if found:
            item["suspected_lines"] = found[:3]
    primary = final_list[0]["category"]

    return {
        "primary_category": primary,
        "categories": [d["category"] for d in final_list],
        "diagnostics": final_list,
        "overall_confidence": final_list[0]["confidence"],
        "is_ai_assessment": False, # Verified rule & diagnostic engine
        "analysis_type": "heuristic_and_runtime_analysis"
    }

def get_student_dna(user_id: int) -> Dict[str, Any]:
    """
    Computes a persistent, real-time learning profile and analytics
    strictly based on the user's actual stored database records.
    Never generates fabricated statistics.
    """
    conn = database.get_connection()
    cursor = conn.cursor()

    # Verify user exists
    cursor.execute("SELECT id, username, display_name, created_at FROM users WHERE id = ?", (user_id,))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return {"error": "User not found", "empty_state": True}

    # Fetch mistake categories metadata
    cursor.execute("SELECT slug, name, description, color, icon FROM mistake_categories")
    cat_rows = cursor.fetchall()
    cat_meta = {row["slug"]: dict(row) for row in cat_rows}

    # Fetch all attempts for this user
    cursor.execute("""
    SELECT a.id, a.problem_id, a.language, a.mistake_categories_json, a.hints_used,
           a.is_solved, a.solved_independently, a.verification_status, a.created_at, a.is_completed,
           p.title as problem_title, p.concept as problem_concept, p.difficulty
    FROM attempts a
    LEFT JOIN problems p ON a.problem_id = p.id
    WHERE a.user_id = ?
    ORDER BY a.created_at ASC
    """, (user_id,))
    attempts = [dict(r) for r in cursor.fetchall()]
    cursor.execute("SELECT attempt_id, status, score, code_attempts FROM bug_challenge_progress WHERE user_id=?", (user_id,))
    bug_challenges = {row["attempt_id"]: dict(row) for row in cursor.fetchall()}
    cursor.execute("SELECT attempt_id, COUNT(*) AS run_count FROM hint_logs WHERE hint_type='verification' GROUP BY attempt_id")
    verification_counts = {row["attempt_id"]: row["run_count"] for row in cursor.fetchall()}
    cursor.execute("""SELECT h.attempt_id,h.context_json FROM hint_logs h JOIN attempts a ON a.id=h.attempt_id
        WHERE a.user_id=? AND h.hint_type='explain_fix'""", (user_id,))
    explanation_rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    explanation_completed_ids = set()
    for saved in explanation_rows:
        try:
            evaluation = json.loads(saved["context_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            evaluation = {}
        if evaluation.get("evaluation_status") == "correct":
            explanation_completed_ids.add(saved["attempt_id"])

    total_attempts = len(attempts)

    # Empty State for brand new users
    if total_attempts == 0:
        return {
            "empty_state": True,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "display_name": user["display_name"],
                "joined": user["created_at"]
            },
            "summary": {
                "total_attempts": 0,
                "solved_count": 0,
                "unsolved_count": 0,
                "solve_rate": 0.0,
                "total_hints_used": 0,
                "avg_hints_per_solved": 0.0,
                "independent_solves": 0,
                "independent_solve_rate": 0.0,
                "debugging_iq": 0,
                "completed_challenges": 0,
                "average_attempts_per_successful_solve": 0.0,
                "successful_solves_with_run_history": 0,
                "data_status": "No recorded attempts yet."
            },
            "mistake_distribution": [],
            "timeline": [],
            "topic_performance": [],
            "difficulty_performance": [],
            "strengths": [],
            "sufficient_data": False,
            "concepts_needing_practice": [],
            "concepts_improving": [],
            "personalized_insights": [
                {
                    "type": "welcome",
                    "text": f"Welcome to CodeMentor, {user['display_name']}! You have not recorded any debugging attempts yet. Submit your first challenge to generate your personal Debugging DNA profile."
                }
            ],
            "recommended_practice": {
                "slug": "c-array-boundary-overflow",
                "title": "Print Elements of an Array (Starter)",
                "concept": "Array Boundaries",
                "reason": "Great introductory problem to begin your learning profile."
            }
        }

    # Compute actual real metrics
    solved_count = sum(1 for a in attempts if a["is_solved"])
    unsolved_count = total_attempts - solved_count
    solve_rate = round((solved_count / total_attempts) * 100.0, 1) if total_attempts > 0 else 0.0
    total_hints = sum(a["hints_used"] for a in attempts)
    solved_hints = sum(a["hints_used"] for a in attempts if a["is_solved"])
    avg_hints = round(solved_hints / solved_count, 1) if solved_count > 0 else 0.0
    independent_solves = sum(1 for a in attempts if a["solved_independently"])
    independent_solve_rate = round((independent_solves / solved_count) * 100.0, 1) if solved_count else 0.0
    completed_count = sum(1 for a in attempts if a["id"] in explanation_completed_ids or
        bug_challenges.get(a["id"], {}).get("status") == "completed")
    successful_run_counts = []
    for attempt in attempts:
        if not attempt["is_solved"]:
            continue
        feature5 = bug_challenges.get(attempt["id"])
        run_count = int(feature5["code_attempts"] or 0) if feature5 else int(verification_counts.get(attempt["id"], 0))
        if run_count > 0:
            successful_run_counts.append(run_count)
    average_attempts_per_successful_solve = round(sum(successful_run_counts) / len(successful_run_counts), 1) if successful_run_counts else 0.0
    # Equal-weight mean of three direct ratios from the student's saved attempts.
    hint_efficiency = round(100.0 * solved_count / (solved_count + solved_hints), 1) if solved_count else 0.0
    debugging_iq = round((solve_rate + independent_solve_rate + hint_efficiency) / 3.0)

    # Category Frequency Breakdown
    category_counts = defaultdict(int)
    category_solved = defaultdict(int)
    category_attempts_history = defaultdict(list)
    topic_stats = defaultdict(lambda: {"attempts": 0, "solved": 0, "completed": 0, "hints": 0})
    difficulty_stats = defaultdict(lambda: {"attempts": 0, "solved": 0, "completed": 0, "hints": 0})

    for att in attempts:
        feature5 = bug_challenges.get(att["id"])
        is_completed = bool(att["id"] in explanation_completed_ids or (feature5 and feature5["status"] == "completed"))
        topic = att["problem_concept"] or "Custom code"
        difficulty = att["difficulty"] or "Custom"
        for stats in (topic_stats[topic], difficulty_stats[difficulty]):
            stats["attempts"] += 1
            stats["solved"] += int(bool(att["is_solved"]))
            stats["completed"] += int(is_completed)
            stats["hints"] += int(att["hints_used"] or 0)
        try:
            cats = json.loads(att["mistake_categories_json"])
        except Exception:
            cats = []
        for cat in cats:
            category_counts[cat] += 1
            if att["is_solved"]:
                category_solved[cat] += 1
            category_attempts_history[cat].append(att)

    def performance_rows(groups):
        rows = []
        for name, values in groups.items():
            rows.append({"name": name, **values,
                "accuracy": round(100.0 * values["solved"] / values["attempts"], 1) if values["attempts"] else 0.0,
                "completion_rate": round(100.0 * values["completed"] / values["attempts"], 1) if values["attempts"] else 0.0,
                "avg_hints": round(values["hints"] / values["attempts"], 1) if values["attempts"] else 0.0,
                "evidence_status": "insufficient_data" if values["attempts"] < 2 else "recorded"})
        return sorted(rows, key=lambda item: (-item["attempts"], item["name"].casefold()))
    topic_performance = performance_rows(topic_stats)
    difficulty_performance = performance_rows(difficulty_stats)
    strengths = [{"name": item["name"], "accuracy": item["accuracy"], "attempts": item["attempts"]}
        for item in topic_performance if item["attempts"] >= 2 and item["accuracy"] >= 80.0]

    total_tag_occurrences = sum(category_counts.values()) or 1
    mistake_distribution = []
    for slug, count in sorted(category_counts.items(), key=lambda x: x[1], reverse=True):
        meta = cat_meta.get(slug, {"name": slug.replace("_", " ").title(), "color": "#6366f1", "icon": "alert-circle"})
        pct = round((count / total_tag_occurrences) * 100.0, 1)
        mistake_distribution.append({
            "slug": slug,
            "name": meta["name"],
            "count": count,
            "percentage": pct,
            "color": meta.get("color", "#6366f1"),
            "solved": category_solved[slug],
            "unsolved": count - category_solved[slug]
        })

    # Timeline Breakdown (grouped by date)
    date_groups = defaultdict(lambda: {"attempts": 0, "solved": 0, "hints": 0})
    for att in attempts:
        date_str = att["created_at"].split(" ")[0] if " " in att["created_at"] else att["created_at"][:10]
        date_groups[date_str]["attempts"] += 1
        if att["is_solved"]:
            date_groups[date_str]["solved"] += 1
        date_groups[date_str]["hints"] += att["hints_used"]

    timeline = [
        {"date": date_k, "attempts": v["attempts"], "solved": v["solved"], "hints": v["hints"],
         "accuracy": round((v["solved"] / v["attempts"]) * 100.0, 1) if v["attempts"] else 0.0}
        for date_k, v in sorted(date_groups.items())
    ]

    # Identify Concepts Needing Practice vs Showing Improvement
    concepts_needing_practice = []
    concepts_improving = []

    for slug, count in category_counts.items():
        meta = cat_meta.get(slug, {"name": slug.replace("_", " ").title()})
        cat_name = meta["name"]
        hist = category_attempts_history[slug]
        solved_in_cat = category_solved[slug]
        unsolved_in_cat = count - solved_in_cat

        if unsolved_in_cat > 0:
            concepts_needing_practice.append({
                "slug": slug,
                "name": cat_name,
                "unsolved_count": unsolved_in_cat,
                "total_count": count,
                "reason": f"{unsolved_in_cat} of {count} recorded attempts remain unsolved."
            })

        # Check for improvement: last attempt was solved, or fewer hints needed
        if len(hist) >= 2:
            earlier = hist[0]
            latest = hist[-1]
            if (not earlier["is_solved"] and latest["is_solved"]) or (latest["is_solved"] and latest["hints_used"] < earlier["hints_used"]):
                concepts_improving.append({
                    "slug": slug,
                    "name": cat_name,
                    "reason": f"Latest attempt was solved with {latest['hints_used']} hint(s), demonstrating measurable conceptual mastery."
                })
        elif len(hist) == 1 and hist[0]["is_solved"]:
            concepts_improving.append({
                "slug": slug,
                "name": cat_name,
                "reason": f"Successfully resolved in attempt using {hist[0]['hints_used']} hint(s)."
            })

    # Generate Personalized, Real Data-Driven Insights
    insights = []
    if mistake_distribution:
        top_mistake = mistake_distribution[0]
        insights.append({
            "type": "top_mistake",
            "title": "Primary Learning Area",
            "text": f"'{top_mistake['name']}' is your most frequent recorded mistake ({top_mistake['count']} attempts recorded, {top_mistake['percentage']}% of all detected mistake tags)."
        })

    if solve_rate >= 70.0:
        insights.append({
            "type": "high_solve_rate",
            "title": "High Solve Consistency",
            "text": f"Strong debugging momentum: You have successfully solved {solved_count} of your {total_attempts} attempts ({solve_rate}% solve rate)."
        })
    elif solve_rate > 0.0:
        insights.append({
            "type": "moderate_solve_rate",
            "title": "Solve Progress",
            "text": f"You have resolved {solved_count} of {total_attempts} debugging challenges ({solve_rate}% solve rate). Keep applying structured hints before checking solutions."
        })

    if concepts_improving:
        top_imp = concepts_improving[0]
        insights.append({
            "type": "improvement",
            "title": "Concept Showing Improvement",
            "text": f"You have shown measurable progress in {top_imp['name']}. {top_imp['reason']}"
        })

    if concepts_needing_practice:
        top_need = concepts_needing_practice[0]
        insights.append({
            "type": "practice_needed",
            "title": "Targeted Practice Recommended",
            "text": f"'{top_need['name']}' needs more practice based on your recent results ({top_need['unsolved_count']} unsolved attempt(s))."
        })

    # Recommended practice problem
    recommended = get_recommended_problem(user_id, concepts_needing_practice, mistake_distribution)

    return {
        "empty_state": False,
        "user": {
            "id": user["id"],
            "username": user["username"],
            "display_name": user["display_name"],
            "joined": user["created_at"]
        },
        "summary": {
            "total_attempts": total_attempts,
            "solved_count": solved_count,
            "unsolved_count": unsolved_count,
            "solve_rate": solve_rate,
            "total_hints_used": total_hints,
            "avg_hints_per_solved": avg_hints,
            "independent_solves": independent_solves,
            "independent_solve_rate": independent_solve_rate,
            "debugging_iq": debugging_iq,
            "completed_challenges": completed_count,
            "average_attempts_per_successful_solve": average_attempts_per_successful_solve,
            "successful_solves_with_run_history": len(successful_run_counts),
            "data_status": ("Limited evidence: fewer than 2 recorded attempts." if total_attempts < 2
                else f"Calculated from {total_attempts} saved attempt(s)."),
            "debugging_iq_label": f"Practice score / 100 = average of solve rate ({solve_rate}%), independent solve rate ({independent_solve_rate}%), and hint efficiency ({hint_efficiency}%); based on {total_attempts} saved attempt(s). Not a standardized IQ test."
        },
        "mistake_distribution": mistake_distribution,
        "timeline": timeline,
        "topic_performance": topic_performance,
        "difficulty_performance": difficulty_performance,
        "strengths": strengths,
        "sufficient_data": total_attempts >= 2,
        "concepts_needing_practice": concepts_needing_practice,
        "concepts_improving": concepts_improving,
        "personalized_insights": insights,
        "recommended_practice": recommended
    }

def get_advanced_insights(user_id: int) -> Dict[str, Any]:
    """Build replay, heatmap, study plan and skill tree from actual saved attempts."""
    dna = get_student_dna(user_id)
    if dna.get("error"):
        return dna
    conn = database.get_connection()
    cur = conn.cursor()
    cur.execute("""SELECT a.id, a.problem_id, a.custom_title, a.language, a.initial_code,
        a.current_code, a.mistake_categories_json, a.hints_used, a.is_solved,
        a.solved_independently, a.verification_status, a.last_test_results_json, a.created_at, a.updated_at,
        a.current_stage, a.is_completed,
        p.title AS problem_title, p.concept, p.difficulty
        FROM attempts a LEFT JOIN problems p ON p.id=a.problem_id
        WHERE a.user_id=? ORDER BY a.created_at DESC, a.id DESC""", (user_id,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT slug, name, color FROM mistake_categories ORDER BY id")
    categories = [dict(r) for r in cur.fetchall()]
    cur.execute("""SELECT h.id, h.attempt_id, h.stage_number, h.hint_type, h.prompt_shown,
        h.student_response, h.context_json, h.created_at, h.hint_provider, h.hint_model
        FROM hint_logs h JOIN attempts a ON a.id=h.attempt_id WHERE a.user_id=?
        AND h.hint_type IN ('verification','hint_request','debug_conversation','explain_fix',
        'diagnosis_context','prediction','investigation') ORDER BY h.created_at,h.id""", (user_id,))
    event_rows = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM bug_challenge_progress WHERE user_id=? ORDER BY created_at,id", (user_id,))
    challenge_rows = {r["attempt_id"]: dict(r) for r in cur.fetchall()}
    cur.execute("""SELECT e.* FROM bug_challenge_events e JOIN attempts a ON a.id=e.attempt_id
        WHERE e.user_id=? AND a.user_id=? ORDER BY e.created_at,e.id""", (user_id, user_id))
    challenge_event_rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    events_by_attempt = defaultdict(list)
    explanation_completed_ids = set()
    for event in event_rows:
        event_item = {"type": event["hint_type"], "stage": event["stage_number"], "created_at": event["created_at"],
            "sequence_id": event["id"], "timestamp_available": True}
        if event["hint_type"] == "verification":
            try:
                saved = json.loads(event["student_response"] or "{}")
            except (TypeError, json.JSONDecodeError):
                saved = {}
            result = saved.get("execution_result") or {}
            event_item.update({"summary": f"{'Passed' if saved.get('is_solved') else 'Failed'} verification · {result.get('passed_tests', 0)}/{result.get('total_tests', 0)} tests passed", "code": saved.get("code", ""), "test_results": result})
        elif event["hint_type"] == "hint_request":
            event_item.update({"summary": "Hint received", "hint": event["prompt_shown"], "student_note": event["student_response"] or ""})
        elif event["hint_type"] == "debug_conversation":
            event_item.update({"summary": "Tutor response", "tutor": event["prompt_shown"],
                "student_note": event["student_response"] or "", "provider": event["hint_provider"] or "local",
                "model": event["hint_model"] or ""})
        elif event["hint_type"] == "explain_fix":
            try:
                evaluation = json.loads(event["context_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                evaluation = {}
            if evaluation.get("evaluation_status") == "correct":
                explanation_completed_ids.add(event["attempt_id"])
            event_item.update({"summary": event["prompt_shown"], "student_explanation": event["student_response"] or "",
                "evaluation_status": evaluation.get("evaluation_status"), "status_label": evaluation.get("status_label"),
                "score": evaluation.get("score"), "feedback": evaluation.get("feedback"),
                "feedback_source": evaluation.get("feedback_source")})
        elif event["hint_type"] in ("prediction", "investigation"):
            event_item.update({"summary": event["prompt_shown"], "student_note": event["student_response"] or ""})
        else:
            event_item.update({"summary": "Observed error/output provided", "details": event["prompt_shown"]})
        events_by_attempt[event["attempt_id"]].append(event_item)

    challenge_event_types = defaultdict(set)
    for event in challenge_event_rows:
        challenge_event_types[event["attempt_id"]].add(event["event_type"])
        if event["event_type"] == "prediction":
            item = {"type": "challenge_prediction", "summary": "Challenge behavior prediction",
                "student_prediction": event["prediction"], "correct": bool(event["is_correct"]),
                "created_at": event["created_at"], "sequence_id": 100000000 + event["id"],
                "timestamp_available": True}
        else:
            item = {"type": "challenge_identification", "summary": "Faulty-line identification",
                "identified_line": event["identified_line"], "student_explanation": event["explanation"],
                "correct": bool(event["is_correct"]), "created_at": event["created_at"],
                "sequence_id": 100000000 + event["id"], "timestamp_available": True}
        events_by_attempt[event["attempt_id"]].append(item)

    for attempt_id, challenge in challenge_rows.items():
        recorded_types = challenge_event_types[attempt_id]
        if challenge.get("prediction") and "prediction" not in recorded_types:
            events_by_attempt[attempt_id].append({"type": "challenge_prediction", "summary": "Previously saved challenge prediction",
                "student_prediction": challenge["prediction"], "correct": bool(challenge["prediction_correct"]),
                "created_at": None, "timestamp_available": False, "history_note": "Only the latest saved prediction is available; its original submission time and earlier retries were not recorded."})
        if challenge.get("identified_line") and "identification" not in recorded_types:
            events_by_attempt[attempt_id].append({"type": "challenge_identification", "summary": "Previously saved faulty-line identification",
                "identified_line": challenge["identified_line"], "student_explanation": challenge["bug_explanation"],
                "correct": bool(challenge["bug_line_correct"]), "created_at": None, "timestamp_available": False,
                "history_note": "This saved identification has no individual timestamp; older retries were not recorded."})

    counts = defaultdict(lambda: {"attempts": 0, "solved": 0, "hints": 0})
    replay = []
    for row in rows:
        try:
            slugs = json.loads(row["mistake_categories_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            slugs = []
        for slug in slugs:
            counts[slug]["attempts"] += 1
            counts[slug]["solved"] += int(bool(row["is_solved"]))
            counts[slug]["hints"] += int(row["hints_used"] or 0)
        try:
            results = json.loads(row["last_test_results_json"]) if row["last_test_results_json"] else None
        except (TypeError, json.JSONDecodeError):
            results = None
        f5 = challenge_rows.get(row["id"])
        challenge = None
        if f5:
            try:
                challenge_score_breakdown = json.loads(f5["score_breakdown_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                challenge_score_breakdown = {}
            challenge = {"difficulty": f5["difficulty"], "topic": f5["topic"], "bug_category": f5["bug_category"],
                "status": f5["status"], "score": f5["score"], "score_breakdown": challenge_score_breakdown,
                "prediction": f5["prediction"], "prediction_correct": bool(f5["prediction_correct"]),
                "prediction_attempts": f5["prediction_attempts"], "identified_line": f5["identified_line"],
                "bug_identification_correct": None if f5["bug_line_correct"] is None else bool(f5["bug_line_correct"]),
                "bug_line": f5["bug_line"] if f5["status"] == "completed" else None,
                "bug_explanation": f5["bug_explanation"], "code_attempts": f5["code_attempts"],
                "verification_passed": bool(f5["verification_passed"]), "final_code": f5["final_code"],
                "completed_at": f5["completed_at"],
                "history_note": "Earlier prediction or identification retries may not be available for records saved before event history was added."
                    if challenge_event_types[row["id"]] == set() else None}
        attempt_events = events_by_attempt[row["id"]]
        attempt_events.sort(key=lambda item: (item.get("created_at") is None, item.get("created_at") or "", item.get("sequence_id", 0)))
        evidence_completed = bool(row["id"] in explanation_completed_ids or (f5 and f5["status"] == "completed"))
        replay.append({"attempt_id": row["id"], "title": row["problem_title"] or row["custom_title"] or "Custom code",
            "language": row["language"], "concept": row["concept"] or "Custom concept", "difficulty": row["difficulty"] or "Custom",
            "created_at": row["created_at"], "solved": bool(row["is_solved"]), "hints_used": row["hints_used"],
            "independent": bool(row["solved_independently"]), "status": row["verification_status"],
            "current_stage": row["current_stage"], "is_completed": evidence_completed,
            "legacy_completion_unverified": bool(row["is_completed"] and not evidence_completed),
            "initial_code": row["initial_code"], "current_code": row["current_code"], "test_results": results,
            "updated_at": row["updated_at"], "is_feature5_challenge": bool(f5), "challenge": challenge,
            "replay_history_note": "Saved verification submissions are shown; editor drafts between verifications were not recorded."
                if row["initial_code"] != row["current_code"] else None,
            "events": attempt_events})

    meta = {c["slug"]: c for c in categories}
    heatmap = []
    skill_tree = []
    for slug, values in counts.items():
        cat = meta.get(slug, {"name": slug.replace("_", " ").title(), "color": "#6366f1"})
        accuracy = round(100.0 * values["solved"] / values["attempts"], 1) if values["attempts"] else 0.0
        heatmap.append({"slug": slug, "name": cat["name"], "attempts": values["attempts"],
            "solved": values["solved"], "accuracy": accuracy,
            "avg_hints": round(values["hints"] / values["attempts"], 1), "color": cat["color"]})
        skill_tree.append({"slug": slug, "name": cat["name"], "mastery": accuracy,
            "evidence_count": values["attempts"], "status": "mastered" if accuracy >= 80 and values["attempts"] >= 2 else "practicing" if values["attempts"] else "locked"})
    for slug, cat in meta.items():
        if slug not in counts:
            skill_tree.append({"slug": slug, "name": cat["name"], "mastery": 0,
                "evidence_count": 0, "status": "locked"})
    heatmap.sort(key=lambda x: (x["accuracy"], -x["attempts"]))
    skill_tree.sort(key=lambda x: (-x["mastery"], x["name"]))

    plan = []
    weak = heatmap[:3]
    for item in weak:
        plan.append({"concept": item["name"], "reason": f"{item['solved']} of {item['attempts']} recorded attempts solved ({item['accuracy']}% accuracy).",
            "action": "Choose a matching practice challenge and try before requesting a hint."})
    if not rows:
        plan.append({"concept": "Start here", "reason": "No attempts are recorded yet, so there is no personal trend to analyze.",
            "action": "Complete one starter challenge to begin building your study plan."})
    recent_activity = []
    for attempt in replay:
        timed_events = [event for event in attempt["events"] if event.get("created_at")]
        if timed_events:
            recent_activity.extend({"attempt_id": attempt["attempt_id"], "title": attempt["title"],
                "summary": event.get("summary") or event.get("type", "Saved event"), "created_at": event["created_at"]}
                for event in timed_events)
        else:
            recent_activity.append({"attempt_id": attempt["attempt_id"], "title": attempt["title"],
                "summary": "Attempt started", "created_at": attempt["created_at"]})
    recent_activity.sort(key=lambda item: (item["created_at"], item["attempt_id"]), reverse=True)
    summary = dna.get("summary") or {}
    return {"user_id": user_id, "debugging_iq": summary.get("debugging_iq", 0),
        "debugging_iq_label": summary.get("debugging_iq_label", "No saved attempts yet; a practice score will appear after a challenge."), "confusion_heatmap": heatmap,
        "skill_tree": skill_tree, "study_plan": plan, "error_replay": replay[:30],
        "repeated_mistakes": [x for x in heatmap if x["attempts"] >= 2],
        "accuracy_trend": dna.get("timeline", []), "topic_performance": dna.get("topic_performance", []),
        "difficulty_performance": dna.get("difficulty_performance", []), "strengths": dna.get("strengths", []),
        "sufficient_data": dna.get("sufficient_data", False), "summary": dna.get("summary", {}),
        "recent_activity": recent_activity[:10], "recommended_practice": dna.get("recommended_practice")}

def get_recommended_problem(user_id: int, needing_practice: List[Dict], distribution: List[Dict]) -> Dict[str, Any]:
    """
    Finds an appropriate practice problem from the database targeting the student's weakest concept.
    """
    conn = database.get_connection()
    cursor = conn.cursor()

    # Determine target category slug
    target_slug = None
    if needing_practice:
        target_slug = needing_practice[0]["slug"]
    elif distribution:
        target_slug = distribution[0]["slug"]

    # Try to find an unsolved problem in that category
    if target_slug:
        cursor.execute("""
        SELECT p.id, p.slug, p.title, p.language, p.difficulty, p.concept, p.mistake_category_slug
        FROM problems p
        WHERE p.mistake_category_slug = ?
          AND p.id NOT IN (SELECT problem_id FROM attempts WHERE user_id = ? AND is_solved = 1)
        LIMIT 1
        """, (target_slug, user_id))
        rec = cursor.fetchone()
        if rec:
            conn.close()
            return {
                "id": rec["id"],
                "slug": rec["slug"],
                "title": rec["title"],
                "language": rec["language"],
                "difficulty": rec["difficulty"],
                "concept": rec["concept"],
                "reason": f"Targeted practice to strengthen your weak concept: {rec['concept']}."
            }

    # Fallback to any unsolved problem
    cursor.execute("""
    SELECT p.id, p.slug, p.title, p.language, p.difficulty, p.concept
    FROM problems p
    WHERE p.id NOT IN (SELECT problem_id FROM attempts WHERE user_id = ? AND is_solved = 1)
    ORDER BY p.id ASC
    LIMIT 1
    """, (user_id,))
    rec = cursor.fetchone()
    conn.close()

    if rec:
        return {
            "id": rec["id"],
            "slug": rec["slug"],
            "title": rec["title"],
            "language": rec["language"],
            "difficulty": rec["difficulty"],
            "concept": rec["concept"],
            "reason": "Next challenge in your curriculum."
        }

    return {
        "slug": "c-recursive-factorial-bug",
        "title": "Recursive Factorial Function",
        "language": "c",
        "difficulty": "Beginner",
        "concept": "Recursion & Base Cases",
        "reason": "Recommended general challenge."
    }

if __name__ == "__main__":
    print("Testing Debugging DNA & Classifier...")
    # Test classifier on C off-by-one
    sample_c = """
    for (int i = 0; i <= size; i++) {
        printf("%d ", arr[i]);
    }
    """
    res = classify_mistakes(sample_c, "c", "segmentation fault (core dumped)")
    print("Classification Result:", json.dumps(res, indent=2))

    # Test student DNA calculation for Alex Sharma (user_id = 1)
    dna = get_student_dna(1)
    print(f"Alex's DNA: Total={dna['summary']['total_attempts']}, Solved={dna['summary']['solved_count']}, Rate={dna['summary']['solve_rate']}%")
    print("Insights:", [ins["text"] for ins in dna["personalized_insights"]])

    # Test empty state for new user (user_id = 3)
    new_dna = get_student_dna(3)
    print("New Student Empty State:", new_dna["empty_state"], new_dna["personalized_insights"][0]["text"])
