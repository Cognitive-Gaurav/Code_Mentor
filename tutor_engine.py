"""
CodeMentor - Hint-First Learning Engine (8 Progressive Stages)
Guides students to think, hypothesize, and debug before viewing complete solutions.
Supports English & Hinglish, live AI providers (OpenAI / Groq), and deterministic
pedagogical fallback for seamless hackathon demos.
"""

import os
import json
import logging
import re
import difflib
import urllib.request
import urllib.error
import socket
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
import database
import code_executor
import analyzer

logger = logging.getLogger("codementor.ai")

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna").strip() or "gpt-6-luna"
OPENAI_URL = "https://api.openai.com/v1/responses"
GROQ_MODEL = "openai/gpt-oss-20b"
AI_TIMEOUT_SECONDS = 60
AI_MAX_ATTEMPTS_PER_PROVIDER = 2
AI_RETRY_BACKOFF_SECONDS = 0.25
AI_CHALLENGE_MAX_OUTPUT_TOKENS = 3000
_PLACEHOLDER_KEYS = {"your_openai_api_key_here", "your_groq_api_key_here", "paste_key_here", "changeme"}

def _provider_label(provider: str) -> str:
    return {"openai": "OpenAI", "groq": "Groq", "local": "Local"}.get(provider, "Local")

def _configured_api_key(provider: str) -> str:
    env_name = "OPENAI_API_KEY" if provider == "openai" else "GROQ_API_KEY"
    value = os.environ.get(env_name, "").strip()
    if not value or value.lower() in _PLACEHOLDER_KEYS or value.lower().startswith("your_"):
        return ""
    return value

def _free_only() -> bool:
    return os.environ.get("FREE_ONLY_MODE", "true").strip().lower() not in ("0", "false", "no")

def _provider_order():
    if not _free_only():
        return []
    selected = os.environ.get("AI_PROVIDER", "openai").strip().lower()
    if selected == "local":
        return []
    if selected == "groq":
        return ["groq", "openai"]
    return ["openai", "groq"]

def get_ai_provider_status() -> Dict[str, Any]:
    """Report configured credentials separately from a real API connection test."""
    keys = {"openai": bool(_configured_api_key("openai")),
            "groq": bool(_configured_api_key("groq"))}
    order = _provider_order()
    available = next((name for name in order if keys[name]), None)
    model = OPENAI_MODEL if available == "openai" else GROQ_MODEL if available == "groq" else "codementor-heuristic-v2"
    return {"configured": available is not None, "connection_tested": False,
            "provider": available or "local", "model": model,
            "free_only": _free_only(), "providers": {k: {"configured": v} for k, v in keys.items()},
            "mode_label": f"{available.title()} key configured; live request not tested" if available else "Local rule-based tutor"}

def _extract_openai_response_text(data: Any) -> str:
    """Read completed text from the Responses API, rejecting partial/refusal output."""
    if not isinstance(data, dict):
        raise ValueError("response_not_object")
    response_status = data.get("status")
    if response_status and response_status != "completed":
        incomplete = data.get("incomplete_details")
        reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
        suffix = ":" + str(reason)[:40] if reason else ""
        raise ValueError("response_not_completed:" + str(response_status)[:40] + suffix)
    output = data.get("output")
    if not isinstance(output, list):
        # Accept the SDK convenience shape in tests or compatible gateways.
        output_text = data.get("output_text")
        return output_text.strip() if isinstance(output_text, str) else ""
    text_parts = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "refusal":
                raise ValueError("response_refusal")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
    return "\n".join(part.strip() for part in text_parts if part.strip())

def _previous_debug_context(attempt_id: int) -> list:
    """Return saved hint/attempt notes for this attempt; do not invent student history."""
    conn = database.get_connection()
    rows = conn.execute("SELECT stage_number, hint_level, hint_type, prompt_shown, student_response FROM hint_logs WHERE attempt_id=? AND hint_type IN ('hint_request','prediction','investigation','diagnosis_context','verification') ORDER BY id DESC LIMIT 12", (attempt_id,)).fetchall()
    conn.close()
    return [{"stage": row["stage_number"], "hint_level": row["hint_level"], "type": row["hint_type"], "hint_or_prompt": row["prompt_shown"], "student_response": row["student_response"]} for row in reversed(rows)]

def _student_error_context(attempt_id: int) -> str:
    conn = database.get_connection()
    row = conn.execute("SELECT prompt_shown FROM hint_logs WHERE attempt_id=? AND hint_type='diagnosis_context' ORDER BY id DESC LIMIT 1", (attempt_id,)).fetchone()
    conn.close()
    return row["prompt_shown"] if row else ""

def _saved_test_results(attempt):
    """Return only valid test result records saved by the real code runner."""
    try:
        saved = json.loads(attempt.get("last_test_results_json") or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(saved, dict):
        return []
    results = saved.get("test_results", [])
    return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []

def _saved_execution_errors(attempt):
    """Return compiler/runtime diagnostics saved by verification, including zero-test compiles."""
    try:
        saved = json.loads(attempt.get("last_test_results_json") or "{}")
    except (TypeError, ValueError):
        saved = {}
    errors = []
    if isinstance(saved, dict) and saved.get("compile_error"):
        errors.append(str(saved["compile_error"]).strip())
    errors.extend(str(item.get("error", "")).strip()
                  for item in _saved_test_results(attempt)
                  if item.get("passed") is False and item.get("error"))
    unique = list(dict.fromkeys(error for error in errors if error))
    return "\n".join(unique)[:4000]

def _has_compile_failure(attempt):
    try:
        saved = json.loads(attempt.get("last_test_results_json") or "{}")
    except (TypeError, ValueError):
        saved = {}
    return bool(isinstance(saved, dict) and (saved.get("compile_error") or saved.get("is_compiled") is False))

def _local_hint(attempt, diagnostic, level, lang):
    """Build a deterministic clue from actual source, analyzer evidence and failed tests."""
    code = attempt.get("current_code", "")
    category = diagnostic.get("category", attempt.get("mistake_category_slug") or "algorithm_logic")
    confidence = float(diagnostic.get("confidence", 0) or 0)
    failures = [item for item in _saved_test_results(attempt) if item.get("passed") is False]
    failure = failures[0] if failures else None
    lines = code.splitlines()
    supported_lines = [number for number in diagnostic.get("suspected_lines", [])
                       if isinstance(number, int) and 1 <= number <= len(lines)]
    compiler_or_runtime = " ".join(str(item.get("error", "")) for item in _saved_test_results(attempt)).lower()
    runtime_line = re.search(r"\bline\s+(\d+)\b|\.java:(\d+)", compiler_or_runtime, re.I)
    if runtime_line:
        reported_line = next((int(value) for value in runtime_line.groups() if value), None)
        if reported_line and 1 <= reported_line <= len(lines) and reported_line not in supported_lines:
            supported_lines.insert(0, reported_line)
    source_evidence = lines[supported_lines[0] - 1].strip() if supported_lines else ""
    if failure:
        expected = str(failure.get("expected_output", "")).strip()
        actual = str(failure.get("actual_output", "")).strip()
        error = str(failure.get("error", "")).strip()
        label = str(failure.get("description", "failing test"))
        input_value = str(failure.get("input", "")).strip()
        evidence = (f"Test '{label}'" + (f" with input '{input_value}'" if input_value else "") +
                    (f" expected '{expected}' but got '{actual}'." if expected or actual else f" failed: {error or 'the output did not match'}."))
    else:
        evidence = ""

    concepts = {
        "array_indexing": ("array index aur valid range", "last valid array index"),
        "off_by_one": ("loop ki boundary", "first aur last loop value"),
        "syntax_errors": ("compiler/parser error aur uske paas ke code", "reported token ke aas-paas ka syntax"),
        "recursion_misunderstanding": ("base case aur recursive call", "smallest input se base case"),
        "loop_logic": ("loop condition, body aur update", "counter ka har iteration mein badlav"),
        "variable_scope": ("variable kahan declare aur initialize hua", "variable ki declaration aur use"),
        "incorrect_conditions": ("condition ke true/false cases", "comparison condition ka actual result"),
        "algorithm_logic": ("algorithm ke intermediate values", "ek chhota failing input aur expected result"),
    }
    topic, focus = concepts.get(category, concepts["algorithm_logic"])
    if (str(attempt.get("language", "")).lower() in ("c", "cpp", "c++")
            and "segmentation fault" in compiler_or_runtime and "[" not in code and "*" in code):
        topic, focus = ("pointer initialization and dereferencing", "whether the pointer holds a valid address before dereferencing it")
    elif "typeerror" in compiler_or_runtime or "unsupported operand" in compiler_or_runtime:
        topic, focus = ("operation mein use ho rahe values ke types", "failing expression ke operands ke types")
    elif "nullpointerexception" in compiler_or_runtime or "null pointer" in compiler_or_runtime:
        topic, focus = ("object/reference initialize hua hai ya nahi", "failing method call se pehle reference ki value")
    elif "nameerror" in compiler_or_runtime or "cannot find symbol" in compiler_or_runtime or "undeclared identifier" in compiler_or_runtime:
        topic, focus = ("identifier ya method ka naam aur scope", "reported identifier ki declaration, spelling aur scope")
    elif "timed out" in compiler_or_runtime or "recursionerror" in compiler_or_runtime:
        topic, focus = ("loop/recursive call ki stopping condition", "har iteration ya recursive call ka termination ki taraf progress")
    is_hinglish = lang == "hinglish"
    if confidence < 0.65 and not failure:
        missing_context = not str(attempt.get("description") or "").strip()
        return (("Abhi exact bug confirm nahi hota. Program se kya expected hai, ek sample input, aur uska expected output batao." if missing_context
                 else "Abhi evidence se exact bug confirm nahi hota. Chhote input par run karke expected aur actual output share karo.") if is_hinglish
                else (("The exact issue is unclear. Please provide what the program should do, one sample input, and its expected output." if missing_context
                      else "The available evidence does not confirm a specific bug yet. Run a small input and share expected and actual output.")))
    if level == 1:
        if failure:
            text = f"{evidence} Is result ko {topic} ke saath compare karo." if is_hinglish else f"{evidence} Compare this result with the code's handling of {topic}."
        else:
            text = f"{topic} ko chhote example par trace karo. Pehla difference dhoondo; abhi fix nahi bataya." if is_hinglish else f"Trace {topic} with a small example. Find the first difference; no fix yet."
        return "Hint 1/4 · Conceptual direction: " + text
    if level == 2:
        if source_evidence:
            location = f"Line {supported_lines[0]} par `{source_evidence}` dekho. " if is_hinglish else f"Inspect line {supported_lines[0]}: `{source_evidence}`. "
        else:
            location = "Diagnosis evidence dekho: " if is_hinglish else "Review the diagnosis evidence: "
        return f"Hint 2/4 · Code evidence: {location}{evidence or diagnostic.get('evidence', 'Check this behavior with a small input.')}"
    if level == 3:
        if is_hinglish:
            return f"Hint 3/4 · Specific clue: {evidence + ' ' if evidence else ''}{focus} ko failing input ke saath step-by-step trace karo. Pehla mismatch dhoondo."
        return f"Hint 3/4 · Specific clue: {evidence + ' ' if evidence else ''}Trace {focus} using the failing input and find the first mismatch."
    if category == "off_by_one" and "<=" in code:
        detail = ("Loop condition ka `<=` endpoint ko include karta hai. Check karo ki ek extra iteration ho rahi hai ya nahi." if is_hinglish
                  else "The `<=` loop condition includes its endpoint. Check whether that creates one extra iteration.")
    elif category == "off_by_one" and str(attempt.get("language", "")).lower() in ("c", "cpp", "c++") and re.search(r"<\s*(?:size|len|length|n)\s*-\s*1", code, re.I):
        detail = ("Current boundary last element se pehle ruk sakti hai. Elements ki count aur loop ke total iterations compare karo, taaki aakhri valid element bhi process ho." if is_hinglish
                  else "This boundary may stop before the last element. Compare the element count with the loop iterations so the final valid element is processed.")
    elif category == "off_by_one" and str(attempt.get("language", "")).lower() == "python" and re.search(r"range\s*\(\s*[^,]+,\s*n\s*\)", code):
        detail = ("Python `range(start, stop)` stop ko include nahi karta. Agar n include karna hai, stop ko n se ek aage set karke failing aur edge case dobara run karo." if is_hinglish
                  else "Python's `range(start, stop)` excludes stop. If n must be included, set stop one beyond n and rerun the failing and an edge case.")
    elif category == "array_indexing":
        detail = ("N elements ke valid indices 0 se N-1 tak hote hain. Loop boundary aur array access ko isi range mein rakho." if is_hinglish
                  else "For N elements, valid indices are 0 through N-1. Keep the loop boundary and array access within that range.")
    elif category == "recursion_misunderstanding":
        detail = ("Sabse chhote valid input ka base-case answer check karo; har recursive call ko us case ke kareeb jaana chahiye." if is_hinglish
                  else "Check the base-case answer for the smallest valid input; each recursive call should move closer to it.")
    else:
        detail = (f"{evidence + ' ' if evidence else ''}{focus} ke liye ek targeted change socho, phir failing aur edge case dobara run karo." if is_hinglish
                  else f"{evidence + ' ' if evidence else ''}Make one targeted change to {focus}, then rerun the failing and an edge case.")
    return "Hint 4/4 · Near-solution guidance: " + detail

def _validated_ai_hint(candidate, attempt, diagnostic, level, previous_hints):
    """Reject unsupported, repetitive or solution-revealing model output."""
    if not isinstance(candidate, str):
        return False
    text = candidate.strip()
    if not text or len(text) > 900 or "```" in text or len(text.splitlines()) > 4:
        return False
    if re.search(r"(?:^|\n)\s*(?:def\s+\w+\s*\(|public\s+static\s+void\s+main|int\s+main\s*\(|#include\s*<)", text, re.I):
        return False
    supported_lines = {number for number in diagnostic.get("suspected_lines", []) if isinstance(number, int)}
    referenced_lines = set()
    for match in re.finditer(r"\bline\s*#?\s*(\d+)\b|\bL(\d+)\b", text, re.I):
        referenced_lines.add(int(match.group(1) or match.group(2)))
    if level == 1 and (referenced_lines or "`" in text):
        return False
    if referenced_lines - supported_lines:
        return False
    failures = [item for item in _saved_test_results(attempt) if item.get("passed") is False]
    source_lines = attempt.get("current_code", "").splitlines()
    grounded_source = " ".join(source_lines[number - 1] for number in supported_lines if 1 <= number <= len(source_lines))
    grounding = " ".join([
        str(attempt.get("description") or ""), str(diagnostic.get("evidence") or ""),
        grounded_source,
        " ".join(str(value) for row in failures for value in (row.get("description"), row.get("input"), row.get("expected_output"), row.get("actual_output"), row.get("error")) if value)
    ]).lower()
    candidate_lower = text.lower()
    identifiers = {token.lower() for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", attempt.get("current_code", "")) if len(token) >= 3}
    evidence_terms = {token.lower() for token in re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", grounding) if len(token) >= 4}
    anchored = any(term in candidate_lower for term in (identifiers & evidence_terms))
    anchored = anchored or any(str(value).strip() and str(value).strip().lower() in candidate_lower
                               for row in failures for value in (row.get("expected_output"), row.get("actual_output")))
    if not anchored:
        return False
    normalized = re.sub(r"\W+", " ", candidate_lower).strip()
    for old in previous_hints:
        old_text = re.sub(r"\W+", " ", str(old.get("hint_or_prompt", "")).lower()).strip()
        if old_text and difflib.SequenceMatcher(None, normalized, old_text).ratio() >= 0.78:
            return False
    # Early levels must not contain a copy of the reference solution.
    if level < 4:
        solution = str(attempt.get("solution_code", ""))
        if solution and len(solution) > 30 and solution.strip() in text:
            return False
    return True

def _generate_hint(attempt, diagnostic, level, lang, fallback):
    """Ask configured providers with structured evidence; safely fall back locally."""
    confidence = float(diagnostic.get("confidence", 0) or 0)
    failed_tests = [item for item in _saved_test_results(attempt) if item.get("passed") is False]
    if confidence < 0.65 and not failed_tests:
        logger.info("tutor_response interaction=hint provider=local reason=low_evidence")
        return fallback, "local", "codementor-evidence-hints-v2"
    context = {
        "hint_level": level,
        "language": attempt.get("language"),
        "code": attempt.get("current_code", ""),
        "problem_statement": attempt.get("description") or "not provided",
        "diagnostic": diagnostic,
        "failed_tests": failed_tests,
        "compiler_or_runtime_errors": ([_saved_execution_errors(attempt)] if _saved_execution_errors(attempt) else
                                        [row.get("error") for row in failed_tests if row.get("error")]),
        "previous_hints_and_attempts": [item for item in _previous_debug_context(attempt.get("id"))
                                         if item.get("type") == "hint_request" and item.get("hint_level") is not None
                                         and item["hint_level"] < level and item.get("hint_or_prompt") != "__PENDING_HINT__"],
        "verified_suspected_lines": diagnostic.get("suspected_lines", []),
        "safe_local_fallback": fallback,
    }
    prompt = (f"Create exactly one progressive debugging hint in {lang}. Return only the hint text, under 100 words. "
              f"Level 1: conceptual direction, no exact line or fix. Level 2: investigation clue using only verified code evidence. "
              f"Level 3: likely root cause based on diagnostic/test output. Level 4: near-solution guidance, never the full solution. "
              f"Do not invent expected behavior, output or line numbers. If the problem statement/evidence is insufficient, say what is missing. "
              f"Structured diagnostic context: {json.dumps(context, ensure_ascii=False)}")
    ai = generate_ai(prompt, "You are a careful debugging tutor. Stay strictly within the provided evidence; do not guess.", "hint_ladder")
    if ai.get("text"):
        if _validated_ai_hint(ai["text"], attempt, diagnostic, level, context["previous_hints_and_attempts"]):
            logger.info("ai_response_validation timestamp=%s feature=hint_ladder candidate_provider=%s outcome=accepted final_provider=%s validation_reason=passed retry_count=%s duration_ms=%s",
                        ai.get("request_timestamp", "unknown"), ai.get("provider", "unknown"), ai.get("provider", "unknown"), ai.get("retry_count", 0), ai.get("duration_ms", 0))
            return ai["text"].strip(), ai["provider"], ai.get("model") or "unknown"
        logger.warning("ai_response_validation timestamp=%s feature=hint_ladder candidate_provider=%s outcome=rejected validation_reason=safety_or_evidence_check final_provider=local retry_count=%s duration_ms=%s",
                       ai.get("request_timestamp", "unknown"), ai.get("provider", "unknown"), ai.get("retry_count", 0), ai.get("duration_ms", 0))
    else:
        logger.info("ai_response_validation feature=hint_ladder interaction=hint candidate_provider=none outcome=unavailable final_provider=local")
    reason = "ai_failure" if not ai.get("text") else "ai_hint_rejected"
    logger.info("tutor_response interaction=hint provider=local reason=%s", reason)
    return fallback, "local", "codementor-evidence-hints-v2"

def _debug_response_is_safe(text, code, diagnostics):
    """Keep tutor turns short and prevent chat from becoming an answer leak."""
    if not isinstance(text, str):
        return False
    value = text.strip()
    if not value or len(value) > 1200 or "```" in value or len(value.splitlines()) > 8:
        return False
    if re.search(r"(?:replace|change|write|use)\s+(?:this|the|line|it)?\s*.*\b(with|to)\b.{0,100}(?:[=+*/<>-]|return\s|print\s*\()", value, re.I):
        return False
    if re.search(r"\b(?:def\s+\w+|return\s+|System\.out\.|printf\s*\(|#include\s*<)", value, re.I):
        return False
    code_lines = {line.strip().lower() for line in str(code).splitlines() if len(line.strip()) >= 16}
    if any(line in value.lower() for line in code_lines):
        return False
    valid_lines = {line for item in diagnostics for line in item.get("suspected_lines", []) if isinstance(line, int)}
    for match in re.finditer(r"\bline\s*#?\s*(\d+)\b", value, re.I):
        if int(match.group(1)) not in valid_lines:
            return False
    return True

def _sanitize_debug_error(value):
    """Keep diagnostic messages while removing paths, echoed code, and caret lines."""
    lines = str(value or "").splitlines()
    useful = [line.strip() for line in lines if re.search(r"\berror\b|exception|warning", line, re.I)]
    cleaned = " ".join(useful or lines[:1])
    cleaned = re.sub(r"(?i)[A-Z]:\\[^:\r\n]*", "source file", cleaned)
    cleaned = re.sub(r"/(?:[^:/\s]+/)*[^:/\s]+(?=:\d)", "source file", cleaned)
    return cleaned[:500]

def _friendly_debug_error(attempt, error, lang):
    """Turn common Java compiler/runtime phrases into beginner-friendly directions."""
    safe_error = _sanitize_debug_error(error)
    source_lines = str(attempt.get("current_code", "")).splitlines()
    line_match = re.search(r"(?:\.java|\.c|\.cpp|\.py)?:(\d+)(?::\d+)?|\bline\s+(\d+)", safe_error, re.I)
    line_num = next((int(value) for value in line_match.groups() if value), None) if line_match else None
    line_ref = f"line {line_num}" if line_num and line_num <= len(source_lines) else "the reported statement"
    language = str(attempt.get("language", "")).lower()
    lowered = safe_error.lower()
    hinglish = lang == "hinglish"
    if language == "java" and "unclosed string literal" in lowered:
        return (f"Java keh raha hai ki {line_ref} par text string ke opening double quote (`\"`) ka matching closing quote nahi mila. Usi line par dono `\"` check karein; Java string ko seedha agli line par nahi chhod sakte. Editor mein quote fix karke dobara Run & Verify karein." if hinglish else
                f"Java says the text string on {line_ref} starts with a double quote (`\"`) but has no matching closing quote. Check that both `\"` marks are on the intended line; Java strings cannot continue onto the next line directly. Fix the quote in the editor, then run Verify again.")
    if language == "java" and ("';' expected" in lowered or "not a statement" in lowered or "illegal start of expression" in lowered):
        semicolon_note = (" Java ko statement ke end ko dikhane ke liye semicolon (`;`) chahiye." if hinglish else
                          " Java needs a semicolon (`;`) to mark the statement's end.") if "';' expected" in lowered else ""
        return (f"Java compiler ne {line_ref} par syntax error diya. Iska matlab hai ki us jagah ka code Java ke liye valid, complete statement nahi ban raha; compiler ko statement ka sahi structure chahiye.{semicolon_note} Us expression ko dekho—kaunsa hissa Java ko incomplete ya standalone invalid lag raha hai?" if hinglish else
                f"The Java compiler reported a syntax error at {line_ref}. The code there does not form a valid, complete Java statement.{semicolon_note} Looking at that expression, which part do you think the compiler cannot parse as a complete statement?")
    if language == "java" and "cannot find symbol" in lowered:
        return (f"Java ko {line_ref} par istemal hua naam nahi mil raha. Naam ki spelling aur capitalization check karein, aur dekhein ki variable ya method pehle declare hua hai." if hinglish else
                f"Java cannot find a name used near {line_ref}. Check its spelling and capitalization, and make sure the variable or method was declared before use.")
    if language == "java" and "arrayindexoutofboundsexception" in lowered:
        return (f"Java keh raha hai ki {line_ref} par array ke bahar ka index use hua. Agar array mein N items hain, valid index 0 se N-1 tak hota hai. Index nikalne wali condition check karein." if hinglish else
                f"Java says code near {line_ref} used an array index outside its bounds. For N items, valid indexes are 0 through N-1. Check the condition that calculates the index.")
    if language == "java" and "nullpointerexception" in lowered:
        return (f"Java keh raha hai ki {line_ref} ke paas kisi object ka reference abhi set nahi tha, phir bhi uska method ya field use hua. Us reference ko use se pehle initialize hua hai ya nahi, check karein." if hinglish else
                f"Java says code near {line_ref} tried to use an object reference that has no object assigned to it yet. Check whether that reference is initialized before its method or field is used.")
    if hinglish:
        return f"Compiler/runtime ne {line_ref} par yeh error diya: `{safe_error}`. Iska simple matlab samajhne ke liye us line aur uske aas-paas ka code check karein."
    return f"The compiler/runtime reported an error near {line_ref}: `{safe_error}`. Check that line and the nearby code to understand what it is pointing to."

def _local_debug_response(student_text, attempt, diagnostic, lang, prior_hints):
    """Contextual deterministic tutor when AI is unavailable or unsafe."""
    text = student_text.strip().lower()
    is_hi = lang == "hinglish"
    observed_error = _saved_execution_errors(attempt) or str(attempt.get("diagnosis_context", "")).strip()
    observed_lower = observed_error.lower()
    compile_failure = (_has_compile_failure(attempt) or any(marker in observed_lower for marker in
        ("not a statement", "';' expected", "syntax error", "illegal start of expression")))
    asks_solution = bool(re.search(r"\b(give me|show me|tell me|want|need)\b.{0,35}\b(answer|solution|corrected code|full code)\b|\b(solution|answer)\s+(please|now)\b", text))
    unsure = bool(re.search(r"\b(i don't know|dont know|not sure|no idea|idk|samajh nahi|pata nahi)\b", text))
    if asks_solution:
        reply = ("Main chat mein poora solution reveal nahi karunga. Existing Solution Unlock policy follow hoti hai; abhi code ka failing test dekho aur batao expected aur actual result mein pehla difference kya hai. Zarurat ho to Hint Ladder ka agla clue lo." if is_hi else
                 "I won't reveal the full solution in chat. The existing Solution Unlock policy still applies. For now, compare the failing test's expected and actual result and tell me the first difference you notice. You can also use the next Hint Ladder clue.")
        return "solution_request", reply
    if bool(attempt.get("is_solved")) and (unsure or len(text) < 8):
        reply = ("Code ke verified run mein saare required tests pass hue. Ab apne words mein batao fix ne pehle wale bug ko kyun roka, aur kaunsa edge case iska proof hai." if is_hi else
                 "The verified run passed all required tests. In your own words, explain why the change prevents the original bug and name one edge case that supports your explanation.")
        return "verification_passed", reply
    confidence = float(diagnostic.get("confidence", 0) or 0)
    failures = [item for item in _saved_test_results(attempt) if item.get("passed") is False]
    if unsure:
        if compile_failure and observed_error:
            explanation = _friendly_debug_error(attempt, observed_error, lang)
            reply = (f"Koi baat nahi. {explanation}" if is_hi else f"That is okay. {explanation}")
            return "unsure", reply
        has_output_evidence = bool(failures and (str(failures[0].get("expected_output", "")).strip() or str(failures[0].get("actual_output", "")).strip()))
        if failures and has_output_evidence:
            result = failures[0]
            expected = str(result.get("expected_output", "")).strip()
            actual = str(result.get("actual_output", "")).strip()
            reply = (f"Koi baat nahi. Failing test mein expected `{expected}` aur actual `{actual}` hai. Sirf us input ke liye code ko step-by-step trace karo: pehla intermediate value kahan alag hota hai?" if is_hi else
                     f"That is okay. This failing test expected `{expected}` but received `{actual}`. Trace the code only for that input: which intermediate value first becomes different?")
        elif observed_error:
            explanation = _friendly_debug_error(attempt, observed_error, lang)
            reply = (f"Koi baat nahi. {explanation}" if is_hi else f"That is okay. {explanation}")
        elif confidence < 0.65:
            reply = ("Abhi diagnosis confirm nahi hai. Program ka expected behavior aur ek sample input/output batao; phir hum evidence ke saath investigate karenge." if is_hi else
                     "The diagnosis is not confirmed yet. Please share the intended behavior and one sample input/output, then we can investigate from evidence.")
        else:
            reply = (f"Chalo chhote example se shuru karte hain. {diagnostic.get('evidence', 'Detected code pattern')} ko ek-ek step trace karo. Tumhare hisaab se sabse pehle kya hota hai?" if is_hi else
                     f"Let's start with a small example. Trace this evidence one step at a time: {diagnostic.get('evidence', 'the detected code pattern')}. What do you think happens first?")
        return "unsure", reply

    category = diagnostic.get("category", "algorithm_logic")
    criteria = {
        "off_by_one": ["loop", "boundary", "range", "endpoint", "extra iteration", "extra time", "one too many", "last element", "inclusive", "exclusive", "excludes", "omits", "short by", "index"],
        "array_indexing": ["array", "index", "bound", "length", "size", "element"],
        "recursion_misunderstanding": ["recursion", "recursive", "base case", "stop", "terminat"],
        "incorrect_conditions": ["condition", "comparison", "true", "false", "even", "odd", "operator"],
        "syntax_errors": ["syntax", "compiler", "token", "semicolon", "bracket", "parenthes"],
        "loop_logic": ["loop", "condition", "counter", "iteration", "terminat", "increment"],
        "algorithm_logic": ["expected", "actual", "logic", "intermediate", "calculation", "operation"],
        "variable_scope": ["scope", "declare", "initialize", "variable", "value"],
    }
    anchors = criteria.get(category, criteria["algorithm_logic"])
    matches = [term for term in anchors if term in text]
    causal_link = bool(re.search(r"\b(because|caus(?:e|es|ed)|so that|means|omits|skips|excludes|includes|stops before|goes past|allows|prevents|due to|results in|extra|too many|rather than|instead of|different from|does not|doesn't)\b", text))
    if bool(attempt.get("is_solved")) and len(matches) >= 2 and causal_link:
        assessment = "verification_passed"
        reply = ("Tumhari explanation verified bug aur fix ke causal link ko sahi pakadti hai, aur actual test runner ne required tests pass kiye. Ab ek edge case batao jo is boundary/logic ko check karta hai." if is_hi else
                 "Your explanation connects the verified bug to the fix, and the actual test runner passed the required tests. Name one edge case that checks this boundary or logic.")
    elif len(matches) >= 2 and causal_link:
        assessment = "correct"
        reply = (f"Sahi direction pakdi—tumne {', '.join(matches[:2])} mention kiya. Ab isi ko failing input ya error evidence se verify karo: actual code step-by-step kya karta hai?" if is_hi else
                 f"That's the right direction—you identified {', '.join(matches[:2])}. Now verify it against the failing input or error evidence: what does the current code do step by step?")
    elif matches or causal_link:
        assessment = "partially_correct"
        reply = (f"Tumhara {matches[0]} wala observation relevant hai, lekin diagnosis abhi poora confirm nahi hua. Expected aur actual result compare karke batao kaunsa code step us difference ko create karta hai." if is_hi else
                 f"Your observation about {matches[0]} is relevant, but the diagnosis is not fully established yet. Compare expected and actual results and point to the code step that creates the difference.")
    elif confidence < 0.65 and not failures:
        assessment = "uncertain"
        reply = ("Is code se abhi exact bug prove nahi hota. Expected behavior aur sample input/output share karoge?" if is_hi else
                 "This code alone does not prove a specific bug yet. Could you share the intended behavior and a sample input/output?")
    elif len(text) < 8:
        assessment = "needs_detail"
        reply = ("Thoda aur detail do: tumhe code kis line ya behavior par doubt hai, aur tumne kya expect kiya tha?" if is_hi else
                 "Could you add a little detail: which behavior seems wrong, and what did you expect to happen?")
    else:
        assessment = "incorrect_or_unrelated"
        if failures:
            result = failures[0]
            if compile_failure and observed_error:
                explanation = _friendly_debug_error(attempt, observed_error, lang)
                reply = (f"Yeh explanation compiler evidence se match nahi karti. {explanation}" if is_hi else
                         f"That explanation does not match the compiler evidence. {explanation}")
                return assessment, reply
            expected = " ".join(str(result.get("expected_output", "")).split())
            actual = " ".join(str(result.get("actual_output", "")).split())
            error = result.get("error") or observed_error
            if expected or actual:
                evidence = (f"expected `{expected}` but got `{actual}`" if expected and actual else
                            f"expected `{expected}`" if expected else f"actual output was `{actual}`")
                reply = (f"Yeh explanation saved test evidence se match nahi karti: {evidence}. Failing input par relevant {category.replace('_', ' ')} ko trace karke pehla mismatch dhoondo." if is_hi else
                         f"That explanation does not match the saved test evidence: {evidence}. Trace the relevant {category.replace('_', ' ')} for the failing input and find the first mismatch.")
            elif error:
                explanation = _friendly_debug_error(attempt, error, lang)
                reply = (f"Yeh explanation actual error se match nahi karti. {explanation}" if is_hi else
                         f"That explanation does not match the observed error. {explanation}")
            else:
                reply = ("Saved test fail hua, lekin uske saath expected/actual output ya error save nahi hua. Code dobara Run & Verify karke actual result share karo; phir hum sahi clue nikaalenge." if is_hi else
                         "The saved test failed, but no expected/actual output or error was recorded. Run & Verify again and share the observed result so we can choose a grounded clue.")
        else:
            reply = (f"Yeh explanation abhi {category.replace('_', ' ')} evidence se connect nahi hui. Chhote input par actual behavior trace karke apni reasoning dobara batao." if is_hi else
                     f"That explanation is not connected to the {category.replace('_', ' ')} evidence yet. Trace a small input and tell me your reasoning again.")
    return assessment, reply

def _debugging_tutor_turn(attempt_id, student_text, lang="en", code_snapshot=None):
    """Save a contextual student/tutor turn and return the updated session."""
    conn = database.get_connection()
    row = conn.execute("""SELECT a.*, p.title, p.description, p.concept, p.test_cases_json,
        p.mistake_category_slug, p.solution_code FROM attempts a LEFT JOIN problems p ON p.id=a.problem_id
        WHERE a.id=?""", (attempt_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "Attempt not found"}
    attempt = dict(row)
    snapshot_matches_verified = code_snapshot is None or code_snapshot == attempt.get("current_code", "")
    if isinstance(code_snapshot, str) and len(code_snapshot) <= 100000:
        attempt["current_code"] = code_snapshot
    if not snapshot_matches_verified:
        # A previous run's diagnostics must not be attached to newly edited code.
        attempt["last_test_results_json"] = "{}"
        attempt["is_solved"] = 0
        attempt["verification_status"] = "unverified"
    lang = "hinglish" if str(lang).lower() in ("hi", "hinglish") else "en"
    failures = [item for item in _saved_test_results(attempt) if item.get("passed") is False]
    compiler_errors = _saved_execution_errors(attempt) if snapshot_matches_verified else ""
    error_context = "\n".join(filter(None, [attempt.get("description", ""), _student_error_context(attempt_id) if snapshot_matches_verified else "",
        compiler_errors, "\n".join(str(item.get("error", "")) for item in failures if item.get("error"))]))
    attempt["diagnosis_context"] = _student_error_context(attempt_id) if snapshot_matches_verified else ""
    diagnosis = analyzer.classify_mistakes(attempt.get("current_code", ""), attempt.get("language", "python"),
        error_message=error_context, compiler_error=compiler_errors or error_context, test_results=failures,
        expected_category=attempt.get("mistake_category_slug"))
    diagnostics = diagnosis.get("diagnostics", [])
    primary = diagnostics[0] if diagnostics else {"category": attempt.get("mistake_category_slug") or "algorithm_logic", "confidence": 0.2,
        "evidence": "No strong code or runtime evidence was found.", "suspected_lines": []}
    previous = conn.execute("""SELECT student_response, prompt_shown, hint_provider, hint_model,
        created_at, context_json FROM hint_logs WHERE attempt_id=? AND hint_type='debug_conversation'
        ORDER BY id DESC LIMIT 8""", (attempt_id,)).fetchall()
    conversation = [{"student": item["student_response"], "tutor": item["prompt_shown"],
        "provider": item["hint_provider"], "model": item["hint_model"], "created_at": item["created_at"]}
        for item in reversed(previous)]
    hints = conn.execute("SELECT hint_level,prompt_shown FROM hint_logs WHERE attempt_id=? AND hint_type='hint_request' ORDER BY id", (attempt_id,)).fetchall()
    attempt["id"] = attempt_id
    prior_hints = [{"hint_level": item["hint_level"], "hint_or_prompt": item["prompt_shown"]} for item in hints if item["prompt_shown"] != "__PENDING_HINT__"]
    conn.close()

    assessment, reply = _local_debug_response(student_text, attempt, primary, lang, prior_hints)
    provider, model = "local", "codementor-context-tutor-v1"
    if assessment not in ("solution_request", "unsure", "verification_passed"):
        safe_failures = []
        for item in failures:
            safe_item = dict(item)
            if safe_item.get("error"):
                safe_item["error"] = _sanitize_debug_error(safe_item["error"])
            safe_failures.append(safe_item)
        context = {"language": attempt.get("language"), "response_language": lang,
            "source_code": attempt.get("current_code", ""), "problem_statement": attempt.get("description") or _student_error_context(attempt_id) or "not provided",
            "diagnostics": diagnostics, "compiler_or_runtime_errors": compiler_errors, "failed_tests": safe_failures, "previous_hints": prior_hints,
            "previous_conversation": conversation, "student_message": student_text,
            "verification_status": attempt.get("verification_status"), "is_solved": bool(attempt.get("is_solved"))}
        prompt = ("Return one JSON object only: {\"assessment\": \"correct|partially_correct|incorrect_or_unrelated|needs_detail|uncertain\", \"reply\": \"...\"}. "
            "Act as a beginner debugging tutor. Assess the student's explanation in context, then ask exactly one useful next question. "
            "Never provide corrected code, replacement text, the exact operator/edit, or full solution. Never invent execution or line evidence. "
            "If intended behavior is unknown, request it. Keep reply under 90 words and in the requested language. Context: " + json.dumps(context, ensure_ascii=False))
        ai = generate_ai(prompt, "You are CodeMentor's cautious debugging tutor. Guide investigation; never solve immediately.", "ai_guided_debugging")
        ai_reply_accepted = False
        validation_failure = "invalid_format"
        if ai.get("text"):
            try:
                raw = ai["text"].strip()
                raw = raw[raw.find("{"):raw.rfind("}") + 1]
                parsed = json.loads(raw)
                candidate = parsed.get("reply")
                candidate_assessment = parsed.get("assessment")
                valid_assessments = {"correct", "partially_correct", "incorrect_or_unrelated", "needs_detail", "uncertain"}
                if candidate_assessment in valid_assessments and _debug_response_is_safe(candidate, attempt.get("current_code", ""), diagnostics):
                    assessment, reply = candidate_assessment, candidate.strip()
                    provider, model = ai.get("provider", "local"), ai.get("model", "unknown")
                    ai_reply_accepted = True
                else:
                    validation_failure = "safety_or_schema_rejected"
            except (ValueError, TypeError, AttributeError):
                validation_failure = "invalid_format"
            if ai_reply_accepted:
                logger.info("ai_response_validation timestamp=%s feature=ai_guided_debugging candidate_provider=%s outcome=accepted final_provider=%s validation_reason=passed retry_count=%s duration_ms=%s",
                            ai.get("request_timestamp", "unknown"), ai.get("provider", "unknown"), provider, ai.get("retry_count", 0), ai.get("duration_ms", 0))
            else:
                logger.warning("ai_response_validation timestamp=%s feature=ai_guided_debugging candidate_provider=%s outcome=rejected validation_reason=%s final_provider=local retry_count=%s duration_ms=%s",
                               ai.get("request_timestamp", "unknown"), ai.get("provider", "unknown"), validation_failure, ai.get("retry_count", 0), ai.get("duration_ms", 0))
            if not ai_reply_accepted and provider == "local":
                logger.info("tutor_response interaction=debug provider=local reason=unsafe_or_unusable_ai_reply")
        else:
            logger.info("ai_response_validation feature=ai_guided_debugging interaction=debug candidate_provider=none outcome=unavailable final_provider=local")
            logger.info("tutor_response interaction=debug provider=local reason=api_failure")
    else:
        reason = "compiler_evidence_guidance" if _has_compile_failure(attempt) else f"local_policy_{assessment}"
        logger.info("tutor_response interaction=debug provider=local reason=%s", reason)
    if not _debug_response_is_safe(reply, attempt.get("current_code", ""), diagnostics):
        assessment, reply = _local_debug_response(student_text, attempt, primary, lang, prior_hints)
        provider, model = "local", "codementor-context-tutor-v1"
        logger.info("tutor_response interaction=debug provider=local reason=local_response_safety")

    context_json = json.dumps({"code_snapshot": attempt.get("current_code", ""), "diagnostics": diagnostics,
        "failed_tests": failures, "hint_levels": [item.get("hint_level") for item in prior_hints],
        "assessment": assessment, "matches_latest_verification": snapshot_matches_verified}, ensure_ascii=False)
    conn = database.get_connection()
    conn.execute("""INSERT INTO hint_logs
        (attempt_id, stage_number, hint_type, prompt_shown, student_response, explanation_lang,
         hint_provider, hint_model, context_json)
        VALUES (?, ?, 'debug_conversation', ?, ?, ?, ?, ?, ?)""",
        (attempt_id, attempt.get("current_stage", 1), reply, student_text, lang, provider, model, context_json))
    conn.commit()
    conn.close()
    stage = get_stage_content(attempt_id, lang=lang)
    stage["debug_assessment"] = assessment
    stage["debug_reply"] = reply
    stage["debug_provider"] = provider
    return stage

def generate_ai(prompt: str, system_instruction: str = "", requested_feature: str = "general") -> Dict[str, Any]:
    """Try configured providers in order and report safe per-request diagnostics."""
    request_started = time.monotonic()
    request_timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    errors = []
    attempts = []
    order = _provider_order()
    deadline = time.monotonic() + AI_TIMEOUT_SECONDS
    attempted = False
    logger.info("ai_request timestamp=%s feature=%s configured_order=%s total_timeout_seconds=%s",
                request_timestamp, requested_feature, ",".join(order) if order else "local_only", AI_TIMEOUT_SECONDS)
    for provider in order:
        if time.monotonic() >= deadline:
            logger.warning("ai_provider_skip provider=%s reason=request_deadline_exceeded", provider)
            break
        if provider == "openai":
            key, model = _configured_api_key("openai"), OPENAI_MODEL
            if not key:
                logger.info("ai_provider_skip provider=%s reason=key_unconfigured", provider)
                continue
            url = OPENAI_URL
            max_output_tokens = AI_CHALLENGE_MAX_OUTPUT_TOKENS if requested_feature == "ai_challenge_generation" else 600
            payload = {"model": model, "instructions": system_instruction or "You are a careful CodeMentor tutor.",
                       "input": prompt, "max_output_tokens": max_output_tokens, "store": False}
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        else:
            key, model = _configured_api_key("groq"), GROQ_MODEL
            if not key:
                logger.info("ai_provider_skip provider=%s reason=key_unconfigured", provider)
                continue
            url = "https://api.groq.com/openai/v1/chat/completions"
            max_tokens = AI_CHALLENGE_MAX_OUTPUT_TOKENS if requested_feature == "ai_challenge_generation" else 600
            payload = {"model": model, "messages": [{"role": "system", "content": system_instruction}, {"role": "user", "content": prompt}], "temperature": 0.3, "max_tokens": max_tokens}
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
        attempted = True
        for attempt_number in range(1, AI_MAX_ATTEMPTS_PER_PROVIDER + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome, status, exception_class, answer = "timeout", None, "RequestDeadlineExceeded", ""
                attempts.append({"provider":provider,"model":model,"attempt":attempt_number,
                                 "outcome":outcome,"http_status":None,"exception":exception_class})
                errors.append(f"{provider}: total request timeout")
                logger.warning("ai_provider_result provider=%s model=%s outcome=timeout http_status=none exception=%s attempt=%s",
                               provider, model, exception_class, attempt_number)
                break
            logger.info("ai_provider_attempt provider=%s model=%s attempt=%s", provider, model, attempt_number)
            outcome, status, exception_class, answer = "request_error", None, None, ""
            parse_failure = None
            retry_after = None
            try:
                req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
                with urllib.request.urlopen(req, timeout=min(AI_TIMEOUT_SECONDS, remaining)) as response:
                    status = getattr(response, "status", None) or getattr(response, "code", None) or 200
                    try:
                        data = json.loads(response.read().decode("utf-8"))
                        if provider == "openai":
                            answer = _extract_openai_response_text(data)
                        else:
                            choices = data.get("choices", []) if isinstance(data, dict) else []
                            answer = choices[0].get("message", {}).get("content", "").strip() if choices and isinstance(choices[0], dict) else ""
                    except (ValueError, UnicodeDecodeError, AttributeError, TypeError, IndexError) as exc:
                        outcome = "invalid_response"
                        safe_error = str(exc)
                        if isinstance(exc, ValueError) and (safe_error == "response_refusal" or safe_error.startswith("response_not_completed:")):
                            parse_failure = safe_error[:80]
                        else:
                            parse_failure = "malformed_json_or_provider_schema:" + type(exc).__name__
                    else:
                        outcome = "success" if answer else "invalid_response"
                        if not answer:
                            parse_failure = "empty_text_response"
                event = {"provider": provider, "model": model, "attempt": attempt_number,
                         "outcome": outcome, "http_status": status, "exception": None,
                         "parse_failure": parse_failure,
                         "duration_ms": round((time.monotonic() - request_started) * 1000)}
                attempts.append(event)
                if outcome == "success":
                    duration_ms = round((time.monotonic() - request_started) * 1000)
                    logger.info("ai_provider_result timestamp=%s feature=%s configured_provider=%s attempted_provider=%s candidate_provider=%s final_provider=pending_application_validation model=%s outcome=success http_status=%s retry_count=%s duration_ms=%s",
                                request_timestamp, requested_feature, order[0] if order else "local", provider, provider, model, status, sum(max(0, item["attempt"] - 1) for item in attempts), duration_ms)
                    return {"text": answer, "provider": provider, "model": model, "error": None,
                            "provider_attempts": attempts, "response_status": "valid",
                            "request_timestamp": request_timestamp, "requested_feature": requested_feature,
                            "configured_provider": order[0] if order else "local", "final_provider": provider,
                            "retry_count": sum(max(0, item["attempt"] - 1) for item in attempts),
                            "duration_ms": duration_ms}
                errors.append(f"{provider}: invalid response")
                logger.warning("ai_provider_result timestamp=%s feature=%s configured_provider=%s attempted_provider=%s final_provider=pending model=%s outcome=invalid_response http_status=%s parse_failure=%s retry_count=%s duration_ms=%s",
                               request_timestamp, requested_feature, order[0] if order else "local", provider, model, status, parse_failure or "unknown", sum(max(0, item["attempt"] - 1) for item in attempts), round((time.monotonic() - request_started) * 1000))
                break  # A successful HTTP response with unusable content is not a transport retry.
            except urllib.error.HTTPError as exc:
                status = exc.code
                exception_class = type(exc).__name__
                outcome = "http_error"
                raw_retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    retry_after = min(2.0, max(0.0, float(raw_retry_after))) if raw_retry_after else None
                except (TypeError, ValueError):
                    retry_after = None
            except (TimeoutError, socket.timeout) as exc:
                exception_class, outcome = type(exc).__name__, "timeout"
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                exception_class = type(reason).__name__ if reason is not None else type(exc).__name__
                outcome = "timeout" if isinstance(reason, (TimeoutError, socket.timeout)) else "network_error"
            except Exception as exc:
                exception_class, outcome = type(exc).__name__, "request_error"

            event = {"provider": provider, "model": model, "attempt": attempt_number,
                     "outcome": outcome, "http_status": status, "exception": exception_class,
                     "parse_failure": None,
                     "duration_ms": round((time.monotonic() - request_started) * 1000)}
            attempts.append(event)
            errors.append(f"{provider}: HTTP {status}" if status else f"{provider}: {outcome} ({exception_class})")
            logger.warning("ai_provider_result timestamp=%s feature=%s configured_provider=%s attempted_provider=%s final_provider=pending model=%s outcome=%s http_status=%s exception=%s retry_count=%s duration_ms=%s",
                           request_timestamp, requested_feature, order[0] if order else "local", provider, model, outcome, status or "none", exception_class or "none", sum(max(0, item["attempt"] - 1) for item in attempts), round((time.monotonic() - request_started) * 1000))

            retryable = (status == 429 or (status is not None and 500 <= status <= 599)
                         or outcome in ("timeout", "network_error"))
            if retryable and attempt_number < AI_MAX_ATTEMPTS_PER_PROVIDER:
                wait_seconds = retry_after if retry_after is not None else AI_RETRY_BACKOFF_SECONDS
                time.sleep(min(wait_seconds, max(0.0, deadline - time.monotonic())))
                continue
            break
    if not attempted:
        logger.info("ai_provider_result provider=local outcome=no_provider_configured")
    duration_ms = round((time.monotonic() - request_started) * 1000)
    logger.info("ai_provider_fallback timestamp=%s feature=%s configured_provider=%s attempted_provider=%s final_provider=local error_category=%s retry_count=%s duration_ms=%s",
                request_timestamp, requested_feature, order[0] if order else "local",
                ",".join(dict.fromkeys(item["provider"] for item in attempts)) or "none",
                ",".join(dict.fromkeys(item["outcome"] for item in attempts)) or "not_configured",
                sum(max(0, item["attempt"] - 1) for item in attempts), duration_ms)
    final_outcome = attempts[-1]["outcome"] if attempts else ("failed" if attempted else "not_configured")
    return {"text": None, "provider": "local", "model": "codementor-heuristic-v2",
            "error": "; ".join(errors) or "No AI key configured", "provider_attempts": attempts,
            "response_status": final_outcome, "request_timestamp": request_timestamp,
            "requested_feature": requested_feature, "configured_provider": order[0] if order else "local",
            "final_provider": "local", "retry_count": sum(max(0, item["attempt"] - 1) for item in attempts),
            "duration_ms": duration_ms}

def call_live_ai(prompt: str, system_instruction: str = "", requested_feature: str = "general") -> Optional[str]:
    """Compatibility wrapper retained for existing tutoring call sites."""
    return generate_ai(prompt, system_instruction, requested_feature).get("text")

def _local_solution_line_note(source_line: str) -> str:
    """Produce cautious syntax-level guidance when no contextual AI walkthrough is available."""
    import re
    line = source_line.strip()
    if not line:
        return "Blank line used to separate nearby code for readability."
    if line.startswith(("#", "//", "/*", "*", "*/")):
        return "Comment for readers; it does not change program execution."
    if line.startswith(("#include", "import ", "from ", "using ")):
        return "Makes a library or module available to the program."
    if re.match(r"(public\s+)?(class|def|function|void|int|float|double|string)\b", line):
        return "Declares a class or function that groups related program behavior."
    if line.startswith(("if ", "if(", "else", "elif ")):
        return "Chooses a branch based on a condition; check which inputs make it true."
    if line.startswith(("for ", "for(", "while ", "while(")):
        return "Repeats work; compare its start, stopping condition, and updates with valid inputs."
    if line.startswith("return"):
        return "Returns a result to the caller and may end the current function."
    if any(token in line for token in ("print(", "printf(", "System.out", "console.log")):
        return "Displays a value so the program's result can be observed."
    assignment = re.match(r"([A-Za-z_]\w*)\s*(?:=|\+=|-=)", line)
    if assignment:
        return f"Stores or updates the value of `{assignment.group(1)}` used by later statements."
    if line in ("{", "}", "};"):
        return "Marks the start or end of a code block."
    return "Executes as part of the verified program; trace its inputs and effect on the result."

def _solution_walkthrough(attempt: Dict[str, Any], code: str) -> List[Dict[str, Any]]:
    """Explain each non-empty solution line with contextual AI and a safe local fallback."""
    lines = [(index, line) for index, line in enumerate(str(code or "").splitlines(), 1) if line.strip()]
    if not lines:
        return []
    if len(lines) > 200:
        return [{"line": index, "code": line, "explanation": _local_solution_line_note(line)} for index, line in lines]
    tests = _saved_test_results(attempt)
    prompt_context = {"problem": attempt.get("description") or "", "language": attempt.get("language") or "text",
        "original_code": str(attempt.get("initial_code") or "")[:12000], "verified_solution": code[:12000],
        "saved_test_results": tests[:12], "source_lines": [{"line": index, "code": line} for index, line in lines]}
    result = generate_ai("Return JSON only: {\"lines\":[{\"line\":1,\"explanation\":\"...\"}]}. Explain every supplied non-empty line in order for a beginner, using the problem and verified tests. Do not rewrite the code, add lines, or claim evidence not present. Keep each explanation under 35 words. Context: " + json.dumps(prompt_context, ensure_ascii=False),
        "You are a careful programming tutor. Explain the purpose of each supplied source line in context. Return only the requested JSON.",
        "solution_walkthrough")
    try:
        raw = result.get("text") or ""
        parsed = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        entries = parsed.get("lines")
        if not isinstance(entries, list) or len(entries) != len(lines):
            raise ValueError("walkthrough_length")
        validated = []
        for expected, entry in zip(lines, entries):
            explanation = entry.get("explanation") if isinstance(entry, dict) else None
            if (entry.get("line") != expected[0] or not isinstance(explanation, str)
                    or not explanation.strip() or len(explanation) > 280 or "```" in explanation):
                raise ValueError("walkthrough_schema")
            validated.append({"line": expected[0], "code": expected[1], "explanation": explanation.strip()})
        return validated
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return [{"line": index, "code": line, "explanation": _local_solution_line_note(line)} for index, line in lines]

def get_stage_content(attempt_id: int, target_stage: int = None, lang: str = "en") -> Dict[str, Any]:
    """
    Retrieves or advances the content for a specific pedagogical stage in an attempt.
    """
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT a.*, p.title, p.slug, p.concept, p.difficulty, p.description, p.buggy_code,
           p.solution_code, p.test_cases_json, p.stages_config_json, p.mistake_category_slug
    FROM attempts a
    LEFT JOIN problems p ON a.problem_id = p.id
    WHERE a.id = ?
    """, (attempt_id,))
    attempt = cursor.fetchone()

    if not attempt:
        conn.close()
        return {"error": "Attempt not found"}

    attempt = dict(attempt)
    cursor.execute("SELECT 1 FROM hint_logs WHERE attempt_id=? AND hint_type='mode' AND prompt_shown IN ('Mode: interview','Mode: competition') LIMIT 1", (attempt_id,))
    restricted_mode = bool(cursor.fetchone())
    mode_row = cursor.execute("SELECT prompt_shown FROM hint_logs WHERE attempt_id=? AND hint_type='mode' LIMIT 1", (attempt_id,)).fetchone()
    stage_mode = mode_row["prompt_shown"].replace("Mode: ", "") if mode_row else "practice"
    current_stage = target_stage or attempt["current_stage"]
    current_stage = max(1, min(8, current_stage))
    if current_stage == 3 and attempt["hints_used"] < 1:
        conn.close()
        return {"error": "Request the first hint before opening this step."}
    if current_stage == 4 and attempt["hints_used"] < 2:
        conn.close()
        return {"error": "Request the first hint before opening the stronger hint."}
    if restricted_mode and current_stage in (3, 4, 8):
        conn.close()
        return {"error": "Hints and solution reveal are disabled in this timed mode."}
    if current_stage == 7 and (not attempt["is_solved"] or attempt["verification_status"] != "verified"):
        conn.close()
        return {"error": "Pass code verification before opening Explanation Verification."}
    if current_stage == 8 and not attempt["is_completed"]:
        conn.close()
        return {"error": "Complete Explanation Verification before opening Stage 8: Answer."}

    if current_stage == 2:
        # Record that the learner opened the investigation step. This saved
        # evidence lets the solution unlock policy enforce the same order even
        # when a client calls the API directly.
        already_opened = cursor.execute("SELECT 1 FROM hint_logs WHERE attempt_id=? AND stage_number=2 AND hint_type='investigation' LIMIT 1", (attempt_id,)).fetchone()
        if not already_opened:
            cursor.execute("INSERT INTO hint_logs (attempt_id,stage_number,hint_type,prompt_shown,student_response,explanation_lang) VALUES (?,2,'investigation','Opened Stage 2: Investigate','',?)", (attempt_id, lang))
            conn.commit()

    # Parse stages config from problem or build default for custom code
    stages_config = {}
    if attempt.get("stages_config_json"):
        try:
            stages_config = json.loads(attempt["stages_config_json"])
        except Exception:
            stages_config = {}

    lang = "hinglish" if str(lang).lower() in ("hi", "hinglish") else "en"
    ai_status = get_ai_provider_status()

    stage_data = {
        "stage_number": current_stage,
        "stage_name": _get_stage_name(current_stage),
        "total_stages": 8,
        "language": lang,
        "programming_language": attempt.get("language", "python"),
        "ai_provider": "local",
        "ai_label": "Local learning curriculum",
        "is_ai_live": False,
        "hints_used": attempt["hints_used"],
        "is_solved": bool(attempt["is_solved"]),
        "solution_unlocked": bool(attempt["solution_unlocked"]),
        "is_completed": bool(attempt.get("is_completed", False)),
        "code_revision": int(attempt.get("code_revision", 0) or 0),
        "attempt_mode": stage_mode,
        "created_at": attempt.get("created_at"),
        "explain_evaluation": None,
        "current_code": attempt["current_code"],
        "problem_title": attempt.get("title") or attempt.get("custom_title") or "Custom Code Challenge",
        "concept": attempt.get("concept") or "Programming Logic",
        "mistake_category": attempt.get("mistake_category_slug") or "algorithm_logic"
    }
    conversation_rows = cursor.execute("""SELECT student_response, prompt_shown, hint_provider,
        hint_model, created_at FROM hint_logs WHERE attempt_id=? AND hint_type='debug_conversation'
        ORDER BY id DESC LIMIT 30""", (attempt_id,)).fetchall()
    stage_data["debug_conversation"] = [{"student": item["student_response"], "tutor": item["prompt_shown"],
        "provider": item["hint_provider"], "model": item["hint_model"], "created_at": item["created_at"]}
        for item in reversed(conversation_rows)]
    saved_fix = cursor.execute("SELECT student_response,context_json,created_at FROM hint_logs WHERE attempt_id=? AND hint_type='explain_fix' ORDER BY id DESC LIMIT 1", (attempt_id,)).fetchone()
    if saved_fix:
        try:
            fix_evaluation = json.loads(saved_fix["context_json"] or "{}")
        except (TypeError, ValueError):
            fix_evaluation = {}
        if fix_evaluation.get("evaluation_status"):
            stage_data["explain_evaluation"] = {**fix_evaluation, "student_explanation": saved_fix["student_response"], "created_at": saved_fix["created_at"]}
    saved_results = _saved_test_results(attempt)
    failed_results = [item for item in saved_results if item.get("passed") is False]
    latest_errors = _saved_execution_errors(attempt)
    diagnosis = analyzer.classify_mistakes(
        attempt.get("current_code", ""), attempt.get("language", "python"),
        error_message="\n".join(filter(None, [attempt.get("description", ""), _student_error_context(attempt_id), latest_errors])),
        compiler_error=latest_errors,
        test_results=failed_results,
        expected_category=attempt.get("mistake_category_slug")
    )
    stage_data["diagnostics"] = diagnosis.get("diagnostics", [])
    stage_data["diagnosis_confidence"] = diagnosis.get("overall_confidence", 0)
    primary_diagnostic = (stage_data["diagnostics"] or [{"category": stage_data["mistake_category"], "confidence": 0.25,
        "evidence": "No strong code or runtime evidence was found."}])[0]
    hint_level = 1 if current_stage == 3 else attempt["hints_used"] if current_stage == 4 else None
    hint_log_id = None
    if hint_level:
        saved_hint = cursor.execute("""
            SELECT id, prompt_shown, hint_provider, hint_model FROM hint_logs
            WHERE attempt_id=? AND hint_type='hint_request' AND hint_level=? AND explanation_lang=?
        """, (attempt_id, hint_level, lang)).fetchone()
        if saved_hint and saved_hint["prompt_shown"] != "__PENDING_HINT__":
            provider = saved_hint["hint_provider"] or "local"
            stage_data.update({"hint_level": hint_level, "hint": saved_hint["prompt_shown"],
                "ai_provider": provider, "ai_model": saved_hint["hint_model"] or "unknown",
                "ai_label": f"{_provider_label(provider)} hint (saved)", "is_ai_live": provider in ("openai", "groq"),
                "instruction": f"Step {3 if hint_level == 1 else 4}: Hint {hint_level} of 4"})
            conn.close()
            return stage_data
        if saved_hint:
            hint_log_id = saved_hint["id"]
        else:
            # Changing language may render the same earned level once more;
            # this does not increment progress and is unique per level/language.
            cursor.execute("""
                INSERT OR IGNORE INTO hint_logs
                (attempt_id, stage_number, hint_type, hint_level, prompt_shown, student_response, explanation_lang)
                VALUES (?, ?, 'hint_request', ?, '__PENDING_HINT__', '', ?)
            """, (attempt_id, current_stage, hint_level, lang))
            row = cursor.execute("""
                SELECT id FROM hint_logs WHERE attempt_id=? AND hint_type='hint_request'
                AND hint_level=? AND explanation_lang=?
            """, (attempt_id, hint_level, lang)).fetchone()
            hint_log_id = row["id"]
            conn.commit()

    # Populate stage-specific content
    if current_stage == 1:
        # Stage 1: Understand
        cfg = stages_config.get("stage1_understand", {})
        prompt_text = "Automated code diagnosis is ready. Review the evidence, then request a hint when you are ready." if lang == "en" else "Code ka automatic diagnosis tayyar hai. Evidence dekho; tayyar ho to hint lo."
        stage_data.update({
            "instruction": "Step 1: Review the Diagnosis",
            "prompt": prompt_text,
            "action_text": "Get First Hint"
        })

    elif current_stage == 2:
        # Stage 2: Investigate
        cfg = stages_config.get("stage2_investigate", {})
        prompt_text = "Review the detected evidence and likely concept. You do not need to guess the bug; the diagnosis has already analyzed the submitted code." if lang == "en" else "Detected evidence aur likely concept dekho. Bug guess karna zaroori nahi—submitted code ka analysis ho chuka hai."
        stage_data.update({
            "instruction": "Step 2: Review Evidence",
            "prompt": prompt_text,
            "action_text": "Get First Hint"
        })

    elif current_stage == 3:
        # Level 1 stays conceptual and must not reveal a known starter's exact
        # correction before the learner has investigated the code.
        fallback = _local_hint(attempt, primary_diagnostic, 1, lang)
        hint_text, hint_provider, hint_model = _generate_hint(attempt, primary_diagnostic, 1, lang, fallback)
        stage_data.update(ai_provider=hint_provider, ai_model=hint_model,
            ai_label=f"{_provider_label(hint_provider)} hint" if hint_provider != "local" else "Evidence-based local hint",
            is_ai_live=hint_provider in ("openai", "groq"), hint_level=1)
        stage_data.update({
            "instruction": "Step 3: First Clue (Gentle Hint)",
            "hint": hint_text,
            "action_text": "I Understand, Let Me Attempt"
        })

    elif current_stage == 4:
        # Levels 2–4 share the existing stronger-hint step but become
        # progressively more precise; history is included to avoid repeats.
        hint_level = max(2, min(4, int(attempt.get("hints_used", 2))))
        category = primary_diagnostic.get("category", stage_data["mistake_category"])
        fallback = _local_hint(attempt, primary_diagnostic, hint_level, lang)
        hint_text, hint_provider, hint_model = _generate_hint(attempt, primary_diagnostic, hint_level, lang, fallback)
        stage_data.update(ai_provider=hint_provider, ai_model=hint_model,
            ai_label=f"{_provider_label(hint_provider)} hint" if hint_provider != "local" else "Evidence-based local hint",
            is_ai_live=hint_provider in ("openai", "groq"), hint_level=hint_level)
        stage_data.update({
            "instruction": f"Step 4: Targeted Clue (Hint {hint_level} of 4)",
            "hint": hint_text,
            "action_text": "Proceed to Code Attempt"
        })

    elif current_stage == 5:
        # Stage 5: Attempt
        stage_data.update({
            "instruction": "Step 5: Write Your Correction",
            "prompt": "Apply your fix in the code editor on the right and click 'Verify Correction' to test your solution against our automated test suite.",
            "action_text": "Run & Verify Code"
        })

    elif current_stage == 6:
        # Stage 6: Verification
        stage_data.update({
            "instruction": "Step 6: Test Suite Verification",
            "verification_status": attempt["verification_status"],
            "last_test_results": json.loads(attempt["last_test_results_json"]) if attempt.get("last_test_results_json") else None
        })

    elif current_stage == 7:
        # Stage 7: Explanation
        cfg = stages_config.get("explanation", {})
        saved_explanation = cursor.execute("SELECT student_response,context_json,created_at FROM hint_logs WHERE attempt_id=? AND hint_type='explain_fix' ORDER BY id DESC LIMIT 1", (attempt_id,)).fetchone()
        if saved_explanation:
            try:
                saved_evaluation = json.loads(saved_explanation["context_json"] or "{}")
            except (TypeError, ValueError):
                saved_evaluation = {}
            if saved_evaluation.get("evaluation_status"):
                stage_data["explain_evaluation"] = {**saved_evaluation,
                    "student_explanation": saved_explanation["student_response"],
                    "created_at": saved_explanation["created_at"]}
        expl_text = cfg.get(lang) or cfg.get("en") or (
            "Bahut badiya! Samajhiye ki yeh fix kyun kaam karta hai aur is concept ko yaad rakhiye."
            if lang == "hinglish" else
            "Well done! Here is the conceptual breakdown of why this solution is correct."
        )
        ai_result = generate_ai(
            prompt=f"Language: {lang}. Concept: {stage_data['concept']}. Bug category: {stage_data['mistake_category']}. Existing concept note: {expl_text}. Explain the submitted fix in simple beginner-friendly language, why the old code failed, one edge case, and one takeaway. Student code:\n{attempt.get('current_code', '')}",
            system_instruction="You are CodeMentor, a careful beginner programming tutor. Be specific and concise. Never claim to have executed code.",
            requested_feature="solution_explanation"
        )
        if ai_result["text"]:
            expl_text = ai_result["text"]
            stage_data.update(ai_provider=ai_result["provider"], ai_model=ai_result["model"], ai_label=f"{_provider_label(ai_result['provider'])} response", is_ai_live=True)
        else:
            stage_data.update(ai_provider="local", ai_model=ai_result["model"], ai_label="Local curriculum response", is_ai_live=False)
        stage_data.update({
            "instruction": "Step 7: Conceptual Explanation",
            "explanation": expl_text,
            "takeaway": "Always verify edge conditions and index boundaries when iterating over collections."
        })

    elif current_stage == 8:
        # Stage 8 is the answer and feedback stage; the solution is a separate
        # section and remains absent until the server records an explicit unlock.
        saved_feedback = stage_data.get("explain_evaluation") or {}
        stage_data.update({
            "instruction": "Step 8: Answer & Feedback",
            "answer_feedback": saved_feedback.get("feedback", "Your explanation has been verified."),
            "last_test_results": saved_results,
            "solution_code": (attempt.get("solution_code") or attempt.get("current_code") or "")
                if attempt.get("solution_unlocked") else None,
            "original_code": attempt.get("initial_code") if attempt.get("solution_unlocked") else None,
            "action_text": "Unlock Complete Solution" if not attempt.get("solution_unlocked") else "Solution Unlocked"
        })
        if attempt.get("solution_unlocked"):
            unlock_row = cursor.execute("SELECT id,context_json FROM hint_logs WHERE attempt_id=? AND hint_type='solution_unlock' ORDER BY id DESC LIMIT 1", (attempt_id,)).fetchone()
            unlock_context = {}
            if unlock_row:
                try:
                    unlock_context = json.loads(unlock_row["context_json"] or "{}")
                except (TypeError, ValueError):
                    unlock_context = {}
            walkthrough = unlock_context.get("walkthrough")
            if not isinstance(walkthrough, list):
                walkthrough = _solution_walkthrough(attempt, stage_data["solution_code"] or "")
                if unlock_row:
                    unlock_context["walkthrough"] = walkthrough
                    cursor.execute("UPDATE hint_logs SET context_json=? WHERE id=?", (json.dumps(unlock_context, ensure_ascii=False), unlock_row["id"]))
                    conn.commit()
            stage_data["solution_walkthrough"] = walkthrough

    if hint_log_id and current_stage in (3, 4) and stage_data.get("hint"):
        cursor.execute("""
            UPDATE hint_logs SET prompt_shown=?, hint_provider=?, hint_model=?
            WHERE id=? AND prompt_shown='__PENDING_HINT__'
        """, (stage_data["hint"][:2000], stage_data.get("ai_provider", "local"),
              stage_data.get("ai_model", "unknown"), hint_log_id))
        conn.commit()

    # Log interaction if changing stage
    if target_stage and target_stage != attempt["current_stage"]:
        cursor.execute("""
        UPDATE attempts
        SET current_stage = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (current_stage, attempt_id))
        conn.commit()

    conn.close()
    return stage_data

def interact_stage(attempt_id: int, user_input: str, action: str, lang: str = "en", code_snapshot: str = None) -> Dict[str, Any]:
    """
    Handles student actions within a stage (submitting predictions, asking for stronger hints,
    skipping, or unlocking).
    """
    conn = database.get_connection()
    cursor = conn.cursor()
    if action in ("request_hint", "debug_reply"):
        # Serialize hint reservations so rapid clicks cannot issue duplicate
        # levels or advance past a level.
        conn.execute("BEGIN IMMEDIATE")

    cursor.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
    attempt = cursor.fetchone()
    if not attempt:
        conn.close()
        return {"error": "Attempt not found"}

    attempt = dict(attempt)
    curr_stage = attempt["current_stage"]
    hints_used = attempt["hints_used"]
    cursor.execute("SELECT prompt_shown FROM hint_logs WHERE attempt_id=? AND hint_type='mode' LIMIT 1", (attempt_id,))
    mode_row = cursor.fetchone()
    restricted_mode = bool(mode_row and mode_row["prompt_shown"] in ("Mode: interview", "Mode: competition"))
    if restricted_mode and action in ("request_hint", "unlock_solution", "debug_reply"):
        conn.close()
        return {"error": "Hints and solution reveal are disabled in this timed mode."}

    if action == "debug_reply":
        conn.close()
        return _debugging_tutor_turn(attempt_id, user_input, lang, code_snapshot)

    # Action Handlers:
    if action == "request_hint":
        last_saved_level = cursor.execute("SELECT COALESCE(MAX(hint_level),0) FROM hint_logs WHERE attempt_id=? AND hint_type='hint_request'", (attempt_id,)).fetchone()[0]
        next_level = max(hints_used, last_saved_level) + 1
        if curr_stage not in (1, 2, 3, 4, 5, 6) or next_level > 4:
            conn.close()
            return {"error": "The hint ladder is complete. Apply your fix or review the evidence."}
        next_stage = 3 if next_level == 1 else 4
        cursor.execute("""
        UPDATE attempts
        SET current_stage = ?, hints_used = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (next_stage, next_level, attempt_id))

        cursor.execute("""
        INSERT INTO hint_logs (attempt_id, stage_number, hint_type, hint_level, prompt_shown, student_response, explanation_lang)
        VALUES (?, ?, 'hint_request', ?, '__PENDING_HINT__', ?, ?)
        """, (attempt_id, next_stage, next_level, user_input, lang))
        conn.commit()
        conn.close()
        return get_stage_content(attempt_id, next_stage, lang)

    elif action == "submit_prediction":
        # Advance from stage 1 to stage 2
        cursor.execute("""
        UPDATE attempts
        SET current_stage = 2, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (attempt_id,))
        cursor.execute("""
        INSERT INTO hint_logs (attempt_id, stage_number, hint_type, prompt_shown, student_response, explanation_lang)
        VALUES (?, 1, 'prediction', 'Submitted output prediction', ?, ?)
        """, (attempt_id, user_input, lang))
        conn.commit()
        conn.close()
        return get_stage_content(attempt_id, 2, lang)

    elif action == "submit_investigation":
        # Timed modes skip the hint ladder and move directly to the attempt.
        next_stage = 5 if restricted_mode else 3
        cursor.execute("""
        UPDATE attempts
        SET current_stage = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (next_stage, attempt_id))
        cursor.execute("""
        INSERT INTO hint_logs (attempt_id, stage_number, hint_type, prompt_shown, student_response, explanation_lang)
        VALUES (?, 2, 'investigation', 'Submitted suspicious line investigation', ?, ?)
        """, (attempt_id, user_input, lang))
        conn.commit()
        conn.close()
        return get_stage_content(attempt_id, next_stage, lang)

    elif action == "proceed_to_attempt":
        # Advance directly to stage 5 (Attempt)
        cursor.execute("""
        UPDATE attempts
        SET current_stage = 5, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (attempt_id,))
        conn.commit()
        conn.close()
        return get_stage_content(attempt_id, 5, lang)

    elif action == "unlock_solution":
        if not attempt["is_completed"] or not attempt["is_solved"] or attempt["verification_status"] != "verified":
            conn.close()
            return {"error": "Verify your code and explanation in Stages 6 and 7 before unlocking the complete solution."}
        stage2_seen = cursor.execute("SELECT 1 FROM hint_logs WHERE attempt_id=? AND stage_number=2 AND hint_type IN ('investigation','prediction') LIMIT 1", (attempt_id,)).fetchone()
        first_hint_seen = cursor.execute("SELECT 1 FROM hint_logs WHERE attempt_id=? AND hint_type='hint_request' AND hint_level=1 AND prompt_shown!='__PENDING_HINT__' LIMIT 1", (attempt_id,)).fetchone()
        if not stage2_seen or not first_hint_seen:
            conn.close()
            return {"error": "Complete Stage 2: Investigate and acknowledge Stage 3: First Hint before unlocking the complete solution."}
        verified_run = cursor.execute("SELECT 1 FROM hint_logs WHERE attempt_id=? AND stage_number=6 AND hint_type='verification' AND prompt_shown='Passed' LIMIT 1", (attempt_id,)).fetchone()
        if not verified_run:
            conn.close()
            return {"error": "Complete the saved code verification in Stage 6 before unlocking the complete solution."}
        cursor.execute("""
        UPDATE attempts
        SET current_stage = 8, solution_unlocked = 1, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """, (attempt_id,))
        cursor.execute("""
        INSERT INTO hint_logs (attempt_id, stage_number, hint_type, prompt_shown, student_response, explanation_lang)
        VALUES (?, 8, 'solution_unlock', 'Student unlocked full solution', ?, ?)
        """, (attempt_id, user_input, lang))
        conn.commit()
        conn.close()
        return get_stage_content(attempt_id, 8, lang)

    conn.close()
    return get_stage_content(attempt_id, curr_stage, lang)

def verify_student_code(attempt_id: int, code: str, lang: str = "en", code_revision: int = None) -> Dict[str, Any]:
    """
    Compiles and executes student code against problem test cases.
    Updates the attempt record and Debugging DNA with verified results.
    """
    conn = database.get_connection()
    cursor = conn.cursor()

    cursor.execute("""
    SELECT a.*, p.language as prob_lang, p.test_cases_json, p.mistake_category_slug
    FROM attempts a
    LEFT JOIN problems p ON a.problem_id = p.id
    WHERE a.id = ?
    """, (attempt_id,))
    attempt = cursor.fetchone()

    if not attempt:
        conn.close()
        return {"error": "Attempt not found"}

    attempt = dict(attempt)
    language = attempt.get("prob_lang") or attempt.get("language") or "python"
    if code_revision is not None and int(code_revision) < int(attempt.get("code_revision", 0) or 0):
        conn.close()
        return {"error": "The editor has a newer saved draft. Run & Verify the latest code."}

    cursor.execute("SELECT prompt_shown FROM hint_logs WHERE attempt_id=? AND hint_type='mode' LIMIT 1", (attempt_id,))
    mode_row = cursor.fetchone()
    mode = mode_row["prompt_shown"].replace("Mode: ", "") if mode_row else "practice"
    if mode in ("interview", "competition"):
        limit_seconds = 20 * 60 if mode == "interview" else 5 * 60
        age = cursor.execute("SELECT CAST(strftime('%s','now') - strftime('%s', ?) AS INTEGER)", (attempt["created_at"],)).fetchone()[0]
        if age is not None and age > limit_seconds:
            conn.close()
            return {"error": "The timed challenge has expired. Start a new attempt to try again.", "mode_expired": True}

    test_cases = []
    if attempt.get("test_cases_json"):
        try:
            test_cases = json.loads(attempt["test_cases_json"])
        except Exception:
            test_cases = []

    # Run code in sandbox
    exec_result = code_executor.execute_code(code, language, test_cases)
    is_solved = bool(exec_result["all_passed"])
    status = "verified" if is_solved else "failed"

    latest_revision = cursor.execute("SELECT code_revision FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if latest_revision and code_revision is not None and int(latest_revision["code_revision"]) > int(code_revision):
        conn.close()
        return {"error": "The editor changed while verification was running. Run & Verify the latest code."}

    # Solved independently if solved with 0 or 1 hints and without unlocking solution
    solved_independently = bool(is_solved and attempt["hints_used"] <= 1 and not attempt["solution_unlocked"])
    # Stage 6 is the verification result screen for both passing and failing
    # runs. A passing run enables Stage 7, but does not silently skip Stage 6.
    next_stage = 6
    if code_revision is None:
        code_revision = int(attempt.get("code_revision", 0) or 0) + 1

    cursor.execute("""
    UPDATE attempts
    SET current_code = CASE WHEN code_revision <= ? THEN ? ELSE current_code END,
        code_revision = MAX(code_revision, ?),
        verified_revision = MAX(verified_revision, ?),
        current_stage = ?,
        is_solved = ?,
        solved_independently = ?,
        verification_status = ?,
        last_test_results_json = ?,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = ?
    """, (
        code_revision,
        code,
        code_revision,
        code_revision,
        next_stage,
        1 if is_solved else 0,
        1 if solved_independently else 0,
        status,
        json.dumps(exec_result),
        attempt_id
    ))
    cursor.execute("INSERT INTO hint_logs (attempt_id, stage_number, hint_type, prompt_shown, student_response, explanation_lang) VALUES (?, ?, 'verification', ?, ?, ?)",
        (attempt_id, next_stage, "Passed" if is_solved else "Failed", json.dumps({"code": code, "execution_result": exec_result, "is_solved": is_solved}), lang))
    conn.commit()
    conn.close()

    stage_content = get_stage_content(attempt_id, next_stage, lang)
    return {
        "execution_result": exec_result,
        "is_solved": is_solved,
        "solved_independently": solved_independently,
        "next_stage_data": stage_content
    }

def _get_stage_name(stage: int) -> str:
    names = {
        1: "Understand",
        2: "Investigate",
        3: "First Hint",
        4: "Stronger Hint",
        5: "Code Attempt",
        6: "Verification",
        7: "Explanation",
        8: "Answer"
    }
    return names.get(stage, f"Stage {stage}")

if __name__ == "__main__":
    print("Testing Tutor Engine...")
    print("AI Provider Status:", get_ai_provider_status())
    content = get_stage_content(1, target_stage=1, lang="en")
    print("Stage 1 English:", content["instruction"], content["prompt"])
    content_hi = get_stage_content(1, target_stage=1, lang="hinglish")
    print("Stage 1 Hinglish:", content_hi["prompt"])
