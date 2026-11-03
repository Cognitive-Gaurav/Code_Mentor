"""
CodeMentor - Core REST API & Web Application Server
Production-ready Python HTTP server with zero third-party dependencies,
serving the modern single-page educational application and all REST API endpoints.
"""

import os
import sys
import json
import ast
import io
import csv
import hashlib
import secrets
import logging
import time
import re
import difflib
from http.cookies import SimpleCookie
import urllib.parse
import uuid
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Optional
import mimetypes
import tokenize
import sqlite3
from collections import defaultdict
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
import traceback

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

import database
import code_executor
import analyzer
import tutor_engine
import feature5

# Bind to 0.0.0.0 on Render / Linux to allow cloud proxy routing; use 127.0.0.1 on Windows local
DEFAULT_HOST = "0.0.0.0" if (sys.platform != "win32" or os.environ.get("RENDER")) else "127.0.0.1"
HOST = os.environ.get("HOST", DEFAULT_HOST)
PORT = int(os.environ.get("PORT", 8000))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ALLOWED_STATIC_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".mjs", ".json",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
    ".woff", ".woff2", ".ttf", ".eot", ".map"
}
SESSION_COOKIE = "codementor_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_LIMIT = 10
LOGIN_WINDOW_SECONDS = 15 * 60
_login_failures = {}


def _assess_fix_explanation(category, explanation):
    """Grade conceptual understanding with a transparent local rubric."""
    text = " ".join(str(explanation or "").lower().split())
    words = re.findall(r"[a-z0-9']+", text)
    terms_by_category = {
        "array_indexing": ["array", "index", "bound", "length", "size", "range", "position"],
        "off_by_one": ["loop", "boundary", "range", "endpoint", "index", "inclusive", "exclusive"],
        "loop_logic": ["loop", "condition", "increment", "decrement", "iteration", "stop", "terminat"],
        "recursion_misunderstanding": ["recursion", "recursive", "base case", "return", "stop", "terminat"],
        "incorrect_conditions": ["condition", "comparison", "true", "false", "operator", "even", "odd"],
        "syntax_errors": ["syntax", "semicolon", "quote", "bracket", "parenthes", "statement"],
        "variable_scope": ["scope", "declare", "initialize", "variable", "value"],
        "algorithm_logic": ["logic", "algorithm", "calculation", "accumulator", "value", "input", "output"],
    }
    terms = terms_by_category.get(category, terms_by_category["algorithm_logic"])
    matched = [term for term in terms if term in text]
    causal = bool(re.search(r"\b(because|so|therefore|which|prevents?|causes?|means|otherwise|to avoid|as a result|stops?)\b", text))
    test_reasoning = bool(re.search(r"\b(edge case|test|input|output|example|boundary|when)\b", text))
    score = min(50, len(matched) * 25) + (35 if causal else 0) + (15 if test_reasoning else 0)
    if len(words) < 6 or len(text) < 30:
        status = "incomplete"
    elif len(matched) >= 2 and causal:
        status = "correct"
    elif matched or causal:
        status = "partially_correct"
    else:
        status = "incorrect"
    return {"status": status, "score": score, "matched_concepts": matched,
        "missing_concepts": [term for term in terms if term not in matched],
        "rubric": {"concept_evidence": min(50, len(matched) * 25),
            "bug_to_fix_reasoning": 35 if causal else 0,
            "test_or_edge_case_reasoning": 15 if test_reasoning else 0}}


def _pdf_escape(text):
    """Return safe printable text for the built-in Helvetica PDF font."""
    value = str(text or "").encode("cp1252", "replace").decode("cp1252")
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_text_pdf(title, lines):
    """Build a small, dependency-free, multipage PDF from verified text data."""
    import textwrap
    expanded = []
    for line in lines:
        expanded.extend(textwrap.wrap(str(line), width=92, replace_whitespace=False,
                                      drop_whitespace=True) or [""])
    pages = [expanded[i:i + 42] for i in range(0, len(expanded), 42)] or [[""]]
    page_ids = list(range(4, 4 + len(pages)))
    content_ids = list(range(4 + len(pages), 4 + (2 * len(pages))))
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Kids [" + " ".join(f"{n} 0 R" for n in page_ids) +
            f"] /Count {len(page_ids)} >>").encode("ascii"),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    for index, (page_lines, page_id, content_id) in enumerate(zip(pages, page_ids, content_ids)):
        commands = ["BT", "/F1 16 Tf", "50 752 Td", f"({_pdf_escape(title)}) Tj",
                    "/F1 10 Tf", "0 -28 Td"]
        start = 0
        for line in page_lines:
            commands.append(f"({_pdf_escape(line)}) Tj")
            commands.append("0 -16 Td")
            start += 1
        commands.append("ET")
        stream = "\n".join(commands).encode("cp1252", "replace")
        objects[page_id] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>").encode("ascii")
        objects[content_id] = b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id in range(1, len(objects) + 1):
        offsets.append(len(output))
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode("ascii"))
    return bytes(output)


def _leaderboard_csv(headers, rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _challenge_failure_is_valid(category, result):
    """A challenge is valid only when its intended failure is observable."""
    if not result or result.get("all_passed"):
        return False
    if category != "syntax_errors":
        return bool(result.get("is_compiled"))
    error_text = " ".join(filter(None, [str(result.get("compile_error") or ""),
        str(result.get("overall_error") or ""),
        " ".join(str(item.get("error") or "") for item in result.get("test_results", []))])).lower()
    return any(token in error_text for token in ("syntaxerror", "syntax error", "expected", "unclosed", "illegal start", "';'", "';' expected", "invalid syntax"))


def _challenge_bug_compiled(category, result):
    """Report parser failures consistently even when the Python runner's compile flag is broad."""
    if category == "syntax_errors" and _challenge_failure_is_valid(category, result):
        return False
    return bool(result and result.get("is_compiled"))


def _format_semicolon_challenge_code(code, language):
    """Put C and Java statements on separate lines without splitting headers or literals."""
    source = str(code or "")
    output = []
    index = 0
    paren_depth = 0
    quote = None
    line_comment = False
    block_comment = False
    text_block = False

    while index < len(source):
        char = source[index]
        next_two = source[index:index + 2]

        if line_comment:
            output.append(char)
            index += 1
            if char == "\n":
                line_comment = False
            continue
        if block_comment:
            output.append(char)
            if next_two == "*/":
                output.append("/")
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if text_block:
            if source[index:index + 3] == '"""':
                output.append('"""')
                index += 3
                text_block = False
            else:
                output.append(char)
                index += 1
            continue
        if quote:
            output.append(char)
            if char == "\\" and index + 1 < len(source):
                output.append(source[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue

        if next_two == "//":
            output.append(next_two)
            index += 2
            line_comment = True
            continue
        if next_two == "/*":
            output.append(next_two)
            index += 2
            block_comment = True
            continue
        if language == "java" and source[index:index + 3] == '"""':
            output.append('"""')
            index += 3
            text_block = True
            continue
        if char in ('"', "'"):
            quote = char
            output.append(char)
            index += 1
            continue
        if char == "(":
            paren_depth += 1
        elif char == ")":
            paren_depth = max(0, paren_depth - 1)

        output.append(char)
        index += 1
        is_c_preprocessor = language == "c" and source[source.rfind("\n", 0, index) + 1:index].lstrip().startswith("#")
        if char == ";" and paren_depth == 0 and not is_c_preprocessor:
            lookahead = index
            while lookahead < len(source) and source[lookahead] in (" ", "\t"):
                lookahead += 1
            if lookahead < len(source) and source[lookahead] not in ("\r", "\n"):
                output.append("\n")
                index = lookahead
    return "".join(output)


def _format_python_challenge_code(code):
    """Use Python's parser to expand compact valid code while preserving invalid syntax challenges."""
    source = str(code or "")
    try:
        tree = ast.parse(source)
        # AST formatting drops comments; retain authored comments rather than silently removing them.
        if not hasattr(ast, "unparse"):
            return source
        if any(token.type == tokenize.COMMENT
                for token in tokenize.generate_tokens(io.StringIO(source).readline)):
            return source
        formatted = ast.unparse(tree)
        return formatted + ("\n" if source.endswith(("\n", "\r")) and not formatted.endswith("\n") else "")
    except (SyntaxError, ValueError, TypeError):
        return source


def _local_bug_challenge(language, difficulty, category=None):
    """Small known-good references and intentional bugs for honest offline fallback."""
    category = {"logical_errors": "incorrect_conditions", "loop_boundary": "off_by_one"}.get(category, category)
    extra_specs = {
        ("syntax_errors", "python"): ("Missing Colon", "Syntax Errors", "Print Positive for numbers above zero, otherwise Non-positive.",
            "number = int(input())\nif number > 0\n    print('Positive')\nelse:\n    print('Non-positive')\n",
            "number = int(input())\nif number > 0:\n    print('Positive')\nelse:\n    print('Non-positive')\n", [{"input":"1\n","expected_output":"Positive"},{"input":"0\n","expected_output":"Non-positive"}]),
        ("syntax_errors", "c"): ("Missing Semicolon", "Syntax Errors", "Print the number 7. The current statement has a syntax error.",
            "#include <stdio.h>\nint main(void) { printf(\"7\\n\") return 0; }\n",
            "#include <stdio.h>\nint main(void) { printf(\"7\\n\"); return 0; }\n", [{"input":"","expected_output":"7"}]),
        ("syntax_errors", "java"): ("Missing Semicolon", "Syntax Errors", "Print the number 7. The current statement has a syntax error.",
            "public class Main { public static void main(String[] args) { System.out.println(7) } }\n",
            "public class Main { public static void main(String[] args) { System.out.println(7); } }\n", [{"input":"","expected_output":"7"}]),
        ("array_indexing", "python"): ("Array Index", "Array Index Errors", "Read a zero-based index and print that item from [10, 20, 30].",
            "values = [10, 20, 30]\nindex = int(input())\nprint(values[index + 1])\n",
            "values = [10, 20, 30]\nindex = int(input())\nprint(values[index])\n", [{"input":"0\n","expected_output":"10"},{"input":"1\n","expected_output":"20"}]),
        ("array_indexing", "c"): ("Array Index", "Array Index Errors", "Read a zero-based index and print that item from [10, 20, 30].",
            "#include <stdio.h>\nint main(void) { int values[3] = {10,20,30}, index; if (scanf(\"%d\", &index) != 1) return 1; printf(\"%d\\n\", values[index + 1]); return 0; }\n",
            "#include <stdio.h>\nint main(void) { int values[3] = {10,20,30}, index; if (scanf(\"%d\", &index) != 1) return 1; printf(\"%d\\n\", values[index]); return 0; }\n", [{"input":"0\n","expected_output":"10"},{"input":"1\n","expected_output":"20"}]),
        ("array_indexing", "java"): ("Array Index", "Array Index Errors", "Read a zero-based index and print that item from [10, 20, 30].",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { int[] values = {10,20,30}; Scanner s = new Scanner(System.in); int index = s.nextInt(); System.out.println(values[index + 1]); } }\n",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { int[] values = {10,20,30}; Scanner s = new Scanner(System.in); int index = s.nextInt(); System.out.println(values[index]); } }\n", [{"input":"0\n","expected_output":"10"},{"input":"1\n","expected_output":"20"}]),
        ("null_pointer_errors", "python"): ("Uninitialized Text", "Null References", "Read a word and print it in uppercase. The current reference has no text value.",
            "word = None\nprint(word.upper())\n", "word = input().strip()\nprint(word.upper())\n", [{"input":"code\n","expected_output":"CODE"}]),
        ("null_pointer_errors", "java"): ("Uninitialized Text", "Null References", "Read a word and print it in uppercase. The current reference has no text value.",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { String word = null; System.out.println(word.toUpperCase()); } }\n",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner s = new Scanner(System.in); String word = s.nextLine(); System.out.println(word.toUpperCase()); } }\n", [{"input":"code\n","expected_output":"CODE"}]),
        ("null_pointer_errors", "c"): ("Uninitialized Text", "Null References", "Print the first letter of the word 'code' in uppercase. The current pointer has no text value.",
            "#include <stdio.h>\nint main(void) { char *word = NULL; printf(\"%c\\n\", word[0]); return 0; }\n",
            "#include <stdio.h>\nint main(void) { char word[] = \"code\"; printf(\"C\\n\"); return 0; }\n", [{"input":"","expected_output":"C"}]),
        ("string_handling_errors", "python"): ("Normalize a Word", "String Handling", "Read a word and print it in lowercase. Mixed-case input should be normalized.",
            "word = input().strip()\nprint(word.upper())\n", "word = input().strip()\nprint(word.lower())\n", [{"input":"HeLLo\n","expected_output":"hello"},{"input":"CoDe\n","expected_output":"code"}]),
        ("string_handling_errors", "java"): ("Normalize a Word", "String Handling", "Read a word and print it in lowercase. Mixed-case input should be normalized.",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner s = new Scanner(System.in); System.out.println(s.nextLine().toUpperCase()); } }\n",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner s = new Scanner(System.in); System.out.println(s.nextLine().toLowerCase()); } }\n", [{"input":"HeLLo\n","expected_output":"hello"},{"input":"CoDe\n","expected_output":"code"}]),
        ("string_handling_errors", "c"): ("Normalize a Word", "String Handling", "Read a word and print it in lowercase. Mixed-case input should be normalized.",
            "#include <stdio.h>\n#include <ctype.h>\nint main(void) { char word[100]; if (scanf(\"%99s\", word) != 1) return 1; for (int i=0; word[i]; i++) word[i] = (char)toupper((unsigned char)word[i]); printf(\"%s\\n\", word); return 0; }\n",
            "#include <stdio.h>\n#include <ctype.h>\nint main(void) { char word[100]; if (scanf(\"%99s\", word) != 1) return 1; for (int i=0; word[i]; i++) word[i] = (char)tolower((unsigned char)word[i]); printf(\"%s\\n\", word); return 0; }\n", [{"input":"HeLLo\n","expected_output":"hello"},{"input":"CoDe\n","expected_output":"code"}]),
        ("function_method_errors", "python"): ("Add Two Numbers", "Functions and Methods", "Read two integers and print their sum. The add function returns the wrong calculation.",
            "def add(a, b):\n    return a - b\na, b = map(int, input().split())\nprint(add(a, b))\n", "def add(a, b):\n    return a + b\na, b = map(int, input().split())\nprint(add(a, b))\n", [{"input":"4 5\n","expected_output":"9"},{"input":"2 3\n","expected_output":"5"}]),
        ("function_method_errors", "java"): ("Add Two Numbers", "Functions and Methods", "Read two integers and print their sum. The add method returns the wrong calculation.",
            "import java.util.Scanner;\npublic class Main { static int add(int a, int b) { return a - b; } public static void main(String[] args) { Scanner s = new Scanner(System.in); System.out.println(add(s.nextInt(), s.nextInt())); } }\n",
            "import java.util.Scanner;\npublic class Main { static int add(int a, int b) { return a + b; } public static void main(String[] args) { Scanner s = new Scanner(System.in); System.out.println(add(s.nextInt(), s.nextInt())); } }\n", [{"input":"4 5\n","expected_output":"9"},{"input":"2 3\n","expected_output":"5"}]),
        ("function_method_errors", "c"): ("Add Two Numbers", "Functions and Methods", "Read two integers and print their sum. The add function returns the wrong calculation.",
            "#include <stdio.h>\nint add(int a, int b) { return a - b; }\nint main(void) { int a,b; if (scanf(\"%d %d\", &a,&b) != 2) return 1; printf(\"%d\\n\", add(a,b)); return 0; }\n",
            "#include <stdio.h>\nint add(int a, int b) { return a + b; }\nint main(void) { int a,b; if (scanf(\"%d %d\", &a,&b) != 2) return 1; printf(\"%d\\n\", add(a,b)); return 0; }\n", [{"input":"4 5\n","expected_output":"9"},{"input":"2 3\n","expected_output":"5"}]),
        ("data_structure_errors", "python"): ("Queue First Item", "Data Structures", "A queue should remove items in first-in, first-out order. Print the first queued item.",
            "queue = [10, 20, 30]\nprint(queue.pop())\n", "queue = [10, 20, 30]\nprint(queue.pop(0))\n", [{"input":"","expected_output":"10"}]),
        ("data_structure_errors", "java"): ("Queue First Item", "Data Structures", "A queue should remove items in first-in, first-out order. Print the first queued item.",
            "import java.util.ArrayDeque;\npublic class Main { public static void main(String[] args) { ArrayDeque<Integer> queue = new ArrayDeque<>(); queue.add(10); queue.add(20); queue.add(30); System.out.println(queue.removeLast()); } }\n",
            "import java.util.ArrayDeque;\npublic class Main { public static void main(String[] args) { ArrayDeque<Integer> queue = new ArrayDeque<>(); queue.add(10); queue.add(20); queue.add(30); System.out.println(queue.removeFirst()); } }\n", [{"input":"","expected_output":"10"}]),
        ("data_structure_errors", "c"): ("Queue First Item", "Data Structures", "A queue should remove items in first-in, first-out order. Print the first queued item.",
            "#include <stdio.h>\nint main(void) { int queue[3] = {10,20,30}; printf(\"%d\\n\", queue[2]); return 0; }\n",
            "#include <stdio.h>\nint main(void) { int queue[3] = {10,20,30}; printf(\"%d\\n\", queue[0]); return 0; }\n", [{"input":"","expected_output":"10"}]),
    }
    if category and category not in ("incorrect_conditions", "off_by_one", "recursion_misunderstanding"):
        extra = extra_specs.get((category, language))
        if not extra:
            return None
        title, concept, description, buggy, solution, tests = extra
        return {"slug": f"local-bug-{language}-{difficulty.lower()}-{category}",
            "title": f"{difficulty} {title}", "language": language, "difficulty": difficulty,
            "concept": concept, "mistake_category_slug": category, "description": description,
            "buggy_code": buggy, "solution_code": solution, "test_cases": tests,
            "stages_config_json": "{}", "source": "local_curated",
            "expected_compile_failure": category == "syntax_errors"}
    specs = {
        ("python", "Easy"): ("Even or Odd", "incorrect_conditions", "Conditions",
            "Read one integer and print Even or Odd. The current condition misclassifies even numbers.",
            "n = int(input())\nprint('Even' if n % 2 == 1 else 'Odd')\n",
            "n = int(input())\nprint('Even' if n % 2 == 0 else 'Odd')\n",
            [{"input":"4\n","expected_output":"Even"},{"input":"5\n","expected_output":"Odd"}]),
        ("python", "Medium"): ("Sum from One to N", "off_by_one", "Loop boundaries",
            "Read N and print the sum of every integer from 1 through N. The loop stops too early.",
            "n = int(input())\ntotal = 0\nfor value in range(1, n):\n    total += value\nprint(total)\n",
            "n = int(input())\ntotal = 0\nfor value in range(1, n + 1):\n    total += value\nprint(total)\n",
            [{"input":"5\n","expected_output":"15"},{"input":"1\n","expected_output":"1"}]),
        ("python", "Hard"): ("Factorial Base Case", "recursion_misunderstanding", "Recursion",
            "Read a non-negative integer and print its factorial. The recursive base case is incorrect.",
            "def factorial(n):\n    if n == 0:\n        return 0\n    return n * factorial(n - 1)\nprint(factorial(int(input())))\n",
            "def factorial(n):\n    if n == 0:\n        return 1\n    return n * factorial(n - 1)\nprint(factorial(int(input())))\n",
            [{"input":"0\n","expected_output":"1"},{"input":"5\n","expected_output":"120"}]),
        ("c", "Easy"): ("Even or Odd", "incorrect_conditions", "Conditions",
            "Read one integer and print Even or Odd. The current condition misclassifies even numbers.",
            "#include <stdio.h>\nint main(void) { int n; if (scanf(\"%d\", &n) != 1) return 1; if (n % 2 == 1) { printf(\"Even\\n\"); } else { printf(\"Odd\\n\"); } return 0; }\n",
            "#include <stdio.h>\nint main(void) { int n; if (scanf(\"%d\", &n) != 1) return 1; if (n % 2 == 0) { printf(\"Even\\n\"); } else { printf(\"Odd\\n\"); } return 0; }\n",
            [{"input":"4\n","expected_output":"Even"},{"input":"5\n","expected_output":"Odd"}]),
        ("c", "Medium"): ("Sum from One to N", "off_by_one", "Loop boundaries",
            "Read N and print the sum of every integer from 1 through N. The loop stops too early.",
            "#include <stdio.h>\nint main(void) { int n, total = 0; if (scanf(\"%d\", &n) != 1) return 1; for (int i = 1; i < n; i++) total += i; printf(\"%d\\n\", total); return 0; }\n",
            "#include <stdio.h>\nint main(void) { int n, total = 0; if (scanf(\"%d\", &n) != 1) return 1; for (int i = 1; i <= n; i++) total += i; printf(\"%d\\n\", total); return 0; }\n",
            [{"input":"5\n","expected_output":"15"},{"input":"1\n","expected_output":"1"}]),
        ("c", "Hard"): ("Factorial Base Case", "recursion_misunderstanding", "Recursion",
            "Read a non-negative integer and print its factorial. The recursive base case is incorrect.",
            "#include <stdio.h>\nlong long factorial(int n) { if (n == 0) return 0; return n * factorial(n - 1); }\nint main(void) { int n; if (scanf(\"%d\", &n) != 1) return 1; printf(\"%lld\\n\", factorial(n)); return 0; }\n",
            "#include <stdio.h>\nlong long factorial(int n) { if (n == 0) return 1; return n * factorial(n - 1); }\nint main(void) { int n; if (scanf(\"%d\", &n) != 1) return 1; printf(\"%lld\\n\", factorial(n)); return 0; }\n",
            [{"input":"0\n","expected_output":"1"},{"input":"5\n","expected_output":"120"}]),
        ("java", "Easy"): ("Even or Odd", "incorrect_conditions", "Conditions",
            "Read one integer and print Even or Odd. The current condition misclassifies even numbers.",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner s = new Scanner(System.in); int n = s.nextInt(); System.out.println(n % 2 == 1 ? \"Even\" : \"Odd\"); } }\n",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner s = new Scanner(System.in); int n = s.nextInt(); System.out.println(n % 2 == 0 ? \"Even\" : \"Odd\"); } }\n",
            [{"input":"4\n","expected_output":"Even"},{"input":"5\n","expected_output":"Odd"}]),
        ("java", "Medium"): ("Sum from One to N", "off_by_one", "Loop boundaries",
            "Read N and print the sum of every integer from 1 through N. The loop stops too early.",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner s = new Scanner(System.in); int n = s.nextInt(), total = 0; for (int i = 1; i < n; i++) total += i; System.out.println(total); } }\n",
            "import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner s = new Scanner(System.in); int n = s.nextInt(), total = 0; for (int i = 1; i <= n; i++) total += i; System.out.println(total); } }\n",
            [{"input":"5\n","expected_output":"15"},{"input":"1\n","expected_output":"1"}]),
        ("java", "Hard"): ("Factorial Base Case", "recursion_misunderstanding", "Recursion",
            "Read a non-negative integer and print its factorial. The recursive base case is incorrect.",
            "import java.util.Scanner;\npublic class Main { static long factorial(int n) { if (n == 0) return 0; return n * factorial(n - 1); } public static void main(String[] args) { Scanner s = new Scanner(System.in); System.out.println(factorial(s.nextInt())); } }\n",
            "import java.util.Scanner;\npublic class Main { static long factorial(int n) { if (n == 0) return 1; return n * factorial(n - 1); } public static void main(String[] args) { Scanner s = new Scanner(System.in); System.out.println(factorial(s.nextInt())); } }\n",
            [{"input":"0\n","expected_output":"1"},{"input":"5\n","expected_output":"120"}]),
    }
    if category in ("incorrect_conditions", "off_by_one", "recursion_misunderstanding"):
        required_level = {"incorrect_conditions": "Easy", "off_by_one": "Medium", "recursion_misunderstanding": "Hard"}[category]
        spec = specs.get((language, required_level))
        if not spec:
            return None
        title, _, concept, description, buggy, solution, tests = spec
        return {"slug": f"local-bug-{language}-{difficulty.lower()}-{category}", "title": f"{difficulty} {title}",
            "language": language, "difficulty": difficulty, "concept": concept, "mistake_category_slug": category,
            "description": description, "buggy_code": buggy, "solution_code": solution,
            "test_cases": tests, "stages_config_json": "{}", "source": "local_curated",
            "expected_compile_failure": False}
    spec = specs.get((language, difficulty))
    if not spec:
        return None
    title, category, concept, description, buggy, solution, tests = spec
    return {"slug": f"local-bug-{language}-{difficulty.lower()}", "title": f"{difficulty} {title}",
        "language": language, "difficulty": difficulty, "concept": concept,
        "mistake_category_slug": category, "description": description,
        "buggy_code": buggy, "solution_code": solution, "test_cases": tests,
        "stages_config_json": "{}", "source": "local_curated",
        "expected_compile_failure": False}


def _format_feature11_12_challenge(code, language):
    return _format_python_challenge_code(code) if language == "python" else _format_semicolon_challenge_code(code, language)


_BUG_CHALLENGE_CATEGORIES = {
    "syntax_errors", "incorrect_conditions", "array_indexing", "off_by_one",
    "null_pointer_errors", "string_handling_errors", "function_method_errors",
    "data_structure_errors", "recursion_misunderstanding",
}
_CHALLENGE_GENERATION_ATTEMPTS = 3
_CHALLENGE_HISTORY_LIMIT = 24
_CODE_KEYWORDS = {
    "if", "else", "elif", "for", "while", "do", "switch", "case", "break", "continue",
    "return", "def", "class", "public", "private", "protected", "static", "void", "int",
    "long", "short", "float", "double", "char", "boolean", "bool", "string", "String",
    "true", "false", "True", "False", "null", "None", "NULL", "new", "import", "from",
    "include", "main", "try", "catch", "finally", "throw", "throws", "print", "printf",
    "System", "out", "println", "input", "range", "len", "pass", "and", "or", "not",
    "in", "is", "lambda", "const", "sizeof", "struct", "typedef", "enum", "static_cast",
}


def _challenge_code_tokens(code):
    """Return code tokens with user identifiers and literal values normalized."""
    token_re = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|\d+(?:\.\d+)?|==|!=|<=|>=|&&|\|\||\+\+|--|->|[^\s]')
    result = []
    for token in token_re.findall(str(code or "")):
        if token[:1] in ("'", '"'):
            result.append("LITERAL")
        elif re.fullmatch(r"\d+(?:\.\d+)?", token):
            result.append("NUMBER")
        elif re.fullmatch(r"[A-Za-z_]\w*", token) and token not in _CODE_KEYWORDS:
            result.append("IDENT")
        else:
            result.append(token)
    return result


def _challenge_bug_signature(buggy_code, solution_code, category):
    buggy = _challenge_code_tokens(buggy_code)
    solution = _challenge_code_tokens(solution_code)
    changes = []
    for tag, b_start, b_end, s_start, s_end in difflib.SequenceMatcher(a=buggy, b=solution).get_opcodes():
        if tag != "equal":
            changes.append((tag, tuple(buggy[b_start:b_end]), tuple(solution[s_start:s_end])))
    return (category, tuple(changes))


def _challenge_duplicate_reason(candidate, recent):
    title = str(candidate.get("title", ""))
    description = str(candidate.get("description", ""))
    category = candidate.get("category")
    statement = re.sub(r"[^a-z0-9]+", " ", f"{title} {description}".lower()).strip()
    code = re.sub(r"\s+", "", str(candidate.get("buggy_code", "")))
    shape = _challenge_code_tokens(candidate.get("buggy_code", ""))
    bug_signature = _challenge_bug_signature(candidate.get("buggy_code", ""), candidate.get("solution_code", ""), category)
    for item in recent:
        previous_statement = re.sub(r"[^a-z0-9]+", " ", f"{item.get('title', '')} {item.get('description', '')}".lower()).strip()
        if statement and previous_statement and difflib.SequenceMatcher(None, statement, previous_statement).ratio() >= 0.76:
            return "repeated_problem_statement"
        previous_code = re.sub(r"\s+", "", str(item.get("buggy_code", "")))
        if code and previous_code and code == previous_code:
            return "repeated_code"
        if category == item.get("category"):
            if bug_signature == item.get("bug_signature"):
                return "repeated_bug_pattern"
            if item.get("language") == candidate.get("language"):
                previous_shape = item.get("code_shape") or _challenge_code_tokens(item.get("buggy_code", ""))
                if shape and previous_shape and difflib.SequenceMatcher(None, shape, previous_shape).ratio() >= 0.94:
                    return "repeated_code_structure"
    return None


def _recent_bug_challenges(user_id):
    conn = database.get_connection()
    try:
        rows = conn.execute("""SELECT p.title,p.description,p.language,p.difficulty,
            p.mistake_category_slug AS category,p.buggy_code,p.solution_code
            FROM bug_challenge_progress b JOIN problems p ON p.id=b.challenge_id
            WHERE b.user_id=? ORDER BY b.created_at DESC,b.id DESC LIMIT ?""",
            (user_id, _CHALLENGE_HISTORY_LIMIT)).fetchall()
        recent = [dict(row) for row in rows]
    finally:
        conn.close()
    for item in recent:
        item["code_shape"] = _challenge_code_tokens(item.get("buggy_code", ""))
        item["bug_signature"] = _challenge_bug_signature(item.get("buggy_code", ""), item.get("solution_code", ""), item.get("category"))
    return recent


def _format_challenge_candidate(candidate, language):
    if language in ("java", "c"):
        candidate["buggy_code"] = _format_semicolon_challenge_code(candidate["buggy_code"], language)
        candidate["solution_code"] = _format_semicolon_challenge_code(candidate["solution_code"], language)
    else:
        candidate["buggy_code"] = _format_python_challenge_code(candidate["buggy_code"])
        candidate["solution_code"] = _format_python_challenge_code(candidate["solution_code"])
    return candidate


def _challenge_runtime_error_summary(error, limit=180):
    """Keep the actionable exception line, without forwarding traceback code/paths."""
    lines = [line.strip() for line in str(error or "").splitlines() if line.strip()]
    if not lines:
        return ""
    diagnostic = re.compile(
        r"^(?:[A-Za-z_][\w.$]*(?:Error|Exception|Fault|Failure)\s*:|"
        r"Exception in thread\b|Runtime Error\s*:|Runtime Exit Code\b|"
        r"terminate called\b|Segmentation fault\b|Killed\b|Execution timed out\b)",
        re.I,
    )
    useful_line = next((line for line in reversed(lines) if diagnostic.search(line)), lines[-1])
    # Error messages can include untrusted control characters. Keep a short,
    # single-line summary for the next AI retry prompt; never log this value.
    useful_line = re.sub(r"[\x00-\x1f\x7f]+", " ", useful_line).strip()
    return useful_line[:limit]


def _reference_test_failure_feedback(reference_result, test_cases, limit=4):
    """Return bounded, prompt-only diagnostics for rejected reference runs."""
    reference_result = reference_result if isinstance(reference_result, dict) else {}
    failed_cases = []
    results = reference_result.get("test_results")
    result_errors = [str(item.get("error") or "") for item in results
                     if isinstance(item, dict)] if isinstance(results, list) else []
    python_syntax_failure = any(re.search(r"\b(?:SyntaxError|IndentationError|TabError):", error)
                                for error in result_errors)
    compile_failure = (not reference_result.get("is_compiled") or bool(reference_result.get("compile_error"))
                       or python_syntax_failure)
    if isinstance(results, list):
        for index, result in enumerate(results):
            if not isinstance(result, dict) or result.get("passed") is True:
                continue
            test = test_cases[index] if index < len(test_cases) and isinstance(test_cases[index], dict) else {}
            item = {"case": index + 1}
            error_text = str(result.get("error") or "")
            if compile_failure:
                item["failure_type"] = "compilation_error"
            elif "output mismatch" in error_text.lower():
                item["failure_type"] = "output_mismatch"
            elif error_text:
                item["failure_type"] = "runtime_error"
            else:
                item["failure_type"] = "unverified_output"
            for output_key, source_key, max_length in (
                ("input", "input", 120),
                ("expected", "expected_output", 120),
                ("actual", "actual_output", 120),
            ):
                value = test.get(source_key, "") if source_key in ("input", "expected_output") else result.get(source_key, "")
                item[output_key] = str(value or "")[:max_length]
            item["error"] = _challenge_runtime_error_summary(result.get("error"), 180)
            failed_cases.append(item)
            if len(failed_cases) >= limit:
                break
    feedback = {
        "kind": "reference_solution_failed_tests",
        "compiled": bool(reference_result.get("is_compiled")) and not compile_failure,
        "failed_case_count": sum(1 for item in results if isinstance(item, dict) and item.get("passed") is not True) if isinstance(results, list) else 0,
        "failed_cases": failed_cases,
    }
    compile_error = reference_result.get("compile_error")
    overall_error = reference_result.get("overall_error")
    if compile_error or (not failed_cases and overall_error):
        feedback["execution_error"] = str(compile_error or overall_error)[:240]
    return feedback


def _safe_reference_exception_message(exception_class, message, error_category):
    """Map runtime text to short diagnostic phrases that cannot echo source/input."""
    if error_category == "timeout":
        return "execution exceeded configured timeout"
    if error_category == "process_launch_failure":
        return "child process launch failed"
    if error_category == "process_exit":
        return "child process exited unsuccessfully"
    if error_category == "compilation_error":
        return "reference failed compilation"
    if error_category == "jvm_memory_reservation_failure":
        return "Java VM memory reservation failed"
    if error_category == "jvm_initialization_failure":
        return "Java VM initialization failed"
    if error_category == "java_class_loading_failure":
        return "Java class loading failed"
    if error_category == "java_entry_point_failure":
        return "Java entry point is missing or invalid"
    if error_category.startswith("java_runtime_exception") or error_category.startswith("java_compile_"):
        return str(message or "Java diagnostic details redacted")[:160]
    text = str(message or "").strip()
    if exception_class == "ValueError":
        if re.match(r"invalid literal for int\(\) with base 10:", text):
            return "invalid integer input (value redacted)"
        if re.match(r"could not convert string to float:", text):
            return "invalid numeric input (value redacted)"
        if re.match(r"not enough values to unpack\b", text):
            return "not enough values to unpack"
        if re.match(r"too many values to unpack\b", text):
            return "too many values to unpack"
    if exception_class == "IndexError" and text in (
        "list index out of range", "tuple index out of range", "string index out of range",
    ):
        return text
    if exception_class == "ZeroDivisionError" and text == "division by zero":
        return text
    if exception_class == "EOFError" and text.startswith("EOF when reading a line"):
        return "input ended before a line was available"
    if exception_class in ("NumberFormatException", "InputMismatchException"):
        return "numeric input format is invalid"
    if exception_class == "NoSuchElementException":
        return "input ended before the required token was available"
    if exception_class == "ArithmeticException" and text == "/ by zero":
        return "division by zero"
    return "exception details redacted"


def _java_process_exit_diagnostic(error):
    """Return fixed diagnostics for common Java launcher failures, never raw stderr."""
    text = str(error or "")
    if re.search(r"could not reserve enough space|cannot allocate memory|insufficient memory for the java runtime environment", text, re.I):
        return "jvm_memory_reservation_failure", "JVMStartupError", "Java VM memory reservation failed"
    if re.search(r"error occurred during initialization of vm|could not create the java virtual machine|unrecognized vm option", text, re.I):
        return "jvm_initialization_failure", "JVMStartupError", "Java VM initialization failed"
    if re.search(r"could not find or load main class|ClassNotFoundException", text, re.I):
        return "java_class_loading_failure", "ClassNotFoundException", "Java entry class could not be loaded"
    if re.search(r"main method not found|main method must return a value of type void", text, re.I):
        return "java_entry_point_failure", "MainMethodError", "Java entry point is missing or invalid"
    if re.search(r"NoClassDefFoundError", text, re.I):
        return "java_class_loading_failure", "NoClassDefFoundError", "Required Java class could not be loaded"
    return None


def _java_runtime_exception_diagnostic(error):
    """Extract a known Java exception class and a fixed, redacted message."""
    text = str(error or "")[-4000:]
    patterns = (
        r'Exception in thread\s+"[^"\r\n]{0,80}"\s+([\w.$]+Exception|[\w.$]+Error)',
        r"Caused by:\s+([\w.$]+Exception|[\w.$]+Error)",
        r"^\s*([\w.$]+Exception|[\w.$]+Error)(?::|$)",
    )
    match = next((m for pattern in patterns for m in re.finditer(pattern, text, re.M)), None)
    if not match:
        return None
    class_name = match.group(1).rsplit(".", 1)[-1]
    safe_messages = {
        "NumberFormatException": "numeric input format is invalid (input value redacted)",
        "IllegalArgumentException": "Java rejected an argument (details redacted)",
        "InputMismatchException": "input token has an unexpected format",
        "NoSuchElementException": "required input token was missing",
        "ArrayIndexOutOfBoundsException": "array index was out of bounds",
        "IndexOutOfBoundsException": "index was out of bounds",
        "NullPointerException": "null reference was accessed",
        "ArithmeticException": "arithmetic operation failed",
        "StackOverflowError": "Java stack limit was exceeded",
        "OutOfMemoryError": "Java runtime memory was exhausted",
        "ClassNotFoundException": "required Java class could not be loaded",
        "NoClassDefFoundError": "required Java class could not be loaded",
        "ExceptionInInitializerError": "Java class initialization failed",
    }
    if class_name not in safe_messages:
        return "java_runtime_exception", "JavaRuntimeException", "Java runtime exception (details redacted)"
    return "java_runtime_exception", class_name, safe_messages[class_name]


def _java_compiler_diagnostic(error):
    """Classify javac stderr without including source excerpts or paths in logs."""
    text = str(error or "")[-4000:].lower()
    diagnostics = (
        ("cannot find symbol", "java_compile_symbol_not_found", "javac could not resolve a symbol"),
        ("incompatible types", "java_compile_type_mismatch", "javac found incompatible types"),
        ("';' expected", "java_compile_syntax_error", "javac expected a statement terminator"),
        ("')' expected", "java_compile_syntax_error", "javac expected a closing parenthesis"),
        ("illegal start of", "java_compile_syntax_error", "javac found an invalid syntax position"),
        ("reached end of file while parsing", "java_compile_syntax_error", "javac found an incomplete declaration"),
        ("unclosed string literal", "java_compile_syntax_error", "javac found an unclosed string literal"),
        ("class is public, should be declared in a file named", "java_compile_class_name_mismatch", "public class name does not match source filename"),
    )
    for needle, category, message in diagnostics:
        if needle in text:
            return category, "JavacError", message
    if re.search(r"\berror:\s*", text):
        return "java_compile_error", "JavacError", "javac reported a compilation error (details redacted)"
    return None


def _reference_failure_log_details(language, validation_diagnostics, execution_diagnostics=None):
    """Build bounded log-safe diagnostics without source, inputs, or outputs."""
    language = language if language in ("python", "c", "java") else "unknown"
    entries = []
    cases = validation_diagnostics.get("failed_cases", []) if isinstance(validation_diagnostics, dict) else []
    raw_errors = execution_diagnostics.get("failed_errors", []) if isinstance(execution_diagnostics, dict) else []
    for item in cases[:4] if isinstance(cases, list) else []:
        if not isinstance(item, dict):
            continue
        error = _challenge_runtime_error_summary(item.get("error"), 180)
        case_number = int(item["case"]) if str(item.get("case", "")).isdigit() else None
        raw_error = (str(raw_errors[case_number - 1] or "")[-4000:]
                     if isinstance(raw_errors, list) and case_number and case_number <= len(raw_errors)
                     else error)
        exception_class = None
        exception_message = ""
        java_exception = re.search(r'Exception in thread\s+"[^"]{0,40}"\s+([\w.$]+)(?::\s*(.*))?$', error)
        typed_exception = re.search(r"^([\w.$]*(?:Error|Exception|Fault|Failure)):\s*(.*)$", error)
        cpp_exception = re.search(r"terminate called after throwing an instance of '([^']{1,80})'", error)
        if java_exception:
            exception_class = java_exception.group(1).rsplit(".", 1)[-1]
            exception_message = java_exception.group(2) or ""
        elif typed_exception:
            exception_class = typed_exception.group(1)
            exception_message = typed_exception.group(2)
        elif cpp_exception:
            exception_class = cpp_exception.group(1)
            exception_message = error[cpp_exception.end():].lstrip(": ")

        timed_out = bool(re.search(r"\b(?:execution timed out|timed out|timeout expired)\b", error, re.I))
        process_launch_failed = error.startswith("Execution failed:")
        exit_match = re.search(r"Runtime Exit Code\s+(-?\d+)", error)
        if item.get("failure_type") == "compilation_error":
            error_category = "compilation_error"
            if language == "java":
                compiler_detail = _java_compiler_diagnostic(raw_error)
                if compiler_detail:
                    error_category, exception_class, exception_message = compiler_detail
        elif timed_out:
            error_category = "timeout"
            exception_class = exception_class or "TimeoutExpired"
        elif process_launch_failed:
            error_category = "process_launch_failure"
            exception_class = exception_class or "OSError"
            exception_message = exception_message or error.partition(":")[2].strip()
        elif language == "java" and exit_match and _java_process_exit_diagnostic(error):
            error_category, exception_class, exception_message = _java_process_exit_diagnostic(error)
        elif language == "java" and exit_match and _java_process_exit_diagnostic(raw_error):
            error_category, exception_class, exception_message = _java_process_exit_diagnostic(raw_error)
        elif language == "java" and exit_match and _java_runtime_exception_diagnostic(raw_error):
            error_category, exception_class, exception_message = _java_runtime_exception_diagnostic(raw_error)
        elif exception_class:
            error_category = "runtime_exception"
        elif exit_match:
            error_category = "process_exit"
        else:
            error_category = str(item.get("failure_type") or "runtime_error")[:40]
        if exception_class and not re.fullmatch(r"[A-Za-z_][\w.$]{0,99}", exception_class):
            exception_class = "OtherException"
        message = _safe_reference_exception_message(exception_class, exception_message, error_category)[:160]
        entries.append({
            "case": case_number,
            "error_category": error_category,
            "exception_class": exception_class[:100] if isinstance(exception_class, str) else None,
            "exception_message": message,
            "timed_out": timed_out,
            "process_launch_failed": process_launch_failed,
            "process_exit_code": int(exit_match.group(1)) if exit_match else None,
        })
    return {"language": language, "failed_case_count": min(100, max(0, int(
        validation_diagnostics.get("failed_case_count", len(entries))
    ))) if isinstance(validation_diagnostics, dict) else len(entries), "failed_cases": entries}


def _log_reference_solution_validation_failure(language, validation_diagnostics, provider, attempt, source, execution_diagnostics=None):
    safe_provider = provider if provider in ("openai", "groq", "local") else "unknown"
    safe_source = source if source in ("ai_generated", "local_curated") else "unknown"
    details = _reference_failure_log_details(language, validation_diagnostics, execution_diagnostics)
    logging.getLogger("codementor.ai").warning(
        "challenge_reference_validation_failed provider=%s source=%s attempt=%s compiled=%s diagnostics=%s",
        safe_provider, safe_source, max(0, min(100, int(attempt))),
        bool(validation_diagnostics.get("compiled")),
        json.dumps(details, ensure_ascii=True, separators=(",", ":")),
    )


def _validate_generated_challenge(candidate, language, category, diagnostics=None, execution_diagnostics=None):
    if not isinstance(candidate, dict):
        return None, "invalid_schema"
    required_strings = ("title", "description", "buggy_code", "solution_code")
    if any(not isinstance(candidate.get(key), str) or not candidate[key].strip() for key in required_strings):
        return None, "missing_required_field"
    if any(len(candidate[key]) > limit for key, limit in (("title", 120), ("description", 2000), ("buggy_code", 100000), ("solution_code", 100000))):
        return None, "field_too_large"
    if str(candidate.get("category", "")).strip().lower() != category:
        return None, "category_mismatch"
    tests = candidate.get("test_cases")
    if not isinstance(tests, list) or not 2 <= len(tests) <= 10:
        return None, "invalid_test_case_count"
    clean_tests = []
    for test in tests:
        if not isinstance(test, dict) or not isinstance(test.get("input"), str) or not isinstance(test.get("expected_output"), str):
            return None, "invalid_test_case"
        if len(test["input"]) > 4000 or len(test["expected_output"]) > 2000:
            return None, "test_case_too_large"
        if not test["expected_output"].strip():
            return None, "empty_expected_output"
        clean_tests.append({"input": test["input"], "expected_output": test["expected_output"]})
    candidate = {**candidate, "category": category, "test_cases": clean_tests,
                 "topic": str(candidate.get("topic") or category.replace("_", " ")).strip()[:80]}
    candidate = _format_challenge_candidate(candidate, language)
    if re.sub(r"\s+", "", candidate["buggy_code"]) == re.sub(r"\s+", "", candidate["solution_code"]):
        return None, "buggy_code_matches_reference"
    reference_result = code_executor.execute_code(candidate["solution_code"], language, clean_tests)
    if not reference_result.get("is_compiled") or not reference_result.get("all_passed"):
        if isinstance(execution_diagnostics, dict):
            results = reference_result.get("test_results")
            execution_diagnostics["failed_errors"] = [
                str(item.get("error") or "") if isinstance(item, dict) else ""
                for item in results
            ] if isinstance(results, list) else []
            execution_diagnostics["compile_error"] = str(reference_result.get("compile_error") or "")
        if isinstance(diagnostics, dict):
            diagnostics.update(_reference_test_failure_feedback(reference_result, clean_tests))
        return None, "reference_solution_failed_tests"
    buggy_result = code_executor.execute_code(candidate["buggy_code"], language, clean_tests)
    if not _challenge_failure_is_valid(category, buggy_result):
        return None, "intended_bug_not_observed"
    candidate["reference_result"] = reference_result
    candidate["buggy_result"] = buggy_result
    return candidate, None


def _challenge_generation_prompt(language, difficulty, category, recent, rejected):
    recent_summary = [{"language": item["language"], "difficulty": item["difficulty"],
        "title": item["title"], "description": str(item["description"] or "")[:240], "category": item["category"],
        "code_structure": " ".join(item["code_shape"][:100])} for item in recent[:12]]
    prompt = ("Return one JSON object only with keys title, description, topic, category, buggy_code, solution_code, test_cases. "
        "Create a genuinely new beginner-friendly debugging challenge. The description must specify the exact stdin input format and stdout output format, state the intended task neutrally, and must not reveal the injected bug or its fix. "
        "The solution_code must correctly implement that exact input/output contract; buggy_code must differ by exactly one intentional bug. "
        "Provide at least two useful test_cases as objects with string input and non-empty expected_output fields, including a meaningful edge case. Independently calculate every expected_output from the described task and verify that the solution_code would produce it exactly (ignoring only line-ending differences and trailing whitespace). Each input must follow the stated format. Do not include prompts, labels, or explanatory text in stdout unless the description explicitly requires them. "
        "If prior candidate validation feedback is included, treat it as mandatory: fix reference syntax for compilation errors, parsing or logic for runtime errors, and the code or expected output for output mismatches; correct input formats when they caused a failure. Keep the statement, solution, inputs, and expected outputs consistent. Do not repeat a rejected reference/test pair. "
        "Use only standard input/output, with no filesystem, network, shell, process-control, concurrency, or external dependency behavior. "
        f"Language: {language}. Difficulty: {difficulty}. Required bug topic/category: {category}. "
        "Vary the problem scenario and code structure from these recently shown challenges for this user; do not reuse a recent title, statement, code structure, or bug pattern. "
        f"Recent challenges: {json.dumps(recent_summary, ensure_ascii=False)}")
    if rejected:
        prompt += " Recent candidates already rejected in this generation: " + json.dumps(rejected[-4:], ensure_ascii=False)
    return prompt


def _generate_diverse_bug_challenge(user_id, payload):
    language = str(payload.get("language", "python")).lower().strip()
    language = {"py": "python", "cpp": "c", "c++": "c"}.get(language, language)
    difficulty = str(payload.get("difficulty", "Easy")).strip().title()
    if language not in ("python", "c", "java"):
        return {"error": "Choose Python, C, or Java for a bug challenge"}, 400
    if difficulty not in ("Easy", "Medium", "Hard"):
        return {"error": "Choose Easy, Medium, or Hard difficulty"}, 400

    requested_topic = str(payload.get("topic") or payload.get("category", "random")).strip().lower()
    topic_aliases = {"logical_errors": "incorrect_conditions", "loop_boundary": "off_by_one"}
    requested_topic = topic_aliases.get(requested_topic, requested_topic)
    if requested_topic not in ("any", "random", *_BUG_CHALLENGE_CATEGORIES):
        return {"error": "Choose one of the supported bug challenge categories"}, 400

    recent = _recent_bug_challenges(user_id)
    recent_categories = [item["category"] for item in recent if item.get("category")]
    tried_categories = []
    rejected = []
    last_reason = "AI unavailable"
    generated = None
    for generation_number in range(_CHALLENGE_GENERATION_ATTEMPTS):
        if requested_topic in ("any", "random"):
            recent_window = recent_categories[:5]
            category_pool = [item for item in sorted(_BUG_CHALLENGE_CATEGORIES)
                             if item not in recent_window and item not in tried_categories]
            if not category_pool:
                category_pool = [item for item in sorted(_BUG_CHALLENGE_CATEGORIES) if item not in tried_categories]
            if not category_pool:
                category_pool = sorted(_BUG_CHALLENGE_CATEGORIES)
            category = secrets.choice(category_pool)
        else:
            category = requested_topic
        tried_categories.append(category)
        prompt = _challenge_generation_prompt(language, difficulty, category, recent, rejected)
        ai = tutor_engine.generate_ai(prompt, "You create safe, genuinely varied beginner debugging challenges. Return valid JSON only.", "ai_challenge_generation")
        if not ai.get("text") or ai.get("provider") not in ("openai", "groq"):
            response_status = ai.get("response_status")
            provider_attempts = ai.get("provider_attempts") or []
            outcomes = {item.get("outcome") for item in provider_attempts if isinstance(item, dict)}
            if response_status == "invalid_response":
                last_reason = "AI returned an incomplete or unusable response"
            elif "http_error" in outcomes:
                last_reason = "AI provider rejected the request"
            elif outcomes.intersection({"network_error", "timeout", "request_error"}):
                last_reason = "AI provider connection failed"
            elif response_status == "not_configured":
                last_reason = "AI provider is not configured"
            else:
                last_reason = "AI provider is unavailable"
            logging.getLogger("codementor.ai").info(
                "challenge_ai_unavailable provider=%s outcome=%s",
                ai.get("configured_provider", "unknown"), response_status or "unknown")
            break
        raw = ai["text"].strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
        try:
            raw = raw[raw.find("{"):raw.rfind("}") + 1]
            candidate = json.loads(raw)
        except (ValueError, TypeError):
            last_reason = "AI response was not valid challenge JSON"
            rejected.append({"category": category, "reason": last_reason})
            logging.getLogger("codementor.ai").warning("challenge_candidate_rejected provider=%s reason=invalid_json attempt=%s",
                ai.get("provider", "unknown"), generation_number + 1)
            continue
        validation_diagnostics = {}
        execution_diagnostics = {}
        candidate, validation_reason = _validate_generated_challenge(
            candidate, language, category, validation_diagnostics, execution_diagnostics)
        if not candidate:
            last_reason = "AI challenge did not pass validation"
            rejection = {"category": category, "reason": validation_reason}
            if validation_diagnostics:
                rejection["validation_feedback"] = validation_diagnostics
            rejected.append(rejection)
            if validation_reason == "reference_solution_failed_tests":
                _log_reference_solution_validation_failure(
                    language, validation_diagnostics, ai.get("provider", "unknown"),
                    generation_number + 1, "ai_generated", execution_diagnostics)
            failed_cases = validation_diagnostics.get("failed_cases", [])
            failure_types = sorted({item.get("failure_type") for item in failed_cases
                                    if isinstance(item, dict) and item.get("failure_type")})
            logging.getLogger("codementor.ai").warning(
                "challenge_candidate_rejected provider=%s reason=%s attempt=%s compiled=%s failed_case_count=%s failed_cases=%s failure_types=%s",
                ai.get("provider", "unknown"), validation_reason, generation_number + 1,
                validation_diagnostics.get("compiled", "unknown"),
                validation_diagnostics.get("failed_case_count", 0),
                ",".join(str(item.get("case")) for item in failed_cases if isinstance(item, dict)) or "none",
                ",".join(failure_types) or "unknown")
            continue
        candidate.update(language=language, difficulty=difficulty)
        duplicate_reason = _challenge_duplicate_reason(candidate, recent)
        if duplicate_reason:
            last_reason = "AI challenge repeated recent user history"
            rejected.append({"title": candidate["title"], "category": category,
                "reason": duplicate_reason, "code_structure": " ".join(_challenge_code_tokens(candidate["buggy_code"])[:100])})
            logging.getLogger("codementor.ai").info("challenge_candidate_rejected provider=%s reason=%s attempt=%s",
                ai.get("provider", "unknown"), duplicate_reason, generation_number + 1)
            continue
        conn = database.get_connection()
        try:
            cursor = conn.execute("""INSERT INTO problems (slug,title,language,difficulty,concept,mistake_category_slug,
                description,buggy_code,solution_code,test_cases_json,stages_config_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", ("ai-" + uuid.uuid4().hex[:14], candidate["title"], language, difficulty,
                 candidate["topic"], category, candidate["description"] + " (AI-generated; reference and challenge validated against saved tests.)",
                 candidate["buggy_code"], candidate["solution_code"], json.dumps(candidate["test_cases"]), "{}"))
            conn.commit()
            challenge_id = cursor.lastrowid
        finally:
            conn.close()
        generated = {"id": challenge_id, "title": candidate["title"], "language": language,
            "difficulty": difficulty, "category": category, "topic": candidate["topic"],
            "description": candidate["description"], "source": ai["provider"], "is_ai_generated": True,
            "validation": {"reference_passed": True, "reference_tests_passed": len(candidate["test_cases"]),
                "buggy_code_compiled": _challenge_bug_compiled(category, candidate["buggy_result"]),
                "at_least_one_test_failed": not bool(candidate["buggy_result"].get("all_passed")),
                "intended_failure_observed": True}}
        logging.getLogger("codementor.ai").info("challenge_generated provider=%s language=%s difficulty=%s category=%s",
            ai["provider"], language, difficulty, category)
        return {"challenge": generated, "ai_status": tutor_engine.get_ai_provider_status()}, 200

    # A curated challenge is used only after bounded AI generation fails. Select a
    # non-recent template when possible, and never claim a local template is AI-made.
    if requested_topic in ("any", "random"):
        fallback_categories = list(dict.fromkeys(tried_categories + sorted(
            _BUG_CHALLENGE_CATEGORIES, key=lambda item: (recent_categories.count(item), item))))
    else:
        fallback_categories = [requested_topic]
    for category in fallback_categories:
        fallback = _local_bug_challenge(language, difficulty, category)
        if not fallback:
            continue
        formatted = dict(fallback)
        if language in ("java", "c"):
            formatted["buggy_code"] = _format_semicolon_challenge_code(formatted["buggy_code"], language)
            formatted["solution_code"] = _format_semicolon_challenge_code(formatted["solution_code"], language)
        else:
            formatted["buggy_code"] = _format_python_challenge_code(formatted["buggy_code"])
            formatted["solution_code"] = _format_python_challenge_code(formatted["solution_code"])
        tests = formatted["test_cases"]
        reference_result = code_executor.execute_code(formatted["solution_code"], language, tests)
        if not reference_result.get("is_compiled") or not reference_result.get("all_passed"):
            fallback_diagnostics = _reference_test_failure_feedback(reference_result, tests)
            fallback_execution_diagnostics = {
                "failed_errors": [str(item.get("error") or "") if isinstance(item, dict) else ""
                                  for item in reference_result.get("test_results", [])]
            }
            _log_reference_solution_validation_failure(
                language, fallback_diagnostics, "local", 0, "local_curated", fallback_execution_diagnostics)
            continue
        buggy_result = code_executor.execute_code(formatted["buggy_code"], language, tests)
        if not _challenge_failure_is_valid(category, buggy_result):
            continue
        duplicate_candidate = {**formatted, "language": language, "category": category}
        if _challenge_duplicate_reason(duplicate_candidate, recent):
            continue
        conn = database.get_connection()
        try:
            conn.execute("""INSERT OR IGNORE INTO problems (slug,title,language,difficulty,concept,mistake_category_slug,
                description,buggy_code,solution_code,test_cases_json,stages_config_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (formatted["slug"] + "-readable-v1", formatted["title"], language,
                difficulty, formatted["concept"], category, formatted["description"], formatted["buggy_code"],
                formatted["solution_code"], json.dumps(tests), formatted.get("stages_config_json", "{}")))
            conn.commit()
            row = conn.execute("SELECT id FROM problems WHERE slug=?", (formatted["slug"] + "-readable-v1",)).fetchone()
            challenge_id = row["id"]
        finally:
            conn.close()
        generated = {"id": challenge_id, "title": formatted["title"], "language": language,
            "difficulty": difficulty, "category": category, "topic": formatted["concept"],
            "description": formatted["description"], "source": "local_curated", "is_ai_generated": False,
            "validation": {"reference_passed": True, "reference_tests_passed": len(tests),
                "buggy_code_compiled": _challenge_bug_compiled(category, buggy_result),
                "at_least_one_test_failed": not bool(buggy_result.get("all_passed")),
                "intended_failure_observed": True}, "fallback_reason": last_reason}
        return {"challenge": generated, "ai_status": tutor_engine.get_ai_provider_status()}, 200
    return {"error": "Could not create a new validated challenge for this topic. Try another topic or difficulty."}, 503




# Feature 9/10: owner-scoped analytics integrated into the main server.
FEATURE9_TOPICS = [
    ("variables_operators", "Variables & Operators", ("variable_scope", "algorithm_logic"), ("variable", "operator", "input output")),
    ("conditions", "Conditions", ("incorrect_conditions",), ("condition", "if else", "boolean")),
    ("loops", "Loops", ("loop_logic", "off_by_one"), ("loop", "iteration")),
    ("arrays", "Arrays", ("array_indexing",), ("array", "index")),
    ("strings", "Strings", ("string_handling_errors",), ("string", "text")),
    ("methods", "Methods & Functions", ("function_method_errors",), ("method", "function")),
    ("recursion", "Recursion", ("recursion_misunderstanding",), ("recursion", "recursive")),
    ("search_sort", "Search & Sort", (), ("search", "sort")),
    ("stack_queue", "Stacks & Queues", ("data_structure_errors",), ("stack", "queue")),
    ("linked_lists", "Linked Lists", ("null_pointer_errors",), ("linked list", "pointer")),
    ("oop", "Object-Oriented Programming", (), ("object oriented", "oop", "class", "inheritance")),
]

FEATURE9_SKILLS = [
    ("variables", "Variables & data types", 1, "variables_operators", ()),
    ("operators", "Operators & expressions", 1, "variables_operators", ("variables",)),
    ("input_output", "Input & output", 1, "variables_operators", ("variables",)),
    ("conditions", "If / else conditions", 2, "conditions", ("operators",)),
    ("loops", "Loops", 2, "loops", ("conditions",)),
    ("nested_loops", "Nested loops", 2, "loops", ("loops",)),
    ("methods", "Methods & functions", 3, "methods", ("conditions",)),
    ("arrays", "Arrays", 3, "arrays", ("loops",)),
    ("strings", "Strings", 3, "strings", ("arrays",)),
    ("problem_solving", "Problem solving", 3, "variables_operators", ("conditions", "loops")),
    ("recursion", "Recursion", 4, "recursion", ("methods", "loops")),
    ("search", "Searching", 4, "search_sort", ("arrays",)),
    ("sorting", "Sorting", 4, "search_sort", ("search",)),
    ("oop", "Object-oriented programming", 4, "oop", ("methods",)),
    ("linked_lists", "Linked lists", 5, "linked_lists", ("oop", "arrays")),
    ("stacks_queues", "Stacks & queues", 5, "stack_queue", ("arrays", "methods")),
]


def _loads(value):
    try:
        result = json.loads(value or "[]")
        return result if isinstance(result, list) else []
    except (TypeError, ValueError):
        return []


def _records(conn, user_id):
    rows = conn.execute("""SELECT a.id,a.problem_id,a.language,a.mistake_categories_json,a.hints_used,
        a.is_solved,a.solved_independently,a.created_at,a.updated_at,
        COALESCE(p.title,a.custom_title,'Coding attempt') title,
        COALESCE(p.concept,'') concept,COALESCE(p.difficulty,'') difficulty,
        COALESCE(p.description,'') description,
        (SELECT h.context_json FROM hint_logs h WHERE h.attempt_id=a.id AND h.hint_type='explain_fix'
         ORDER BY h.id DESC LIMIT 1) explanation_context,
        EXISTS(SELECT 1 FROM hint_logs h WHERE h.attempt_id=a.id AND h.hint_type='explain_fix'
          AND json_valid(h.context_json) AND json_extract(h.context_json,'$.evaluation_status')='correct') explained
        FROM attempts a LEFT JOIN problems p ON p.id=a.problem_id
        WHERE a.user_id=? ORDER BY a.created_at,a.id""", (user_id,)).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["categories"] = _loads(d.pop("mistake_categories_json"))
        d["solved"] = bool(d.pop("is_solved"))
        d["explained"] = bool(d["explained"])
        result.append(d)
    return result


def _topic_matches(record, category_slugs, keywords):
    categories = set(record["categories"])
    if categories.intersection(category_slugs):
        return True
    haystack = " ".join(str(record.get(k) or "") for k in ("title", "concept", "description")).lower()
    return any(word in haystack for word in keywords)


def build_overview(conn, user_id, today=None):
    """Compute owner-scoped insights; conn must be opened read-only by caller."""
    today = today or date.today()
    records = _records(conn, int(user_id))
    topic_stats = []
    by_topic = {}
    for slug, name, categories, keywords in FEATURE9_TOPICS:
        matching = [r for r in records if _topic_matches(r, categories, keywords)]
        solved = sum(r["solved"] for r in matching)
        accuracy = round(100 * solved / len(matching)) if matching else 0
        split = max(1, len(matching) // 2)
        old = matching[:split]
        recent = matching[split:] if len(matching) > 1 else []
        old_acc = 100 * sum(r["solved"] for r in old) / len(old) if old else 0
        recent_acc = 100 * sum(r["solved"] for r in recent) / len(recent) if recent else 0
        repeats = sum(max(0, count - 1) for cat in categories
                      if (count := sum(cat in r["categories"] for r in matching)) > 1)
        if len(matching) < 2:
            status = "insufficient_data"
        elif len(matching) >= 4 and accuracy >= 80 and repeats < 2:
            status = "strong"
        elif len(recent) and recent_acc >= 50 and recent_acc - old_acc >= 15:
            status = "improving"
        else:
            status = "needs_practice"
        stat = {"id": slug, "name": name, "attempts": len(matching), "solved": solved,
                "unsolved": len(matching)-solved, "accuracy": accuracy, "hints": sum(int(r["hints_used"] or 0) for r in matching),
                "repeated_failures": repeats, "status": status,
                "trend": "up" if recent and recent_acc > old_acc else "down" if recent and recent_acc < old_acc else "steady"}
        topic_stats.append(stat)
        by_topic[slug] = matching

    plan = []
    for stat in sorted(topic_stats, key=lambda x: (x["status"] != "needs_practice", x["accuracy"], -x["attempts"])):
        if stat["attempts"] < 2 or stat["status"] == "strong":
            continue
        plan.append({"topic_id": stat["id"], "topic": stat["name"],
            "reason": f"{stat['accuracy']}% accuracy across {stat['attempts']} saved attempts; {stat['repeated_failures']} repeated category failures.",
            "focus": "Review the concept, trace one example by hand, then solve a fresh problem and explain the cause of the bug.",
            "difficulty": "Easy" if stat["accuracy"] < 40 else "Medium",
            "daily_goal": "Complete one focused practice attempt today.",
            "weekly_goal": "Complete three attempts this week and give one correct fix explanation.",
            "daily_done": any(_on_date(r["created_at"], today) and r["solved"] for r in by_topic[stat["id"]]),
            "weekly_solved": sum(r["solved"] and _within_days(r["created_at"], today, 7) for r in by_topic[stat["id"]]),
            "weekly_explanations": sum(r["explained"] and _within_days(r["created_at"], today, 7) for r in by_topic[stat["id"]]),
            "status": "in_progress"})
    for item in plan:
        if item["daily_done"] and item["weekly_solved"] >= 3 and item["weekly_explanations"] >= 1:
            item["status"] = "completed"

    completed_ids = set()
    skills = []
    for sid, name, level, topic_id, prerequisites in FEATURE9_SKILLS:
        matching = by_topic[topic_id]
        solved = sum(r["solved"] for r in matching)
        explained = sum(r["solved"] and r["explained"] for r in matching)
        evidence = min(100, round(100 * min(solved, 2) / 2) + (50 if explained else 0))
        done = solved >= 2 and explained >= 1
        status = "completed" if done else "in_progress" if matching else "available" if all(p in completed_ids for p in prerequisites) else "locked"
        if done:
            completed_ids.add(sid)
        skills.append({"id": sid, "name": name, "level": level, "topic": topic_id,
            "prerequisites": list(prerequisites), "status": status, "evidence_attempts": len(matching),
            "successful_attempts": solved, "correct_explanations": explained, "progress": evidence,
            "completion_rule": "2 verified solved attempts plus 1 correctly evaluated explanation in this topic"})

    return {"heatmap": topic_stats, "study_plan": plan, "skill_tree": skills,
            "skill_progress": {"completed": len(completed_ids), "total": len(FEATURE9_SKILLS),
                               "percent": round(100 * len(completed_ids) / len(FEATURE9_SKILLS))},
            "thresholds": {"insufficient_data": "Fewer than 2 attempts", "strong": "At least 4 attempts, at least 80% accuracy, fewer than 2 repeated category failures",
                "improving": "At least 2 attempts; recent accuracy is at least 50% and at least 15 points above earlier accuracy",
                "needs_practice": "At least 2 attempts, but the Strong or Improving rule is not yet met"},
            "total_attempts": len(records)}


def _within_days(value, today, days):
    try:
        parsed = date.fromisoformat(str(value)[:10])
        return today - timedelta(days=days-1) <= parsed <= today
    except (TypeError, ValueError):
        return False


def _on_date(value, today):
    try:
        return date.fromisoformat(str(value)[:10]) == today
    except (TypeError, ValueError):
        return False

# Features 11/12: interview and competition logic integrated into the main server.
LANGUAGES = ("java", "python", "c", "cpp")
DIFFICULTIES = ("Easy", "Medium", "Hard")
TOPICS = ("Arrays", "Strings", "Searching and Sorting", "Recursion", "Linked List", "Stack and Queue", "Basic OOP", "Problem Solving", "Mixed")
INTERVIEW_DURATIONS = (15, 30, 45, 60)
QUESTION_COUNTS = (1, 2, 3, 4, 5)
_challenge_factory = None
_challenge_formatter = None


def set_challenge_factory(factory, formatter=None):
    """Inject the existing main-server curated generator without changing it."""
    global _challenge_factory, _challenge_formatter
    _challenge_factory, _challenge_formatter = factory, formatter

QUESTIONS = [
    {"id":"array_sum","topic":"Arrays","difficulty":"Easy","title":"Sum an Array","statement":"Read n followed by n integers. Print their sum.","io":"Input: n, then n integers. Output: one integer sum.","constraints":"1 <= n <= 100000; values fit in signed 32-bit integers.","examples":[{"input":"5\n1 2 3 4 5\n","output":"15"}],
     "cases":[("5\n1 2 3 4 5\n","15"),("1\n-7\n","-7"),("4\n0 -2 8 1\n","7"),("3\n100 200 300\n","600")],
     "solutions":{
      "python":"import sys\ndata=list(map(int,sys.stdin.buffer.read().split()))\nn=data[0]\nprint(sum(data[1:1+n]))\n",
      "java":"import java.util.*;\npublic class Main { public static void main(String[] args) { Scanner s=new Scanner(System.in); int n=s.nextInt(); long total=0; for(int i=0;i<n;i++) total+=s.nextInt(); System.out.println(total); } }\n",
      "c":"#include <stdio.h>\nint main(void){int n; long long x,total=0; if(scanf(\"%d\",&n)!=1)return 0; for(int i=0;i<n;i++){scanf(\"%lld\",&x);total+=x;} printf(\"%lld\\n\",total);return 0;}\n"}},
    {"id":"string_reverse","topic":"Strings","difficulty":"Easy","title":"Reverse a Line","statement":"Read one line of text and print its characters in reverse order. Spaces are part of the input.","io":"Input: one line. Output: the reversed line.","constraints":"The line contains at most 10000 characters.","examples":[{"input":"code mentor\n","output":"rotnem edoc"}],
     "cases":[("code mentor\n","rotnem edoc"),("a\n","a"),("\n",""),("level\n","level")],
     "solutions":{
      "python":"import sys\nsys.stdout.write(sys.stdin.readline().rstrip('\\r\\n')[::-1]+'\\n')\n",
      "java":"import java.io.*;\npublic class Main { public static void main(String[] args) throws Exception { BufferedReader r=new BufferedReader(new InputStreamReader(System.in)); String x=r.readLine(); if(x==null)x=\"\"; System.out.println(new StringBuilder(x).reverse()); } }\n",
      "c":"#include <stdio.h>\n#include <string.h>\nint main(void){char s[10005];if(!fgets(s,sizeof(s),stdin))return 0;int n=(int)strlen(s);while(n>0&&(s[n-1]=='\\n'||s[n-1]=='\\r'))n--;for(int i=n-1;i>=0;i--)putchar(s[i]);putchar('\\n');return 0;}\n"}},
    {"id":"binary_search","topic":"Searching and Sorting","difficulty":"Medium","title":"Binary Search","statement":"Given a sorted array and a target, print the target's zero-based index. Print -1 if it is absent. If it occurs more than once, print any matching index.","io":"Input: n, n sorted integers, then target. Output: one index.","constraints":"1 <= n <= 100000; array values are sorted in nondecreasing order.","examples":[{"input":"5\n1 3 5 7 9\n7\n","output":"3"}],
     "cases":[("5\n1 3 5 7 9\n7\n","3"),("4\n2 4 6 8\n1\n","-1"),("1\n-3\n-3\n","0"),("6\n0 2 4 6 8 10\n8\n","4")],
     "solutions":{
      "python":"import sys\na=list(map(int,sys.stdin.buffer.read().split())); n=a[0]; nums=a[1:1+n]; target=a[1+n]\nlo,hi=0,n-1; ans=-1\nwhile lo<=hi:\n mid=(lo+hi)//2\n if nums[mid]==target: ans=mid; break\n if nums[mid]<target: lo=mid+1\n else: hi=mid-1\nprint(ans)\n",
      "java":"import java.util.*;\npublic class Main { public static void main(String[] args) { Scanner s=new Scanner(System.in); int n=s.nextInt(); int[] a=new int[n]; for(int i=0;i<n;i++)a[i]=s.nextInt(); int target=s.nextInt(),lo=0,hi=n-1,ans=-1; while(lo<=hi){int m=lo+(hi-lo)/2;if(a[m]==target){ans=m;break;}if(a[m]<target)lo=m+1;else hi=m-1;} System.out.println(ans); } }\n",
      "c":"#include <stdio.h>\nint main(void){int n;if(scanf(\"%d\",&n)!=1)return 0;int a[100000];for(int i=0;i<n;i++)scanf(\"%d\",&a[i]);int t;scanf(\"%d\",&t);int l=0,r=n-1,ans=-1;while(l<=r){int m=l+(r-l)/2;if(a[m]==t){ans=m;break;}if(a[m]<t)l=m+1;else r=m-1;}printf(\"%d\\n\",ans);return 0;}\n"}},
    {"id":"factorial_recursion","topic":"Recursion","difficulty":"Hard","title":"Recursive Factorial","statement":"Read n and print n factorial. Implement the calculation with a recursive function.","io":"Input: one integer n. Output: n!.","constraints":"0 <= n <= 12.","examples":[{"input":"5\n","output":"120"}],
     "cases":[("5\n","120"),("0\n","1"),("1\n","1"),("10\n","3628800")],
     "solutions":{
      "python":"import sys\nn=int(sys.stdin.readline())\ndef fact(x): return 1 if x<=1 else x*fact(x-1)\nprint(fact(n))\n",
      "java":"import java.util.*;\npublic class Main { static long fact(int n){return n<=1?1:n*fact(n-1);} public static void main(String[] args){System.out.println(fact(new Scanner(System.in).nextInt()));} }\n",
      "c":"#include <stdio.h>\nlong long fact(int n){return n<=1?1:(long long)n*fact(n-1);}\nint main(void){int n;if(scanf(\"%d\",&n)!=1)return 0;printf(\"%lld\\n\",fact(n));return 0;}\n"}},
    {"id":"linked_list_deduplicate","topic":"Linked List","difficulty":"Medium","title":"Remove Duplicates from a Sorted List","statement":"A sorted linked list is provided as n followed by its node values. Print the values after removing adjacent duplicates while preserving order. The input/output representation is an array; focus on the linked-list traversal logic.","io":"Input: n, then n nondecreasing integers. Output: unique values separated by spaces.","constraints":"0 <= n <= 100000.","examples":[{"input":"7\n1 1 2 3 3 3 5\n","output":"1 2 3 5"}],
     "cases":[("7\n1 1 2 3 3 3 5\n","1 2 3 5"),("0\n",""),("1\n9\n","9"),("4\n-2 -2 0 0\n","-2 0")],
     "solutions":{
      "python":"import sys\na=list(map(int,sys.stdin.buffer.read().split())); n=a[0]; vals=a[1:1+n]; out=[]\nfor x in vals:\n if not out or out[-1]!=x: out.append(x)\nprint(*out)\n",
      "java":"import java.util.*;\npublic class Main { public static void main(String[] args){Scanner s=new Scanner(System.in);int n=s.nextInt();StringBuilder out=new StringBuilder();int prev=0;boolean has=false;for(int i=0;i<n;i++){int x=s.nextInt();if(!has||x!=prev){if(out.length()>0)out.append(' ');out.append(x);prev=x;has=true;}}System.out.println(out);} }\n",
      "c":"#include <stdio.h>\nint main(void){int n;if(scanf(\"%d\",&n)!=1)return 0;int prev=0,has=0,first=1;for(int i=0;i<n;i++){int x;scanf(\"%d\",&x);if(!has||x!=prev){if(!first)putchar(' ');printf(\"%d\",x);first=0;prev=x;has=1;}}putchar('\\n');return 0;}\n"}},
    {"id":"balanced_brackets","topic":"Stack and Queue","difficulty":"Medium","title":"Balanced Brackets","statement":"Read one line containing only (), [] and {}. Print YES when every opening bracket is closed in the correct order; otherwise print NO.","io":"Input: one line of brackets. Output: YES or NO.","constraints":"The line contains at most 10000 bracket characters.","examples":[{"input":"{[()]}\n","output":"YES"}],
     "cases":[("{[()]}\n","YES"),("([)]\n","NO"),("((\n","NO"),("\n","YES")],
     "solutions":{
      "python":"import sys\ns=sys.stdin.readline().strip(); stack=[]; pairs={')':'(',']':'[','}':'{'}; ok=True\nfor ch in s:\n if ch in '([{': stack.append(ch)\n elif ch in pairs:\n  if not stack or stack.pop()!=pairs[ch]: ok=False; break\nprint('YES' if ok and not stack else 'NO')\n",
      "java":"import java.util.*;\npublic class Main { public static void main(String[] args){String s=new Scanner(System.in).nextLine();ArrayDeque<Character> st=new ArrayDeque<>();boolean ok=true;for(char c:s.toCharArray()){if(c=='('||c=='['||c=='{')st.push(c);else if(c==')'||c==']'||c=='}'){if(st.isEmpty()){ok=false;break;}char x=st.pop();if(!((x=='('&&c==')')||(x=='['&&c==']')||(x=='{'&&c=='}'))){ok=false;break;}}}System.out.println(ok&&st.isEmpty()?\"YES\":\"NO\");} }\n",
      "c":"#include <stdio.h>\n#include <string.h>\nint main(void){char s[10005],st[10005];if(!fgets(s,sizeof(s),stdin))return 0;int top=0,ok=1;for(int i=0;s[i]&&s[i]!='\\n';i++){char c=s[i];if(c=='('||c=='['||c=='{')st[top++]=c;else if(c==')'||c==']'||c=='}'){if(!top){ok=0;break;}char x=st[--top];if(!((x=='('&&c==')')||(x=='['&&c==']')||(x=='{'&&c=='}'))){ok=0;break;}}}printf(\"%s\\n\",ok&&top==0?\"YES\":\"NO\");return 0;}\n"}},
    {"id":"rectangle_oop","topic":"Basic OOP","difficulty":"Easy","title":"Rectangle Area","statement":"Read width and height. Define a Rectangle class/record (or struct in C), store the dimensions, and print its area.","io":"Input: width and height. Output: one area value.","constraints":"0 <= width,height <= 100000.","examples":[{"input":"4 6\n","output":"24"}],
     "cases":[("4 6\n","24"),("0 9\n","0"),("7 7\n","49"),("1 12\n","12")],
     "solutions":{
      "python":"import sys\nclass Rectangle:\n def __init__(self,w,h): self.width=w; self.height=h\n def area(self): return self.width*self.height\nw,h=map(int,sys.stdin.read().split()); print(Rectangle(w,h).area())\n",
      "java":"import java.util.*;\nclass Rectangle { int width,height; Rectangle(int w,int h){width=w;height=h;} int area(){return width*height;} }\npublic class Main { public static void main(String[] args){Scanner s=new Scanner(System.in);Rectangle r=new Rectangle(s.nextInt(),s.nextInt());System.out.println(r.area());} }\n",
      "c":"#include <stdio.h>\ntypedef struct {int width;int height;} Rectangle;\nint area(Rectangle r){return r.width*r.height;}\nint main(void){Rectangle r;if(scanf(\"%d %d\",&r.width,&r.height)!=2)return 0;printf(\"%d\\n\",area(r));return 0;}\n"}},
    {"id":"array_max","topic":"Problem Solving","difficulty":"Easy","title":"Find the Maximum","statement":"Read n integers and print the largest value.","io":"Input: n, then n integers. Output: one maximum value.","constraints":"1 <= n <= 100000.","examples":[{"input":"5\n-4 2 9 1 3\n","output":"9"}],
     "cases":[("5\n-4 2 9 1 3\n","9"),("1\n-8\n","-8"),("4\n7 7 2 7\n","7"),("3\n-3 -1 -9\n","-1")],
     "solutions":{
      "python":"import sys\na=list(map(int,sys.stdin.buffer.read().split())); print(max(a[1:1+a[0]]))\n",
      "java":"import java.util.*;\npublic class Main { public static void main(String[] args){Scanner s=new Scanner(System.in);int n=s.nextInt(),m=Integer.MIN_VALUE;for(int i=0;i<n;i++)m=Math.max(m,s.nextInt());System.out.println(m);} }\n",
      "c":"#include <stdio.h>\n#include <limits.h>\nint main(void){int n;if(scanf(\"%d\",&n)!=1)return 0;int m=INT_MIN;for(int i=0;i<n;i++){int x;scanf(\"%d\",&x);if(x>m)m=x;}printf(\"%d\\n\",m);return 0;}\n"}},
]


def connect_store():
    """Use the existing CodeMentor database connection for additive feature tables."""
    return database.get_connection()


def init_store():
    """Create only new Feature 11/12 tables in the already initialized database."""
    conn = database.get_connection()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS interviews (
      id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, language TEXT NOT NULL, difficulty TEXT NOT NULL,
      topic TEXT NOT NULL, duration_minutes INTEGER NOT NULL, question_count INTEGER NOT NULL,
      created_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'in_progress', completed_at INTEGER,
      total_score INTEGER, report_json TEXT,
      FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS interview_questions (
      id INTEGER PRIMARY KEY AUTOINCREMENT, interview_id TEXT NOT NULL, position INTEGER NOT NULL,
      question_id TEXT NOT NULL, code TEXT NOT NULL DEFAULT '', run_passed INTEGER NOT NULL DEFAULT 0,
      run_total INTEGER NOT NULL DEFAULT 0, submitted INTEGER NOT NULL DEFAULT 0,
      score INTEGER NOT NULL DEFAULT 0, passed_tests INTEGER NOT NULL DEFAULT 0,
      total_tests INTEGER NOT NULL DEFAULT 0, approach TEXT NOT NULL DEFAULT '',
      complexity TEXT NOT NULL DEFAULT '', edge_cases TEXT NOT NULL DEFAULT '',
      FOREIGN KEY(interview_id) REFERENCES interviews(id) ON DELETE CASCADE,
      UNIQUE(interview_id,position));
    CREATE INDEX IF NOT EXISTS idx_interviews_user_created ON interviews(user_id,created_at DESC);
    CREATE TABLE IF NOT EXISTS competitions (
      id TEXT PRIMARY KEY, creator_user_id INTEGER NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
      difficulty TEXT NOT NULL, language TEXT NOT NULL, starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
      duration_minutes INTEGER NOT NULL, max_participants INTEGER NOT NULL,
      created_at INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled',
      FOREIGN KEY(creator_user_id) REFERENCES users(id));
    CREATE TABLE IF NOT EXISTS competition_challenges (
      competition_id TEXT PRIMARY KEY, challenge_title TEXT NOT NULL, challenge_description TEXT NOT NULL,
      language TEXT NOT NULL, difficulty TEXT NOT NULL, bug_category TEXT NOT NULL, buggy_code TEXT NOT NULL,
      solution_code TEXT NOT NULL, tests_json TEXT NOT NULL,
      FOREIGN KEY(competition_id) REFERENCES competitions(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS competition_user_challenges (
      id TEXT PRIMARY KEY, owner_user_id INTEGER NOT NULL, title TEXT NOT NULL,
      description TEXT NOT NULL, language TEXT NOT NULL, difficulty TEXT NOT NULL,
      topic TEXT NOT NULL, bug_category TEXT NOT NULL, buggy_code TEXT NOT NULL,
      solution_code TEXT NOT NULL, tests_json TEXT NOT NULL, created_at INTEGER NOT NULL,
      FOREIGN KEY(owner_user_id) REFERENCES users(id));
    CREATE INDEX IF NOT EXISTS idx_competition_user_challenges_owner
      ON competition_user_challenges(owner_user_id, created_at DESC);
    CREATE TABLE IF NOT EXISTS competition_participants (
      competition_id TEXT NOT NULL, user_id INTEGER NOT NULL, joined_at INTEGER NOT NULL,
      started_at INTEGER, finished_at INTEGER, status TEXT NOT NULL DEFAULT 'joined',
      code TEXT, passed_tests INTEGER NOT NULL DEFAULT 0, total_tests INTEGER NOT NULL DEFAULT 0,
      score INTEGER NOT NULL DEFAULT 0, execution_json TEXT,
      identified_category TEXT, bug_explanation TEXT, evaluation_started_at INTEGER,
      PRIMARY KEY(competition_id,user_id),
      FOREIGN KEY(competition_id) REFERENCES competitions(id) ON DELETE CASCADE,
      FOREIGN KEY(user_id) REFERENCES users(id));
    CREATE INDEX IF NOT EXISTS idx_competitions_times ON competitions(starts_at,ends_at);
    CREATE INDEX IF NOT EXISTS idx_competition_participants_rank ON competition_participants(competition_id,score DESC,passed_tests DESC,finished_at ASC);
    CREATE TABLE IF NOT EXISTS competition_rooms (
      competition_id TEXT PRIMARY KEY, room_code TEXT NOT NULL UNIQUE,
      actual_started_at INTEGER, cancelled_at INTEGER,
      FOREIGN KEY(competition_id) REFERENCES competitions(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS competition_rounds (
      competition_id TEXT NOT NULL, position INTEGER NOT NULL,
      challenge_title TEXT NOT NULL, challenge_description TEXT NOT NULL,
      language TEXT NOT NULL, difficulty TEXT NOT NULL, bug_category TEXT NOT NULL,
      buggy_code TEXT NOT NULL, solution_code TEXT NOT NULL, tests_json TEXT NOT NULL,
      PRIMARY KEY(competition_id,position),
      FOREIGN KEY(competition_id) REFERENCES competitions(id) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS competition_round_submissions (
      competition_id TEXT NOT NULL, user_id INTEGER NOT NULL, position INTEGER NOT NULL,
      code TEXT NOT NULL, status TEXT NOT NULL, passed_tests INTEGER NOT NULL DEFAULT 0,
      total_tests INTEGER NOT NULL DEFAULT 0, score INTEGER NOT NULL DEFAULT 0,
      finished_at INTEGER, execution_json TEXT,
      PRIMARY KEY(competition_id,user_id,position),
      FOREIGN KEY(competition_id,position) REFERENCES competition_rounds(competition_id,position) ON DELETE CASCADE,
      FOREIGN KEY(competition_id,user_id) REFERENCES competition_participants(competition_id,user_id) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS idx_competition_round_rank ON competition_round_submissions(competition_id,position,score DESC,finished_at ASC);
    """)
    conn.execute("""INSERT OR IGNORE INTO competition_rounds(competition_id,position,challenge_title,challenge_description,language,difficulty,bug_category,buggy_code,solution_code,tests_json)
        SELECT competition_id,1,challenge_title,challenge_description,language,difficulty,bug_category,buggy_code,solution_code,tests_json FROM competition_challenges""")
    # Additive migration: existing single-challenge rooms remain intact.
    room_columns = {row[1] for row in conn.execute("PRAGMA table_info(competition_participants)")}
    if "ready" not in room_columns:
        conn.execute("ALTER TABLE competition_participants ADD COLUMN ready INTEGER NOT NULL DEFAULT 0")
    if "last_seen_at" not in room_columns:
        conn.execute("ALTER TABLE competition_participants ADD COLUMN last_seen_at INTEGER")
    competition_columns={row[1] for row in conn.execute("PRAGMA table_info(competitions)")}
    if "topic" not in competition_columns:conn.execute("ALTER TABLE competitions ADD COLUMN topic TEXT NOT NULL DEFAULT 'Mixed'")
    if "scoring_rule" not in competition_columns:conn.execute("ALTER TABLE competitions ADD COLUMN scoring_rule TEXT NOT NULL DEFAULT 'balanced'")
    rows = conn.execute("SELECT id FROM competitions WHERE id NOT IN (SELECT competition_id FROM competition_rooms)").fetchall()
    for row in rows:
        code = secrets.token_urlsafe(6).replace("-", "").replace("_", "").upper()[:8]
        while conn.execute("SELECT 1 FROM competition_rooms WHERE room_code=?", (code,)).fetchone():
            code = secrets.token_urlsafe(6).replace("-", "").replace("_", "").upper()[:8]
        conn.execute("INSERT INTO competition_rooms(competition_id,room_code) VALUES(?,?)", (row[0], code))
    conn.commit()
    conn.close()


def _source_ro():
    path = Path(database.DB_PATH).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def validate_owner_session(token):
    if not token or len(token) > 512:
        return None
    import hashlib
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    conn = _source_ro()
    try:
        row = conn.execute("SELECT user_id FROM auth_sessions WHERE token_hash=? AND expires_at>?", (digest, int(time.time()))).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def user_display_name(user_id):
    conn = _source_ro()
    try:
        row = conn.execute("SELECT display_name FROM users WHERE id=?", (int(user_id),)).fetchone()
        return row[0] if row else "Student"
    finally:
        conn.close()


def catalog():
    return {"languages": list(LANGUAGES), "difficulties": list(DIFFICULTIES), "topics": list(TOPICS),
            "durations": list(INTERVIEW_DURATIONS), "question_counts": list(QUESTION_COUNTS),
            "questions": [{k:q[k] for k in ("id","topic","difficulty","title")} for q in QUESTIONS]}


def _select_questions(topic, difficulty, count):
    pool = [q for q in QUESTIONS if (topic == "Mixed" or q["topic"] == topic) and (difficulty == "Mixed" or q["difficulty"] == difficulty)]
    if len(pool) < count:
        return None
    # Rotate deterministically using current time, so repeated sessions can vary without a random dependency.
    offset = int(time.time()) % len(pool)
    pool = pool[offset:] + pool[:offset]
    return pool[:count]


def _public_question(q, code="", position=1, count=1):
    return {"id": q["id"], "position": position, "count": count, "topic": q["topic"], "difficulty": q["difficulty"],
            "title": q["title"], "statement": q["statement"], "io": q["io"], "constraints": q["constraints"],
            "examples": q["examples"], "code": code}


def create_interview(user_id, payload):
    language, difficulty, topic = payload.get("language"), payload.get("difficulty"), payload.get("topic")
    try:
        duration, count = int(payload.get("duration_minutes")), int(payload.get("question_count"))
    except (TypeError, ValueError):
        return {"error":"Choose a supported duration and question count."},400
    if language not in LANGUAGES or difficulty not in (*DIFFICULTIES,"Mixed") or topic not in TOPICS or duration not in INTERVIEW_DURATIONS or count not in QUESTION_COUNTS:
        return {"error":"Invalid interview configuration."},400
    selected = _select_questions(topic, difficulty, count)
    if not selected:
        return {"error":"That topic and difficulty do not have enough different questions. Choose Mixed or a smaller question count."},400
    now=int(time.time()); interview_id=uuid.uuid4().hex
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO interviews(id,user_id,language,difficulty,topic,duration_minutes,question_count,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (interview_id,user_id,language,difficulty,topic,duration,count,now))
        conn.executemany("INSERT INTO interview_questions(interview_id,position,question_id) VALUES(?,?,?)",
                         [(interview_id,i,q["id"]) for i,q in enumerate(selected,1)])
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
    return get_interview(user_id, interview_id),201


def list_interviews(user_id):
    conn=connect_store()
    try:
        _expire_interviews(conn, user_id)
        rows=conn.execute("SELECT id,language,difficulty,topic,duration_minutes,question_count,created_at,status,completed_at,total_score FROM interviews WHERE user_id=? ORDER BY created_at DESC",(user_id,)).fetchall()
        return [dict(r) for r in rows]
    finally: conn.close()


def get_interview(user_id, interview_id):
    conn=connect_store()
    try:
        _expire_interviews(conn, user_id, interview_id)
        interview=conn.execute("SELECT * FROM interviews WHERE id=? AND user_id=?",(interview_id,user_id)).fetchone()
        if not interview:return None
        qs=conn.execute("SELECT * FROM interview_questions WHERE interview_id=? ORDER BY position",(interview_id,)).fetchall()
        qmap={q["id"]:q for q in QUESTIONS}
        now=int(time.time()); limit=interview["duration_minutes"]*60; remaining=max(0,limit-(now-interview["created_at"]))
        public_q=[]
        for row in qs:
            q=qmap[row["question_id"]]
            public_q.append(_public_question(q,row["code"],row["position"],len(qs)) | {"submitted":bool(row["submitted"]),"score":row["score"],"passed_tests":row["passed_tests"],"total_tests":row["total_tests"],"run_passed":row["run_passed"],"run_total":row["run_total"],"approach":row["approach"],"complexity":row["complexity"],"edge_cases":row["edge_cases"]})
        report=json.loads(interview["report_json"] or "null") if interview["report_json"] else None
        return {"id":interview_id,"language":interview["language"],"difficulty":interview["difficulty"],"topic":interview["topic"],"duration_minutes":interview["duration_minutes"],"question_count":interview["question_count"],"created_at":interview["created_at"],"status":interview["status"],"remaining_seconds":remaining,"questions":public_q,"report":report}
    finally: conn.close()


def _build_interview_report(conn, interview_id, status, started_at, completed_at):
    rows=conn.execute("SELECT question_id,score,passed_tests,total_tests,approach,complexity,edge_cases,submitted FROM interview_questions WHERE interview_id=? ORDER BY position",(interview_id,)).fetchall()
    qmap={q["id"]:q for q in QUESTIONS}
    submitted=[r for r in rows if r["submitted"]]
    passed=sum(r["passed_tests"] for r in submitted); total=sum(r["total_tests"] for r in submitted)
    scores=[r["score"] for r in submitted]
    topic_counts={}
    for row in submitted:
        topic=qmap[row["question_id"]]["topic"]
        agg=topic_counts.setdefault(topic,{"questions":0,"passed_tests":0,"total_tests":0,"score_total":0})
        agg["questions"]+=1;agg["passed_tests"]+=row["passed_tests"];agg["total_tests"]+=row["total_tests"];agg["score_total"]+=row["score"]
    topics={topic:{**v,"accuracy":round(100*v["passed_tests"]/v["total_tests"]) if v["total_tests"] else 0,
                   "average_score":round(v["score_total"]/v["questions"]) if v["questions"] else 0} for topic,v in topic_counts.items()}
    strengths=[]; improvements=[]
    if submitted and passed==total:strengths.append("All hidden evaluation cases for submitted questions passed")
    elif submitted:improvements.append("Review the failing evaluation cases and boundary conditions")
    if any(r["approach"].strip() for r in submitted):strengths.append("An approach explanation was recorded")
    else:improvements.append("Write down the key steps before coding")
    if any(r["edge_cases"].strip() for r in submitted):strengths.append("Edge-case notes were recorded")
    else:improvements.append("Record edge cases before submission")
    if not any(r["complexity"].strip() for r in submitted):improvements.append("Practice deriving time and space complexity")
    recommendations=[]
    if passed<total:recommendations.append("Retry a related practice problem and test small and boundary inputs.")
    if any(not r["complexity"].strip() for r in submitted):recommendations.append("For each loop, describe how its work grows with input size.")
    if not recommendations:recommendations.append("Try a higher difficulty or a new topic next.")
    return {"total_score":round(sum(scores)/len(scores)) if scores else 0,
            "questions_completed":len(submitted),"question_count":len(rows),"passed_tests":passed,
            "total_tests":total,"incorrect_tests":total-passed,"time_taken_seconds":max(0,(completed_at or int(time.time()))-started_at),
            "topic_performance":topics,"strengths":strengths,"areas_for_improvement":improvements,
            "recommendations":recommendations,"status":status,
            "scoring_rule":"Score is the percentage of hidden evaluation test cases passed. Approach, code quality, and complexity are recorded for review but are not subjectively scored.",
            "questions":[{"question_id":r["question_id"],"topic":qmap[r["question_id"]]["topic"],"score":r["score"],
                "passed_tests":r["passed_tests"],"total_tests":r["total_tests"],"incorrect_tests":r["total_tests"]-r["passed_tests"],
                "approach":r["approach"],"complexity":r["complexity"],"edge_cases":r["edge_cases"],"submitted":bool(r["submitted"])} for r in rows]}


def _expire_interviews(conn, user_id, interview_id=None):
    now=int(time.time())
    sql="SELECT id,created_at,duration_minutes FROM interviews WHERE user_id=? AND status='in_progress' AND created_at+duration_minutes*60<=?"
    args=[user_id,now]
    if interview_id:
        sql+=" AND id=?";args.append(interview_id)
    for row in conn.execute(sql,args).fetchall():
        deadline=row["created_at"]+row["duration_minutes"]*60
        report=_build_interview_report(conn,row["id"],"Expired",row["created_at"],deadline)
        conn.execute("UPDATE interviews SET status='expired',completed_at=?,total_score=?,report_json=? WHERE id=? AND user_id=? AND status='in_progress'",
                     (deadline,report["total_score"],json.dumps(report),row["id"],user_id))
    if conn.in_transaction:conn.commit()


def save_interview_code(user_id, interview_id, position, code):
    if not isinstance(code,str) or len(code)>50000:return {"error":"Code must be 50,000 characters or fewer."},400
    conn=connect_store()
    try:
        row=conn.execute("SELECT i.status,i.created_at,i.duration_minutes,q.submitted FROM interviews i JOIN interview_questions q ON q.interview_id=i.id WHERE i.id=? AND i.user_id=? AND q.position=?",(interview_id,user_id,position)).fetchone()
        if not row:return {"error":"Interview question not found."},404
        if row["status"]!="in_progress" or int(time.time())>row["created_at"]+row["duration_minutes"]*60:return {"error":"This interview has ended."},409
        if row["submitted"]:return {"error":"This question was already submitted."},409
        conn.execute("UPDATE interview_questions SET code=? WHERE interview_id=? AND position=?",(code,interview_id,position)); conn.commit()
        return {"saved":True},200
    finally:conn.close()


def _get_question_row(conn,user_id,interview_id,position):
    return conn.execute("SELECT i.language,i.status,i.created_at,i.duration_minutes,q.* FROM interviews i JOIN interview_questions q ON q.interview_id=i.id WHERE i.id=? AND i.user_id=? AND q.position=?",(interview_id,user_id,position)).fetchone()


def interview_run(user_id, interview_id, position, payload, submit=False):
    code=payload.get("code")
    if not isinstance(code,str) or not code.strip() or len(code)>50000:return {"error":"Enter code (up to 50,000 characters)."},400
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row=_get_question_row(conn,user_id,interview_id,position)
        if not row:return {"error":"Interview question not found."},404
        now=int(time.time())
        if row["status"]!="in_progress" or now>row["created_at"]+row["duration_minutes"]*60:return {"error":"This interview has ended."},409
        if submit and row["submitted"]:return {"error":"This question was already submitted."},409
        q=next(q for q in QUESTIONS if q["id"]==row["question_id"])
        tests=[{"input":i,"expected_output":o,"description":f"Case {n+1}"} for n,(i,o) in enumerate(q["cases"])]
        visible_tests=[{"input":e["input"],"expected_output":e["output"],"description":"Example"} for e in q["examples"]]
        result=code_executor.execute_code(code,row["language"],tests if submit else visible_tests)
        passed=int(result.get("passed_tests") or 0); total=len(tests) if submit else len(visible_tests)
        run_json={"is_compiled":bool(result.get("is_compiled")),"compile_error":result.get("compile_error"),"passed_tests":passed,"total_tests":total,
                  "all_passed":bool(result.get("all_passed")),"execution_type":"verified_execution"}
        if submit:
            approach=str(payload.get("approach") or "")[:4000]; complexity=str(payload.get("complexity") or "")[:500]; edge=str(payload.get("edge_cases") or "")[:2000]
            score=round(100*passed/max(1,total))
            conn.execute("UPDATE interview_questions SET code=?,submitted=1,score=?,passed_tests=?,total_tests=?,approach=?,complexity=?,edge_cases=? WHERE id=?",
                         (code,score,passed,total,approach,complexity,edge,row["id"]))
            all_done=conn.execute("SELECT MIN(submitted) FROM interview_questions WHERE interview_id=?",(interview_id,)).fetchone()[0]
            report=None
            if all_done:
                report=_build_interview_report(conn,interview_id,"Completed",row["created_at"],now)
                conn.execute("UPDATE interviews SET status='completed',completed_at=?,total_score=?,report_json=? WHERE id=?",(now,report["total_score"],json.dumps(report),interview_id))
            conn.commit()
            return {"result":run_json,"question_score":score,"report":report},200
        conn.execute("UPDATE interview_questions SET code=?,run_passed=?,run_total=? WHERE id=?",(code,passed,total,row["id"]))
        conn.commit()
        # Do not return expected outputs for evaluation cases.
        return {"result":run_json,"visible_results":[{"description":x.get("description"),"passed":bool(x.get("passed")),"actual_output":x.get("actual_output"),"error":x.get("error")} for x in result.get("test_results",[])]},200
    except Exception:
        conn.rollback(); raise
    finally:conn.close()


def interview_question_ai(user_id, interview_id, position, payload):
    session=get_interview(user_id,interview_id)
    if not session:return {"error":"Interview not found."},404
    if session["status"]!="in_progress" or session["remaining_seconds"]<=0:return {"error":"This interview has ended."},409
    question=next((q for q in session["questions"] if q["position"]==position),None)
    if not question:return {"error":"Interview question not found."},404
    kind=payload.get("kind")
    if kind not in ("approach","complexity","edge_case","hint"):return {"error":"Choose a supported follow-up type."},400
    prompts={"approach":"Ask the candidate one interview-style follow-up question about their approach. Do not give a solution or code.","complexity":"Ask one question that helps the candidate analyze time or space complexity. Do not give the answer.","edge_case":"Ask one focused edge-case question. Do not disclose hidden tests or expected outputs.","hint":"Give one brief conceptual hint only because the candidate requested it. Do not provide code, exact steps, expected outputs, or a complete solution."}
    try:
        response=tutor_engine.generate_ai(
            "Question: "+question["statement"]+"\nCandidate code (may be partial):\n"+str(payload.get("code") or "")[:5000]+"\nCandidate explanation:\n"+str(payload.get("explanation") or "")[:1500],
            prompts[kind]+" Keep the response to one question. Never reveal a complete solution.",
            "interview_assistant")
        if response.get("text"):
            candidate=str(response["text"]).strip()
            # Keep interviewer turns short and reject code or answer-shaped output.
            q_spec=next(q for q in QUESTIONS if q["id"]==question["id"])
            solution=q_spec["solutions"].get(session["language"],q_spec["solutions"].get("c" if session["language"]=="cpp" else session["language"],""))
            solution_lines=[line.strip() for line in solution.splitlines() if len(line.strip())>=28]
            leaks_solution=any(line in candidate for line in solution_lines)
            answer_shaped=(len(candidate)>500 or "```" in candidate or leaks_solution or (kind!="hint" and "?" not in candidate))
            if not answer_shaped:
                return {"question":candidate,"provider":response.get("provider","local"),"ai_available":response.get("provider")!="local"},200
    except Exception:
        pass
    local={"approach":"What are the main steps your solution follows, and why does each step help solve the problem?","complexity":"How does your running time grow when the input size doubles? Which part of your code determines that?","edge_case":"What should happen for the smallest valid input, and how does your code handle it?","hint":"Start by writing down the information the program must keep track of. Which part of the input should be examined first?"}
    return {"question":local[kind],"provider":"local","ai_available":False},200


def interview_report(user_id, interview_id):
    session=get_interview(user_id,interview_id)
    if not session:return None
    if session["report"]:return session["report"]
    return {"status":session["status"],"questions_completed":sum(q["submitted"] for q in session["questions"]),"question_count":session["question_count"],"remaining_seconds":session["remaining_seconds"]}


def create_user_competition_challenge(user_id, payload):
    """Save a host-authored challenge only after both buggy and fixed code are validated."""
    title=str(payload.get("title") or "").strip()
    description=str(payload.get("description") or "").strip()
    topic=str(payload.get("topic") or "").strip()
    language=payload.get("language")
    difficulty=payload.get("difficulty")
    category=payload.get("bug_category")
    buggy=str(payload.get("buggy_code") or "")
    solution=str(payload.get("solution_code") or "")
    tests=payload.get("tests")
    if (not title or len(title)>100 or not description or len(description)>2000 or topic not in TOPICS
            or language not in LANGUAGES or difficulty not in DIFFICULTIES
            or category not in ("syntax_errors","incorrect_conditions","array_indexing","off_by_one","loop_logic",
                "recursion_misunderstanding","variable_scope","null_pointer_errors","string_handling_errors",
                "function_method_errors","data_structure_errors","algorithm_logic")
            or not buggy.strip() or len(buggy)>20000 or not solution.strip() or len(solution)>20000):
        return {"error":"Enter a title, description, supported language/difficulty/topic, bug category, and both code versions (each up to 20,000 characters)."},400
    if not isinstance(tests,list) or not 2<=len(tests)<=20 or not all(
        isinstance(test,dict) and isinstance(test.get("input"),str) and isinstance(test.get("expected_output"),str)
        and len(test["input"])<=5000 and len(test["expected_output"])<=2000 for test in tests):
        return {"error":"Provide 2–20 test cases as JSON objects with string input and expected_output fields."},400
    reference=code_executor.execute_code(solution,language,tests)
    buggy_result=code_executor.execute_code(buggy,language,tests)
    invalid_bug=(buggy_result.get("is_compiled") if category=="syntax_errors" else
                 (not buggy_result.get("is_compiled") or buggy_result.get("all_passed")))
    if not reference.get("is_compiled") or not reference.get("all_passed") or invalid_bug:
        return {"error":"Validation failed: corrected code must compile and pass every test; buggy code must show the selected bug."},400
    challenge_id=uuid.uuid4().hex
    conn=connect_store()
    try:
        conn.execute("INSERT INTO competition_user_challenges(id,owner_user_id,title,description,language,difficulty,topic,bug_category,buggy_code,solution_code,tests_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (challenge_id,user_id,title,description,language,difficulty,topic,category,buggy,solution,json.dumps(tests),int(time.time())))
        conn.commit()
    finally:conn.close()
    return {"challenge":{"id":f"user:{challenge_id}","title":title,"language":language,"difficulty":difficulty,"topic":topic,"test_count":len(tests),"validated":True}},201


def _competition_challenge(selection, language, difficulty, user_id=None):
    if not isinstance(selection, str):
        selection = str(selection or "")
    if selection.startswith("curated:"):
        parts = selection.split(":")
        if len(parts) != 3 or parts[1] != language or parts[2] != difficulty or _challenge_factory is None:
            return None
        generator_language = "c" if language == "cpp" else language
        challenge = _challenge_factory(generator_language, difficulty)
        if not challenge:
            return None
        if _challenge_formatter:
            code_language = "python" if language == "python" else language
            challenge["buggy_code"] = _challenge_formatter(challenge["buggy_code"], code_language)
            challenge["solution_code"] = _challenge_formatter(challenge["solution_code"], code_language)
        return {"title":challenge["title"],"description":challenge["description"],"language":language,
                "difficulty":difficulty,"concept":challenge.get("concept","Mixed"),"bug_category":challenge["mistake_category_slug"],
                "buggy_code":challenge["buggy_code"],"solution_code":challenge["solution_code"],
                "tests":challenge["test_cases"]}
    if selection.startswith("problem:"):
        try: problem_id = int(selection.split(":",1)[1])
        except ValueError: return None
        source = _source_ro()
        try:
            row = source.execute("SELECT id,title,description,language,difficulty,concept,buggy_code,solution_code,test_cases_json,mistake_category_slug FROM problems WHERE id=?",(problem_id,)).fetchone()
        finally: source.close()
        if not row or not (row["language"] == language or (language == "cpp" and row["language"] == "c")):
            return None
        level = str(row["difficulty"]).casefold()
        allowed = {"easy":{"easy","beginner"},"medium":{"medium","intermediate"},"hard":{"hard"}}[difficulty.casefold()]
        if level not in allowed: return None
        try: tests = json.loads(row["test_cases_json"] or "[]")
        except (TypeError,ValueError): return None
        return {"title":row["title"],"description":row["description"],"language":language,"difficulty":difficulty,"concept":row["concept"],
                "bug_category":row["mistake_category_slug"],"buggy_code":row["buggy_code"],
                "solution_code":row["solution_code"],"tests":tests}
    if selection.startswith("user:") and user_id is not None:
        challenge_id=selection.split(":",1)[1]
        if not re_full_uuid(challenge_id):return None
        conn=connect_store()
        try:
            row=conn.execute("SELECT title,description,language,difficulty,topic,bug_category,buggy_code,solution_code,tests_json FROM competition_user_challenges WHERE id=? AND owner_user_id=?",(challenge_id,user_id)).fetchone()
        finally:conn.close()
        if not row or row["language"]!=language or row["difficulty"]!=difficulty:return None
        try:tests=json.loads(row["tests_json"])
        except (TypeError,ValueError):return None
        return {"title":row["title"],"description":row["description"],"language":language,"difficulty":difficulty,"concept":row["topic"],
                "bug_category":row["bug_category"],"buggy_code":row["buggy_code"],"solution_code":row["solution_code"],"tests":tests}
    return None


def re_full_uuid(value):
    return isinstance(value,str) and len(value)==32 and all(ch in "0123456789abcdef" for ch in value)


def create_competition(creator_id,payload):
    title=str(payload.get("title") or "").strip(); description=str(payload.get("description") or "").strip()
    try:
        selections=payload.get("challenge_ids")
        if not isinstance(selections,list):selections=[payload.get("challenge_id") or ""]
        selections=[str(x) for x in selections]
        start=int(payload.get("starts_at")); end=int(payload.get("ends_at")); duration=int(payload.get("duration_minutes")); maximum=int(payload.get("max_participants"))
    except (TypeError,ValueError):return {"error":"Enter a valid challenge and competition schedule."},400
    language=payload.get("language"); difficulty=payload.get("difficulty");topic=str(payload.get("topic") or "Mixed");scoring_rule=str(payload.get("scoring_rule") or "balanced")
    if not title or len(title)>100 or len(description)>2000:return {"error":"Title (1-100 characters) and description (up to 2,000 characters) are required."},400
    if language not in LANGUAGES or difficulty not in DIFFICULTIES or duration<1 or duration>180 or maximum<2 or maximum>100 or start<int(time.time()) or end<=start or end-start>30*86400 or scoring_rule not in ("balanced","tests_only"):return {"error":"Invalid language, difficulty, schedule, duration, participant limit (2–100), or scoring rule."},400
    if not 1<=len(selections)<=5 or len(set(selections))!=len(selections):return {"error":"Choose between one and five different challenges."},400
    problems=[]
    for selection in selections:
        problem=(_competition_challenge(selection,language,difficulty,creator_id) if selection.startswith("user:")
                 else _competition_challenge(selection,language,difficulty))
        tests=problem.get("tests") if problem else None
        if not problem or not isinstance(tests,list) or not 1<=len(tests)<=30 or not all(isinstance(t,dict) and "input" in t and "expected_output" in t for t in tests):return {"error":"Choose validated challenges matching the selected language and difficulty."},400
        if topic!="Mixed" and str(problem.get("concept") or "Mixed").casefold()!=topic.casefold():return {"error":"Selected challenges must match the chosen topic."},400
        reference=code_executor.execute_code(problem["solution_code"],language,tests)
        buggy=code_executor.execute_code(problem["buggy_code"],language,tests)
        if not reference.get("is_compiled") or not reference.get("all_passed") or (buggy.get("all_passed") or (problem["bug_category"]=="syntax_errors" and buggy.get("is_compiled")) or (problem["bug_category"]!="syntax_errors" and not buggy.get("is_compiled"))):
            return {"error":"A selected challenge did not pass validation; choose another catalog challenge."},409
        if any(p["buggy_code"]==problem["buggy_code"] or (p["title"]==problem["title"] and p["bug_category"]==problem["bug_category"]) for p in problems):return {"error":"Selected challenges must use different code and bug patterns."},409
        problems.append(problem)
    problem=problems[0];tests=problem["tests"]
    comp_id=uuid.uuid4().hex
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO competitions(id,creator_user_id,title,description,difficulty,language,starts_at,ends_at,duration_minutes,max_participants,created_at,topic,scoring_rule) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (comp_id,creator_id,title,description,difficulty,language,start,end,duration,maximum,int(time.time()),topic,scoring_rule))
        conn.execute("INSERT INTO competition_challenges VALUES(?,?,?,?,?,?,?,?,?)",(comp_id,problem["title"],problem["description"],language,difficulty,problem["bug_category"],problem["buggy_code"],problem["solution_code"],json.dumps(tests)))
        room_code = secrets.token_urlsafe(6).replace("-", "").replace("_", "").upper()[:8]
        while conn.execute("SELECT 1 FROM competition_rooms WHERE room_code=?", (room_code,)).fetchone():
            room_code = secrets.token_urlsafe(6).replace("-", "").replace("_", "").upper()[:8]
        conn.execute("INSERT INTO competition_rooms(competition_id,room_code) VALUES(?,?)", (comp_id, room_code))
        conn.executemany("INSERT INTO competition_rounds VALUES(?,?,?,?,?,?,?,?,?,?)",[(comp_id,pos,p["title"],p["description"],language,difficulty,p["bug_category"],p["buggy_code"],p["solution_code"],json.dumps(p["tests"])) for pos,p in enumerate(problems,1)])
        conn.commit()
    except Exception:conn.rollback();raise
    finally:conn.close()
    return {"competition":public_competition(comp_id,include_details=True)},201


def public_competition(comp_id,include_details=False):
    conn=connect_store()
    try:
        c=conn.execute("SELECT id,title,description,difficulty,language,starts_at,ends_at,duration_minutes,max_participants,created_at,topic,scoring_rule FROM competitions WHERE id=?",(comp_id,)).fetchone()
        if not c:return None
        count=conn.execute("SELECT COUNT(*) FROM competition_participants WHERE competition_id=? AND status NOT IN ('left','removed','cancelled')",(comp_id,)).fetchone()[0]
        result=dict(c); result["participants"]=count; result["status"]="scheduled" if time.time()<c["starts_at"] else "active" if time.time()<c["ends_at"] else "ended"
        room=conn.execute("SELECT room_code,actual_started_at,cancelled_at FROM competition_rooms WHERE competition_id=?",(comp_id,)).fetchone()
        if room:
            if include_details: result["room_code"]=room["room_code"]
            result["actual_started_at"]=room["actual_started_at"]
            if room["cancelled_at"]: result["status"]="cancelled"
            elif room["actual_started_at"]: result["status"]="active" if time.time()<min(c["ends_at"],room["actual_started_at"]+c["duration_minutes"]*60) else "ended"
        result["challenge_count"]=conn.execute("SELECT COUNT(*) FROM competition_rounds WHERE competition_id=?",(comp_id,)).fetchone()[0] or 1
        host=_source_ro()
        try:
            name=host.execute("SELECT display_name FROM users WHERE id=(SELECT creator_user_id FROM competitions WHERE id=?)",(comp_id,)).fetchone()
            result["host_name"]=name[0] if name else "Host"
        finally: host.close()
        if include_details:
            ch=conn.execute("SELECT challenge_title,challenge_description FROM competition_challenges WHERE competition_id=?",(comp_id,)).fetchone()
            result["challenge"]={"title":ch[0],"description":ch[1]} if ch else None
        return result
    finally:conn.close()


def list_competitions():
    conn=connect_store()
    try:ids=[r[0] for r in conn.execute("SELECT id FROM competitions ORDER BY starts_at DESC")]
    finally:conn.close()
    results=[public_competition(i,True) for i in ids]
    for item in results:
        if item:item.pop("room_code",None)
    return results


def can_view_competition_room_code(user_id,comp_id):
    conn=connect_store()
    try:return bool(conn.execute("SELECT 1 FROM competitions c LEFT JOIN competition_participants p ON p.competition_id=c.id AND p.user_id=? WHERE c.id=? AND (c.creator_user_id=? OR p.user_id IS NOT NULL)",(user_id,comp_id,user_id)).fetchone())
    finally:conn.close()


def join_competition(user_id,comp_id):
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        comp=conn.execute("SELECT starts_at,ends_at,max_participants FROM competitions WHERE id=?",(comp_id,)).fetchone()
        if not comp:conn.rollback();return {"error":"Competition not found."},404
        now=int(time.time())
        if now>=comp["ends_at"]:conn.rollback();return {"error":"Competition registration has ended."},409
        existing=conn.execute("SELECT status FROM competition_participants WHERE competition_id=? AND user_id=?",(comp_id,user_id)).fetchone()
        if existing:conn.rollback();return {"error":"You already joined this competition."},409
        room_state=conn.execute("SELECT actual_started_at,cancelled_at FROM competition_rooms WHERE competition_id=?",(comp_id,)).fetchone()
        if room_state and room_state["cancelled_at"]:conn.rollback();return {"error":"This competition room was cancelled."},410
        if room_state and room_state["actual_started_at"]:conn.rollback();return {"error":"This competition has already started."},409
        count=conn.execute("SELECT COUNT(*) FROM competition_participants WHERE competition_id=? AND status NOT IN ('left','removed','cancelled')",(comp_id,)).fetchone()[0]
        if count>=comp["max_participants"]:conn.rollback();return {"error":"This competition is full."},409
        conn.execute("INSERT INTO competition_participants(competition_id,user_id,joined_at,last_seen_at) VALUES(?,?,?,?)",(comp_id,user_id,now,now));conn.commit()
        return {"joined":True,"competition_id":comp_id},201
    finally:conn.close()


def join_competition_code(user_id, room_code):
    code = str(room_code or "").strip().upper()
    if not 6 <= len(code) <= 12:
        return {"error":"Enter a valid room code."},400
    conn=connect_store()
    try:
        row=conn.execute("SELECT competition_id FROM competition_rooms WHERE room_code=?",(code,)).fetchone()
        if not row:return {"error":"Competition room was not found."},404
        comp_id=row[0]
    finally:conn.close()
    return join_competition(user_id,comp_id)


def preview_competition_code(room_code):
    code=str(room_code or "").strip().upper()
    conn=connect_store()
    try:
        row=conn.execute("SELECT competition_id FROM competition_rooms WHERE room_code=?",(code,)).fetchone()
        if not row:return None
        comp_id=row[0]
        result=public_competition(comp_id,True)
        if not result:return None
        room=conn.execute("SELECT actual_started_at,cancelled_at FROM competition_rooms WHERE competition_id=?",(comp_id,)).fetchone()
        result["can_join"]=not room["actual_started_at"] and not room["cancelled_at"] and time.time()<result["ends_at"] and result["participants"]<result["max_participants"]
        return result
    finally:conn.close()


def competition_lobby(user_id,comp_id):
    conn=connect_store()
    try:
        now=int(time.time())
        conn.execute("UPDATE competition_participants SET last_seen_at=? WHERE competition_id=? AND user_id=? AND (last_seen_at IS NULL OR last_seen_at<?)",(now,comp_id,user_id,now-10))
        conn.commit()
        comp=conn.execute("SELECT creator_user_id FROM competitions WHERE id=?",(comp_id,)).fetchone()
        if not comp:return None
        mine=conn.execute("SELECT 1 FROM competition_participants WHERE competition_id=? AND user_id=? AND status NOT IN ('left','removed','cancelled')",(comp_id,user_id)).fetchone()
        if not mine and comp[0] != user_id:return None
        room=conn.execute("SELECT room_code,actual_started_at,cancelled_at FROM competition_rooms WHERE competition_id=?",(comp_id,)).fetchone()
        rows=conn.execute("SELECT p.user_id,p.status,p.ready,p.joined_at,p.last_seen_at FROM competition_participants p WHERE p.competition_id=? ORDER BY p.joined_at",(comp_id,)).fetchall()
        challenge_count=conn.execute("SELECT COUNT(*) FROM competition_rounds WHERE competition_id=?",(comp_id,)).fetchone()[0] or 1
        starts=room["actual_started_at"] if room else None
        state={"competition":public_competition(comp_id,True),"room_code":room["room_code"] if room else None,"host":int(comp[0]),"is_host":int(comp[0])==int(user_id),"started_at":starts,"cancelled":bool(room["cancelled_at"]) if room else False,"challenge_count":challenge_count,"participants":[]}
        ids=[r["user_id"] for r in rows]
        source=_source_ro()
        try:names={r[0]:r[1] for r in source.execute("SELECT id,display_name FROM users WHERE id IN (%s)" % (",".join("?" for _ in ids) or "NULL"),ids)} if ids else {}
        finally:source.close()
        state["participants"]=[{"user_id":r["user_id"],"display_name":names.get(r["user_id"],"Student"),"status":r["status"],"ready":bool(r["ready"]),"online":r["status"] not in ("left","removed","cancelled") and bool(r["last_seen_at"] and int(time.time())-r["last_seen_at"]<20)} for r in rows]
        return state
    finally:conn.close()


def set_competition_ready(user_id,comp_id,ready):
    conn=connect_store()
    try:
        cur=conn.execute("UPDATE competition_participants SET ready=?,last_seen_at=? WHERE competition_id=? AND user_id=? AND status='joined'",(1 if ready else 0,int(time.time()),comp_id,user_id));conn.commit()
        if not cur.rowcount:return {"error":"Join this waiting room before changing ready status."},404
        return {"ready":bool(ready)},200
    finally:conn.close()


def start_competition_room(user_id,comp_id):
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        comp=conn.execute("SELECT creator_user_id,starts_at,ends_at,duration_minutes FROM competitions WHERE id=?",(comp_id,)).fetchone()
        room=conn.execute("SELECT actual_started_at,cancelled_at FROM competition_rooms WHERE competition_id=?",(comp_id,)).fetchone()
        if not comp or int(comp["creator_user_id"])!=int(user_id):conn.rollback();return {"error":"Only the competition host can start this room."},403
        if room["cancelled_at"]:conn.rollback();return {"error":"This room is cancelled."},410
        if room["actual_started_at"]:conn.rollback();return {"error":"Competition already started."},409
        count=conn.execute("SELECT COUNT(*) FROM competition_participants WHERE competition_id=? AND status NOT IN ('left','removed','cancelled')",(comp_id,)).fetchone()[0]
        if count<2:conn.rollback();return {"error":"At least two participants must join before the host starts."},409
        now=int(time.time())
        if now>=comp["ends_at"]:conn.rollback();return {"error":"This competition registration window has ended."},409
        conn.execute("UPDATE competition_rooms SET actual_started_at=? WHERE competition_id=? AND actual_started_at IS NULL",(now,comp_id))
        conn.execute("UPDATE competition_participants SET started_at=?,status='in_progress' WHERE competition_id=? AND status='joined'",(now,comp_id))
        conn.commit()
        return {"started_at":now,"duration_seconds":comp["duration_minutes"]*60},200
    finally:conn.close()


def leave_competition(user_id,comp_id):
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room=conn.execute("SELECT actual_started_at FROM competition_rooms WHERE competition_id=?",(comp_id,)).fetchone()
        if not room:conn.rollback();return {"error":"Competition not found."},404
        if room[0]:conn.rollback();return {"error":"Participants cannot leave after the competition starts."},409
        cur=conn.execute("UPDATE competition_participants SET status='left',ready=0,finished_at=? WHERE competition_id=? AND user_id=? AND status='joined'",(int(time.time()),comp_id,user_id));conn.commit()
        if not cur.rowcount:return {"error":"No waiting-room participation to leave."},404
        return {"left":True},200
    finally:conn.close()


def manage_competition_room(user_id,comp_id,action,target_user_id=None):
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        comp=conn.execute("SELECT creator_user_id FROM competitions WHERE id=?",(comp_id,)).fetchone()
        room=conn.execute("SELECT actual_started_at,cancelled_at FROM competition_rooms WHERE competition_id=?",(comp_id,)).fetchone()
        if not comp or not room:conn.rollback();return {"error":"Competition not found."},404
        if int(comp[0])!=int(user_id):conn.rollback();return {"error":"Only the host can manage this room."},403
        if room["actual_started_at"]:conn.rollback();return {"error":"Room management is locked after the competition starts."},409
        if action=="cancel":
            conn.execute("UPDATE competition_rooms SET cancelled_at=? WHERE competition_id=?",(int(time.time()),comp_id))
            conn.execute("UPDATE competition_participants SET status='cancelled',finished_at=? WHERE competition_id=? AND status='joined'",(int(time.time()),comp_id))
        elif action=="remove" and target_user_id is not None and int(target_user_id)!=int(user_id):
            cur=conn.execute("UPDATE competition_participants SET status='removed',ready=0,finished_at=? WHERE competition_id=? AND user_id=? AND status='joined'",(int(time.time()),comp_id,int(target_user_id)))
            if not cur.rowcount:conn.rollback();return {"error":"Waiting participant not found."},404
        else:conn.rollback();return {"error":"Unsupported room action."},400
        conn.commit();return {"updated":True},200
    finally:conn.close()


def start_competition_challenge(user_id,comp_id):
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT c.starts_at,c.ends_at,c.duration_minutes,c.language,c.difficulty,ch.challenge_title,ch.challenge_description,ch.buggy_code,p.status,p.started_at,r.actual_started_at FROM competitions c JOIN competition_challenges ch ON ch.competition_id=c.id JOIN competition_participants p ON p.competition_id=c.id LEFT JOIN competition_rooms r ON r.competition_id=c.id WHERE c.id=? AND p.user_id=?",(comp_id,user_id)).fetchone()
        if not row:conn.rollback();return {"error":"Join this competition before starting its challenge."},404
        now=int(time.time())
        if row["actual_started_at"]:
            if now>=min(row["ends_at"],row["actual_started_at"]+row["duration_minutes"]*60):conn.rollback();return {"error":"The competition is not active."},409
        elif not row["starts_at"]<=now<row["ends_at"]:conn.rollback();return {"error":"The competition is not active."},409
        if row["status"]=="submitted":conn.rollback();return {"error":"Your submission is already locked."},409
        if row["status"]=="evaluating":conn.rollback();return {"error":"Your submission is being evaluated."},409
        if row["started_at"] and now>=min(row["ends_at"],row["started_at"]+row["duration_minutes"]*60):
            deadline=min(row["ends_at"],row["started_at"]+row["duration_minutes"]*60)
            conn.execute("UPDATE competition_participants SET status='timed_out',finished_at=? WHERE competition_id=? AND user_id=?",(deadline,comp_id,user_id));conn.commit()
            return {"error":"Your competition timer has expired."},410
        if not row["started_at"]:conn.execute("UPDATE competition_participants SET started_at=?,status='in_progress' WHERE competition_id=? AND user_id=?",(row["actual_started_at"] or now,comp_id,user_id));conn.commit()
        else:conn.commit()
        source=_source_ro()
        try: categories=[r[0] for r in source.execute("SELECT DISTINCT mistake_category_slug FROM problems ORDER BY mistake_category_slug")]
        finally:source.close()
        return {"competition_id":comp_id,"title":row["challenge_title"],"description":row["challenge_description"],"language":row["language"],"difficulty":row["difficulty"],"buggy_code":row["buggy_code"],"categories":categories,"remaining_seconds":max(0,min(row["duration_minutes"]*60-(now-(row["started_at"] or row["actual_started_at"] or now)),row["ends_at"]-now))},200
    finally:conn.close()


def run_competition_sample(user_id,comp_id,code):
    if not isinstance(code,str) or not code.strip() or len(code)>50000:return {"error":"Enter code (up to 50,000 characters)."},400
    conn=connect_store()
    try:
        row=conn.execute("SELECT c.ends_at,c.duration_minutes,ch.language,ch.tests_json,p.started_at,p.status FROM competitions c JOIN competition_challenges ch ON ch.competition_id=c.id JOIN competition_participants p ON p.competition_id=c.id WHERE c.id=? AND p.user_id=?",(comp_id,user_id)).fetchone()
        if not row:return {"error":"Competition participation not found."},404
        now=int(time.time())
        if not row["started_at"] or row["status"] not in ("in_progress","joined"):return {"error":"Start the competition before running code."},409
        if now>=row["ends_at"] or now>=row["started_at"]+row["duration_minutes"]*60:return {"error":"Competition time has ended."},410
        tests=json.loads(row["tests_json"] or "[]")
        if not tests:return {"error":"No visible sample is available."},409
        sample=tests[0]
        language=row["language"]
    finally:conn.close()
    result=code_executor.execute_code(code,language,[sample])
    actual=(result.get("test_results") or [{}])[0].get("actual_output","")
    return {"is_compiled":bool(result.get("is_compiled")),"compile_error":result.get("compile_error"),
        "passed":bool(result.get("all_passed")),"actual_output":actual,
        "sample_input":sample.get("input",""),"expected_output":sample.get("expected_output","")},200


def get_competition_round(user_id,comp_id,position):
    try:position=int(position)
    except (TypeError,ValueError):return {"error":"Invalid challenge number."},400
    conn=connect_store()
    try:
        row=conn.execute("SELECT c.ends_at,c.duration_minutes,c.language,c.difficulty,r.challenge_title,r.challenge_description,r.buggy_code,p.started_at,p.status,s.status AS round_status FROM competitions c JOIN competition_rounds r ON r.competition_id=c.id JOIN competition_participants p ON p.competition_id=c.id LEFT JOIN competition_round_submissions s ON s.competition_id=c.id AND s.user_id=p.user_id AND s.position=r.position WHERE c.id=? AND p.user_id=? AND r.position=?",(comp_id,user_id,position)).fetchone()
        if not row:return {"error":"Challenge not found for this participant."},404
        room=conn.execute("SELECT actual_started_at FROM competition_rooms WHERE competition_id=?",(comp_id,)).fetchone()
        started=row["started_at"] or (room[0] if room else None)
        if not started:return {"error":"The host has not started the competition."},409
        now=int(time.time());remaining=max(0,min(row["ends_at"]-now,row["duration_minutes"]*60-(now-started)))
        if remaining<=0:return {"error":"Competition time has ended."},410
        source=_source_ro()
        try:categories=[r[0] for r in source.execute("SELECT DISTINCT mistake_category_slug FROM problems ORDER BY mistake_category_slug")]
        finally:source.close()
        return {"competition_id":comp_id,"position":position,"count":conn.execute("SELECT COUNT(*) FROM competition_rounds WHERE competition_id=?",(comp_id,)).fetchone()[0],"title":row["challenge_title"],"description":row["challenge_description"],"language":row["language"],"difficulty":row["difficulty"],"buggy_code":row["buggy_code"],"categories":categories,"status":row["round_status"] or "not_submitted","remaining_seconds":remaining},200
    finally:conn.close()


def run_competition_round_sample(user_id,comp_id,position,code):
    if not isinstance(code,str) or not code.strip() or len(code)>50000:return {"error":"Enter code (up to 50,000 characters)."},400
    try:position=int(position)
    except (TypeError,ValueError):return {"error":"Invalid challenge number."},400
    conn=connect_store()
    try:
        row=conn.execute("SELECT c.ends_at,c.duration_minutes,r.language,r.tests_json,p.started_at,rs.status FROM competitions c JOIN competition_rounds r ON r.competition_id=c.id JOIN competition_participants p ON p.competition_id=c.id LEFT JOIN competition_round_submissions rs ON rs.competition_id=c.id AND rs.user_id=p.user_id AND rs.position=r.position WHERE c.id=? AND p.user_id=? AND r.position=?",(comp_id,user_id,position)).fetchone()
        if not row:return {"error":"Challenge not found for this participant."},404
        if not row["started_at"]:return {"error":"Start the competition first."},409
        if row["status"] in ("evaluating","submitted"):return {"error":"This challenge is already locked."},409
        now=int(time.time())
        if now>=row["ends_at"] or now>=row["started_at"]+row["duration_minutes"]*60:return {"error":"Competition time has ended."},410
        cases=json.loads(row["tests_json"] or "[]")
        if not cases:return {"error":"No visible sample is available."},409
        sample=cases[0];language=row["language"]
    finally:conn.close()
    result=code_executor.execute_code(code,language,[sample]);item=(result.get("test_results") or [{}])[0]
    return {"is_compiled":bool(result.get("is_compiled")),"compile_error":result.get("compile_error"),"passed":bool(result.get("all_passed")),"actual_output":item.get("actual_output",""),"sample_input":sample.get("input",""),"expected_output":sample.get("expected_output","")},200


def submit_competition_round(user_id,comp_id,position,code,identified_category,explanation):
    if not isinstance(code,str) or not code.strip() or len(code)>50000:return {"error":"Enter code (up to 50,000 characters)."},400
    if not isinstance(identified_category,str) or len(identified_category)>80:return {"error":"Choose the suspected bug category."},400
    if not isinstance(explanation,str) or not 20<=len(explanation.strip())<=2000:return {"error":"Explain why the category fits in 20 to 2,000 characters."},400
    try:position=int(position)
    except (TypeError,ValueError):return {"error":"Invalid challenge number."},400
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT c.ends_at,c.duration_minutes,c.scoring_rule,r.language,r.tests_json,r.bug_category,p.started_at,p.status FROM competitions c JOIN competition_rounds r ON r.competition_id=c.id JOIN competition_participants p ON p.competition_id=c.id WHERE c.id=? AND p.user_id=? AND r.position=?",(comp_id,user_id,position)).fetchone()
        if not row:conn.rollback();return {"error":"Challenge not found for this participant."},404
        if not row["started_at"]:conn.rollback();return {"error":"The host has not started the competition."},409
        now=int(time.time());deadline=min(row["ends_at"],row["started_at"]+row["duration_minutes"]*60)
        if now>=deadline:conn.rollback();return {"error":"Competition submission window has ended."},409
        exists=conn.execute("SELECT status FROM competition_round_submissions WHERE competition_id=? AND user_id=? AND position=?",(comp_id,user_id,position)).fetchone()
        if exists:conn.rollback();return {"error":"This challenge submission is already locked."},409
        tests=json.loads(row["tests_json"]);conn.execute("INSERT INTO competition_round_submissions(competition_id,user_id,position,code,status,total_tests) VALUES(?,?,?,?,?,?)",(comp_id,user_id,position,code,"evaluating",len(tests)));conn.commit()
        language=row["language"];expected_category=row["bug_category"]
    except Exception:conn.rollback();raise
    finally:conn.close()
    try:
        result=code_executor.execute_code(code,language,tests);passed=int(result.get("passed_tests") or 0);total=len(tests);finished=int(time.time())
        if finished>=deadline:raise TimeoutError("Competition timer expired during evaluation.")
        matched=identified_category==expected_category;score=round(100*passed/max(1,total)) if row["scoring_rule"]=="tests_only" else round(90*passed/max(1,total))+(10 if matched else 0)
        safe={"is_compiled":bool(result.get("is_compiled")),"compile_error":result.get("compile_error"),"passed_tests":passed,"total_tests":total,"score":score,"all_passed":bool(result.get("all_passed")),"identification_correct":matched}
        conn=connect_store();conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE competition_round_submissions SET status='submitted',passed_tests=?,score=?,finished_at=?,execution_json=? WHERE competition_id=? AND user_id=? AND position=? AND status='evaluating'",(passed,score,finished,json.dumps(safe),comp_id,user_id,position))
        summary=conn.execute("SELECT COUNT(*) total, SUM(CASE WHEN status='submitted' THEN 1 ELSE 0 END) done, SUM(passed_tests) passed, SUM(total_tests) tests, SUM(score) points, MAX(finished_at) finished FROM competition_round_submissions WHERE competition_id=? AND user_id=?",(comp_id,user_id)).fetchone()
        total_rounds=conn.execute("SELECT COUNT(*) FROM competition_rounds WHERE competition_id=?",(comp_id,)).fetchone()[0]
        if summary["done"]>=total_rounds:
            aggregate=round((summary["points"] or 0)/max(1,total_rounds))
            conn.execute("UPDATE competition_participants SET status='submitted',finished_at=?,passed_tests=?,total_tests=?,score=? WHERE competition_id=? AND user_id=?",(finished,summary["passed"] or 0,summary["tests"] or 0,aggregate,comp_id,user_id))
        conn.commit();conn.close()
        return {"submission":safe,"position":position,"challenge_count":total_rounds,"challenges_completed":summary["done"],"competition_complete":summary["done"]>=total_rounds},200
    except Exception as exc:
        recovery=connect_store();recovery.execute("DELETE FROM competition_round_submissions WHERE competition_id=? AND user_id=? AND position=? AND status='evaluating'",(comp_id,user_id,position));recovery.commit();recovery.close()
        if isinstance(exc,TimeoutError):return {"error":str(exc)},410
        raise


def submit_competition(user_id,comp_id,code,identified_category,explanation):
    if not isinstance(code,str) or not code.strip() or len(code)>50000:return {"error":"Enter code (up to 50,000 characters)."},400
    if not isinstance(identified_category,str) or len(identified_category)>80:return {"error":"Choose the suspected bug category."},400
    if not isinstance(explanation,str) or not 20<=len(explanation.strip())<=2000:return {"error":"Explain why the category fits in 20 to 2,000 characters."},400
    conn=connect_store()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("SELECT c.ends_at,c.duration_minutes,c.scoring_rule,ch.language,ch.tests_json,ch.bug_category,p.started_at,p.status FROM competitions c JOIN competition_challenges ch ON ch.competition_id=c.id JOIN competition_participants p ON p.competition_id=c.id WHERE c.id=? AND p.user_id=?",(comp_id,user_id)).fetchone()
        if not row:conn.rollback();return {"error":"Competition participation not found."},404
        now=int(time.time())
        if row["status"]=="submitted":conn.rollback();return {"error":"Submission already recorded; duplicate points are not awarded."},409
        if row["status"]=="evaluating":conn.rollback();return {"error":"Your submission is already being evaluated."},409
        if not row["started_at"]:conn.rollback();return {"error":"Start the assigned challenge first."},409
        if now>=row["ends_at"] or now>row["started_at"]+row["duration_minutes"]*60:conn.rollback();return {"error":"Competition submission window has ended."},409
        tests=json.loads(row["tests_json"])
        language=row["language"]
        expected_category=row["bug_category"]
        ends_at=row["ends_at"]
        deadline=min(ends_at,row["started_at"]+row["duration_minutes"]*60)
        started_at=row["started_at"]
        claim_time=now
        conn.execute("UPDATE competition_participants SET status='evaluating',code=?,identified_category=?,bug_explanation=?,evaluation_started_at=? WHERE competition_id=? AND user_id=? AND status IN ('joined','in_progress')",
            (code,identified_category,explanation.strip(),claim_time,comp_id,user_id))
        if conn.total_changes<1:
            conn.rollback();return {"error":"The submission could not be claimed."},409
        conn.commit()
    except Exception:conn.rollback();raise
    finally:conn.close()

    try:
        result=code_executor.execute_code(code,language,tests)
        passed=int(result.get("passed_tests") or 0); total=len(tests)
        finished=int(time.time())
        conn=connect_store()
        conn.execute("BEGIN IMMEDIATE")
        state=conn.execute("SELECT status FROM competition_participants WHERE competition_id=? AND user_id=?",(comp_id,user_id)).fetchone()
        if not state or state["status"]!="evaluating":
            conn.rollback();conn.close();return {"error":"Submission evaluation state changed."},409
        if finished>=deadline:
            conn.execute("UPDATE competition_participants SET status='timed_out',finished_at=?,evaluation_started_at=NULL WHERE competition_id=? AND user_id=?",(deadline,comp_id,user_id));conn.commit();conn.close()
            return {"error":"The competition timer expired before evaluation finished."},410
        identification_correct=identified_category==expected_category
        score=round(100*passed/max(1,total)) if row["scoring_rule"]=="tests_only" else round(90*passed/max(1,total))+(10 if identification_correct else 0)
        safe={"is_compiled":bool(result.get("is_compiled")),"compile_error":result.get("compile_error"),"passed_tests":passed,"total_tests":total,"score":score,"all_passed":bool(result.get("all_passed"))}
        rule_text="100% based on hidden tests passed." if row["scoring_rule"]=="tests_only" else "90% based on hidden tests passed; 10% for selecting the correct bug category. Explanation wording is not subjectively graded."
        safe.update({"identification_correct":identification_correct,"score_rule":rule_text})
        conn.execute("UPDATE competition_participants SET finished_at=?,status='submitted',passed_tests=?,total_tests=?,score=?,execution_json=?,evaluation_started_at=NULL WHERE competition_id=? AND user_id=? AND status='evaluating'",
                     (finished,passed,total,score,json.dumps(safe),comp_id,user_id));conn.commit();conn.close()
        return {"submission":safe,"elapsed_seconds":finished-row["started_at"]},200
    except Exception:
        try:
            recovery=connect_store()
            recovery.execute("UPDATE competition_participants SET status='in_progress',code=NULL,identified_category=NULL,bug_explanation=NULL,evaluation_started_at=NULL WHERE competition_id=? AND user_id=? AND status='evaluating'",(comp_id,user_id))
            recovery.commit();recovery.close()
        except sqlite3.Error:
            pass
        raise


def competition_status(user_id,comp_id):
    conn=connect_store()
    try:
        row=conn.execute("SELECT status,joined_at,started_at,finished_at,passed_tests,total_tests,score,ready,code FROM competition_participants WHERE competition_id=? AND user_id=?",(comp_id,user_id)).fetchone()
        if not row:return None
        result=dict(row)
        rounds=conn.execute("SELECT r.position,s.status,s.score,s.passed_tests,s.total_tests,s.code FROM competition_rounds r LEFT JOIN competition_round_submissions s ON s.competition_id=r.competition_id AND s.position=r.position AND s.user_id=? WHERE r.competition_id=? ORDER BY r.position",(user_id,comp_id)).fetchall()
        result["rounds"]=[dict(r) for r in rounds]
        result["challenge_count"]=len(rounds)
        if result["status"]=="submitted" and rounds and not any(r["status"]=="submitted" for r in rounds):
            result["rounds"][0].update({"status":"submitted","score":result["score"],"passed_tests":result["passed_tests"],"total_tests":result["total_tests"],"code":result["code"]})
        result["next_round"]=next((r["position"] for r in result["rounds"] if r["status"]!="submitted"),None)
        result["challenges_completed"]=sum(1 for r in result["rounds"] if r["status"]=="submitted")
        return result
    finally:conn.close()


def competition_history(user_id):
    conn=connect_store()
    try:
        rows=conn.execute("""SELECT c.id,c.title,c.description,c.difficulty,c.language,c.starts_at,c.ends_at,c.duration_minutes,c.max_participants,p.status,p.joined_at,p.started_at,p.finished_at,p.passed_tests,p.total_tests,p.score,
            (SELECT COUNT(*) FROM competition_rounds r WHERE r.competition_id=c.id) rounds_total,
            (SELECT COUNT(*) FROM competition_round_submissions s WHERE s.competition_id=c.id AND s.user_id=p.user_id AND s.status='submitted') rounds_done
            FROM competition_participants p JOIN competitions c ON c.id=p.competition_id WHERE p.user_id=? ORDER BY p.joined_at DESC""",(user_id,)).fetchall()
        now=int(time.time())
        return [{"id":r["id"],"title":r["title"],"description":r["description"],"difficulty":r["difficulty"],
            "language":r["language"],"status":r["status"],"joined_at":r["joined_at"],"started_at":r["started_at"],
            "finished_at":r["finished_at"],"passed_tests":r["passed_tests"],"total_tests":r["total_tests"],"score":r["score"],
            "challenges_solved":r["rounds_done"] if r["rounds_total"] else (1 if r["status"]=="submitted" else 0),"challenges_total":r["rounds_total"] or 1,
            "completion_seconds":(r["finished_at"]-r["started_at"]) if r["finished_at"] and r["started_at"] else None,
            "competition_status":"scheduled" if now<r["starts_at"] else "active" if now<r["ends_at"] else "ended"} for r in rows]
    finally:conn.close()


def leaderboard(comp_id):
    conn=connect_store()
    try:
        exists=conn.execute("SELECT 1 FROM competitions WHERE id=?",(comp_id,)).fetchone()
        if not exists:return None
        rows=conn.execute("""SELECT p.user_id,p.status,p.passed_tests,p.total_tests,p.score,p.finished_at,p.started_at,p.joined_at,
            CASE WHEN (SELECT COUNT(*) FROM competition_rounds r WHERE r.competition_id=p.competition_id)=0 THEN CASE WHEN p.status='submitted' THEN 1 ELSE 0 END ELSE (SELECT COUNT(*) FROM competition_round_submissions s WHERE s.competition_id=p.competition_id AND s.user_id=p.user_id AND s.status='submitted') END AS challenges_solved,
            MAX(1,(SELECT COUNT(*) FROM competition_rounds r WHERE r.competition_id=p.competition_id)) AS challenges_total
            FROM competition_participants p WHERE p.competition_id=? AND p.status NOT IN ('left','removed','cancelled') ORDER BY challenges_solved DESC,p.passed_tests DESC,p.score DESC,CASE WHEN p.finished_at IS NULL THEN 1 ELSE 0 END,p.finished_at ASC,p.joined_at ASC""",(comp_id,)).fetchall()
    finally:conn.close()
    # Public names are resolved read-only from the existing authentication DB.
    source=_source_ro()
    try:
        names={r[0]:r[1] for r in source.execute("SELECT id,display_name FROM users WHERE id IN (%s)" % (",".join("?" for _ in rows) or "NULL"),[r["user_id"] for r in rows])} if rows else {}
    finally:source.close()
    now=int(time.time()); comp=public_competition(comp_id)
    result=[]
    for rank,r in enumerate(rows,1):
        elapsed=(r["finished_at"] or now)-r["started_at"] if r["started_at"] else None
        result.append({"rank":rank,"display_name":names.get(r["user_id"],"Student"),"score":r["score"],"passed_tests":r["passed_tests"],"total_tests":r["total_tests"],"status":r["status"],"completion_seconds":elapsed,"challenges_solved":r["challenges_solved"],"challenges_total":r["challenges_total"]})
    return {"competition":comp,"leaderboard":result}


def global_practice_leaderboard():
    """Return the same saved best-attempt ordering shown on the dashboard."""
    conn = database.get_connection()
    try:
        rows = conn.execute("""SELECT u.display_name, a.user_id, a.id, a.is_solved, a.hints_used, a.created_at,
            CAST((julianday(a.updated_at)-julianday(a.created_at))*86400 AS INTEGER) AS duration_seconds
            FROM attempts a JOIN users u ON u.id=a.user_id
            JOIN hint_logs h ON h.attempt_id=a.id AND h.hint_type='mode' AND h.prompt_shown='Mode: competition'
            ORDER BY a.is_solved DESC, a.hints_used ASC, duration_seconds ASC, a.created_at ASC""").fetchall()
        best_by_user = {}
        for row in rows:
            best_by_user.setdefault(row["user_id"], dict(row))
        return [{"rank": i + 1, "student": row["display_name"], "solved": bool(row["is_solved"]),
            "hints": row["hints_used"], "attempt_id": row["id"],
            "duration_seconds": max(0, row["duration_seconds"] or 0)}
            for i, row in enumerate(list(best_by_user.values())[:20])]
    finally:
        conn.close()


def _dna_report_lines(dna, advanced):
    user = dna.get("user") or {}
    summary = dna.get("summary") or {}
    lines = [f"Student: {user.get('display_name') or 'Student'}",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", "",
        "DEBUGGING DNA PROFILE", "Profile status: " + ("No activity yet" if dna.get("empty_state") else "Based on saved activity")]
    lines += ["", "SUMMARY",
        f"Recorded attempts: {summary.get('total_attempts', 0)}",
        f"Solved: {summary.get('solved_count', 0)}",
        f"Solve rate: {summary.get('solve_rate', 0)}%",
        f"Independent solves: {summary.get('independent_solves', 0)}",
        f"Debugging IQ: {summary.get('debugging_iq', 0)}"]
    categories = dna.get("mistake_distribution") or []
    lines.extend(["", "COMMON ERROR CATEGORIES"])
    lines.extend([f"{row.get('name', 'Error category')}: {row.get('count', 0)} occurrence(s)" for row in categories]
                 or ["No error categories recorded yet."])
    lines.extend(["", "STRENGTHS"])
    lines.extend([f"{item.get('name')}: {item.get('accuracy', 0)}% across {item.get('attempts', 0)} attempt(s)"
                  for item in (dna.get("strengths") or [])] or ["No repeated strength evidence yet."])
    lines.extend(["", "AREAS TO IMPROVE"])
    lines.extend([f"{item.get('name')}: {item.get('reason') or item.get('description') or 'More practice may help.'}"
                  for item in (dna.get("concepts_needing_practice") or [])] or ["No improvement areas are recorded yet."])
    lines.extend(["", "PROGRESS HISTORY"])
    lines.extend([f"{item.get('date', 'Date unavailable')}: {item.get('attempts', 0)} attempt(s), {item.get('solved', 0)} solved"
                  for item in (dna.get("timeline") or [])] or ["No activity history is recorded yet."])
    suggestions = dna.get("personalized_insights") or advanced.get("study_plan") or []
    lines.extend(["", "PERSONALIZED LEARNING SUGGESTIONS"])
    lines.extend([str(item.get("text") or item.get("reason") or item.get("concept") or "").strip()
                  for item in suggestions if isinstance(item, dict) and
                  (item.get("text") or item.get("reason") or item.get("concept"))]
                 or ["Start a coding challenge to build a personalized learning plan."])
    return lines


def competition_challenge_catalog(language,difficulty,include_all=False,user_id=None):
    if language not in LANGUAGES or difficulty not in DIFFICULTIES:return []
    if include_all:
        source=_source_ro()
        try:
            source_language="c" if language=="cpp" else language
            rows=source.execute("SELECT id,title,language,difficulty,concept,test_cases_json FROM problems WHERE language=? ORDER BY id DESC LIMIT 50",(source_language,)).fetchall()
        finally:source.close()
        allowed={"easy":{"easy","beginner"},"medium":{"medium","intermediate"},"hard":{"hard"}}[difficulty.casefold()]
        results=[]
        for row in rows:
            if row["difficulty"].casefold() not in allowed or not (language==row["language"] or (language=="cpp" and row["language"]=="c")):continue
            try:count=len(json.loads(row["test_cases_json"] or "[]"))
            except (TypeError,ValueError):continue
            results.append({"id":f"problem:{row['id']}","title":row["title"],"language":language,"difficulty":difficulty,"topic":row["concept"],"test_count":count})
        if user_id is not None:
            conn=connect_store()
            try:
                rows=conn.execute("SELECT id,title,topic,tests_json FROM competition_user_challenges WHERE owner_user_id=? AND language=? AND difficulty=? ORDER BY created_at DESC",(user_id,language,difficulty)).fetchall()
            finally:conn.close()
            for row in rows:
                try:count=len(json.loads(row["tests_json"]))
                except (TypeError,ValueError):continue
                results.append({"id":f"user:{row['id']}","title":row["title"],"language":language,"difficulty":difficulty,"topic":row["topic"],"test_count":count,"source":"Your custom challenge"})
        if results:return results
    if _challenge_factory is None:return []
    challenge=_challenge_factory("c" if language == "cpp" else language,difficulty)
    if not challenge:return []
    return [{"id":f"curated:{language}:{difficulty}","title":challenge["title"],"language":language,
             "difficulty":difficulty,"topic":challenge["concept"],
             "test_count":len(challenge["test_cases"])}]

set_challenge_factory(_local_bug_challenge, _format_feature11_12_challenge)


def find_static_file(rel_path: str) -> Optional[str]:
    """
    Finds a static asset file whether placed:
    1. Inside a 'static/' folder (e.g. static/index.html)
    2. Directly at the repository root (e.g. index.html)
    3. Inside any subfolder
    """
    clean = rel_path.lstrip("/").replace("\\", "/")
    normalized = os.path.normpath(clean).replace("\\", "/")
    if normalized == ".." or normalized.startswith("../"):
        return None
    # Strip leading 'static/' if request already included it
    base_name = clean.split("static/", 1)[-1] if clean.startswith("static/") else clean

    candidate_paths = [
        # 1. Check inside static/ folder next to server.py
        os.path.join(BASE_DIR, "static", base_name),
        # 2. Check directly next to server.py (where index.html, style.css, app.js are in GitHub root)
        os.path.join(BASE_DIR, base_name),
        # 3. Check inside working directory static/ folder
        os.path.join(os.getcwd(), "static", base_name),
        # 4. Check directly in working directory
        os.path.join(os.getcwd(), base_name),
        # 5. Check inside subfolders
        os.path.join(BASE_DIR, "Hackathon Project", "static", base_name),
        os.path.join(BASE_DIR, "Hackathon Project", base_name),
    ]
    for p in candidate_paths:
        resolved = os.path.realpath(p)
        try:
            if os.path.commonpath([os.path.realpath(BASE_DIR), resolved]) == os.path.realpath(BASE_DIR) and os.path.isfile(resolved):
                return resolved
        except ValueError:
            continue

    # Recursive fallback search
    try:
        for root, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in ("backups", "secrets", "venv", "env")]
            if base_name in files:
                candidate = os.path.realpath(os.path.join(root, base_name))
                if os.path.commonpath([os.path.realpath(BASE_DIR), candidate]) == os.path.realpath(BASE_DIR):
                    return candidate
    except Exception:
        pass

    return None

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class CodeMentorHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        # The application is served same-origin; wildcard CORS is unnecessary.
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # Route API requests
        if path.startswith("/api/"):
            public_paths = {"/api/health", "/api/categories", "/api/problems"}
            if path not in public_paths and not path.startswith("/api/problems/"):
                self.auth_user = self.get_authenticated_user()
                if not self.auth_user:
                    self.send_json_error(401, "Please sign in to continue.")
                    return
                if not self.authorize_private_path(path):
                    self.send_json_error(404, "Resource not found")
                    return
            try:
                self.handle_api_get(path, query)
            except Exception:
                print(f"[API Error] GET {path}: request failed")
                self.send_json_error(500, "Request could not be completed. Please try again.")
            return

        # Serve static frontend files
        if path == "/" or path == "":
            index_path = find_static_file("index.html")
            if index_path:
                self.serve_file(index_path, "text/html; charset=utf-8")
            else:
                self.serve_missing_static_page(os.path.join(BASE_DIR, "index.html"))
            return

        if path == "/features9_10.html":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
            return

        clean_path = path.lstrip("/")
        target_file = find_static_file(clean_path)

        if target_file:
            _, ext = os.path.splitext(target_file)
            if ext.lower() not in ALLOWED_STATIC_EXTENSIONS:
                self.send_json_error(403, "Access to non-static file is forbidden")
                return
            mime_type, _ = mimetypes.guess_type(target_file)
            self.serve_file(target_file, mime_type or "application/octet-stream")
        else:
            # Fallback to index.html for SPA client-side routing
            index_path = find_static_file("index.html")
            if index_path:
                self.serve_file(index_path, "text/html; charset=utf-8")
            else:
                self.serve_missing_static_page(os.path.join(BASE_DIR, "index.html"))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            self.send_json_error(400, "Invalid Content-Length")
            return
        if content_length < 0 or content_length > 1_000_000:
            self.send_json_error(413, "Request body exceeds the 1MB limit")
            return
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"

        try:
            payload = json.loads(body) if body.strip() else {}
        except Exception:
            self.send_json_error(400, "Malformed JSON request body")
            return

        public_posts = {"/api/auth/register", "/api/auth/login"}
        if path not in public_posts:
            self.auth_user = self.get_authenticated_user()
            if not self.auth_user:
                self.send_json_error(401, "Please sign in to continue.")
                return
            if not self.authorize_private_path(path):
                self.send_json_error(404, "Resource not found")
                return
            origin = self.headers.get("Origin")
            if origin:
                parsed_origin = urllib.parse.urlparse(origin)
                if parsed_origin.netloc.lower() != self.headers.get("Host", "").lower() or parsed_origin.scheme not in ("http", "https"):
                    self.send_json_error(403, "Cross-site request rejected")
                    return

        try:
            self.handle_api_post(path, query, payload)
        except Exception as exc:
            # Log only source function/line locations and exception class; never
            # print request data, exception text, local paths, or secrets.
            frames = traceback.extract_tb(exc.__traceback__)[-6:]
            trace = " > ".join(f"{frame.name}:{frame.lineno}" for frame in frames)
            print(f"[API Error] POST {path}: {type(exc).__name__}; trace={trace}")
            self.send_json_error(500, "Request could not be completed. Please try again.")

    # ------------------ API Route Handlers ------------------

    def get_authenticated_user(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value
        except (KeyError, TypeError, ValueError):
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        conn = database.get_connection()
        row = conn.execute("SELECT u.id, u.username, u.display_name FROM auth_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?", (token_hash, int(time.time()))).fetchone()
        conn.close()
        return dict(row) if row else None

    def authorize_private_path(self, path):
        if path == "/api/dna/report.pdf":
            return True
        if path.startswith(("/api/dna/", "/api/insights/", "/api/replay/")):
            requested = path.rsplit("/", 1)[-1]
            return requested.isdigit() and int(requested) == self.auth_user["id"]
        if path.startswith("/api/attempts/"):
            suffix = path[len("/api/attempts/"):].split("/", 1)[0]
            if suffix == "start":
                return True
            if not suffix.isdigit():
                return False
            conn = database.get_connection()
            row = conn.execute("SELECT user_id FROM attempts WHERE id=?", (int(suffix),)).fetchone()
            conn.close()
            return bool(row and row["user_id"] == self.auth_user["id"])
        return True

    def make_session(self, user_id):
        token = secrets.token_urlsafe(32)
        now = int(time.time())
        conn = database.get_connection()
        conn.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
        conn.execute("INSERT INTO auth_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
            (hashlib.sha256(token.encode("utf-8")).hexdigest(), user_id, now, now + SESSION_TTL_SECONDS))
        conn.commit()
        conn.close()
        return token

    def session_cookie_header(self, token, clear=False):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        if clear:
            return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"
        return f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; HttpOnly; SameSite=Lax{secure}"

    def auth_rate_limited(self):
        now = time.time()
        key = self.client_address[0]
        recent = [stamp for stamp in _login_failures.get(key, []) if now - stamp < LOGIN_WINDOW_SECONDS]
        _login_failures[key] = recent
        return len(recent) >= LOGIN_LIMIT

    def note_auth_failure(self):
        _login_failures.setdefault(self.client_address[0], []).append(time.time())

    def handle_api_get(self, path: str, query: dict):
        # 1. Health & Status
        if path == "/api/health":
            ai_status = tutor_engine.get_ai_provider_status()
            gcc_avail = os.path.exists(code_executor.get_gcc_path())
            javac_avail = os.path.exists(code_executor.get_javac_path())
            self.send_json_response(200, {
                "status": "healthy",
                "service": "CodeMentor AI Learning Platform",
                "ai_provider": ai_status,
                "compilers": {
                    "gcc_c": gcc_avail,
                    "python": True,
                    "javac_java": javac_avail
                }
            })
            return

        if path == "/api/auth/me":
            self.send_json_response(200, {"user": self.auth_user})
            return

        # Features 9/10 derive owner-scoped insights from the main CodeMentor database.
        if path == "/api/features9-10/overview":
            conn = database.get_connection()
            try:
                data = build_overview(conn, self.auth_user["id"])
            finally:
                conn.close()
            self.send_json_response(200, data)
            return

        # Feature 11: owner-scoped interview setup, sessions, reports and history.
        if path == "/api/interviews/catalog":
            self.send_json_response(200, catalog())
            return
        if path in ("/api/interviews", "/api/interviews/history"):
            self.send_json_response(200, {"interviews": list_interviews(self.auth_user["id"])})
            return
        if path == "/api/competitions":
            self.send_json_response(200, {"competitions": list_competitions()})
            return
        if path == "/api/competitions/history":
            self.send_json_response(200, {"competitions": competition_history(self.auth_user["id"])})
            return
        room_preview = re.fullmatch(r"/api/competition-rooms/([A-Za-z0-9_-]{6,12})", path)
        if room_preview:
            result = preview_competition_code(room_preview.group(1))
            if result is None:self.send_json_error(404, "Competition room was not found")
            else:self.send_json_response(200, result)
            return
        if path == "/api/competitions/join-by-code":
            self.send_json_error(405, "Use POST to join with a room code")
            return
        if path == "/api/competition-challenges/catalog":
            language = query.get("language", [""])[0]
            difficulty = query.get("difficulty", [""])[0]
            include_all = query.get("all", ["0"])[0] == "1"
            self.send_json_response(200, {"challenges": competition_challenge_catalog(language, difficulty, include_all, self.auth_user["id"])})
            return
        interview_match = re.fullmatch(r"/api/interviews/([a-f0-9]{32})(?:/(report))?", path)
        if interview_match:
            interview_id, report_path = interview_match.groups()
            session = get_interview(self.auth_user["id"], interview_id)
            if not session:
                self.send_json_error(404, "Interview not found")
                return
            self.send_json_response(200, {"report": session["report"]} if report_path else session)
            return
        competition_match = re.fullmatch(r"/api/competitions/([a-f0-9]{32})(?:/(leaderboard|leaderboard\.(?:csv|pdf)|status|lobby))?", path)
        competition_round_match = re.fullmatch(r"/api/competitions/([a-f0-9]{32})/rounds/(\d+)", path)
        if competition_round_match:
            comp_id, position = competition_round_match.groups()
            result, status = get_competition_round(self.auth_user["id"], comp_id, position)
            self.send_json_response(status, result)
            return
        if competition_match:
            competition_id, action = competition_match.groups()
            if action in ("leaderboard", "leaderboard.csv", "leaderboard.pdf"):
                result = leaderboard(competition_id)
                if result is None:
                    self.send_json_error(404, "Competition not found")
                elif action == "leaderboard.csv":
                    rows = result["leaderboard"]
                    payload = _leaderboard_csv(["Rank", "Display name", "Total score", "Correct submissions", "Available submissions", "Challenges completed", "Challenges total", "Status", "Completion seconds"],
                        [[row["rank"], row["display_name"], row["score"], row["passed_tests"], row["total_tests"], row["challenges_solved"], row["challenges_total"], row["status"], row["completion_seconds"]] for row in rows])
                    self.send_download_response(200, payload, "text/csv; charset=utf-8", "codementor-competition-leaderboard.csv")
                elif action == "leaderboard.pdf":
                    rows = result["leaderboard"]
                    competition_title = (result.get("competition") or {}).get("title", "Competition")
                    lines = [f"Scope: {competition_title}", "Ranking follows the saved competition ranking rules.", ""]
                    lines.extend([f"#{row['rank']} {row['display_name']} | Score {row['score']} | {row['passed_tests']}/{row['total_tests']} correct submissions | {row['challenges_solved']}/{row['challenges_total']} challenges | {row['status']}" for row in rows]
                                 or ["No joined participants yet."])
                    self.send_download_response(200, _build_text_pdf("Competition Leaderboard", lines), "application/pdf", "codementor-competition-leaderboard.pdf")
                else:
                    self.send_json_response(200, result)
                return
            if action == "status":
                status = competition_status(self.auth_user["id"], competition_id)
                if status is None:
                    self.send_json_error(404, "Participation not found")
                else:
                    self.send_json_response(200, {"participation": status})
                return
            if action == "lobby":
                lobby = competition_lobby(self.auth_user["id"], competition_id)
                if lobby is None:
                    self.send_json_error(404, "Competition room not found")
                else:
                    self.send_json_response(200, lobby)
                return
            result = public_competition(competition_id, include_details=True)
            if result is None:
                self.send_json_error(404, "Competition not found")
            else:
                if not can_view_competition_room_code(self.auth_user["id"], competition_id):
                    result.pop("room_code", None)
                self.send_json_response(200, result)
            return

        # 2. Current signed-in user only; account switching would bypass auth.
        if path == "/api/users":
            self.send_json_response(200, {"users": [self.auth_user]})
            return

        # 3. Mistake Categories Taxonomy
        if path == "/api/categories":
            conn = database.get_connection()
            c = conn.cursor()
            c.execute("SELECT * FROM mistake_categories ORDER BY id ASC")
            cats = [dict(r) for r in c.fetchall()]
            conn.close()
            self.send_json_response(200, {"categories": cats})
            return

        # 4. Problems Catalog
        if path == "/api/problems":
            conn = database.get_connection()
            c = conn.cursor()
            c.execute("""
            SELECT id, slug, title, language, difficulty, concept, mistake_category_slug, description
            FROM problems ORDER BY id ASC
            """)
            problems = [dict(r) for r in c.fetchall()]
            conn.close()
            self.send_json_response(200, {"problems": problems})
            return

        # 5. Single Problem Details
        if path.startswith("/api/problems/"):
            slug_or_id = path.split("/api/problems/")[1].strip()
            conn = database.get_connection()
            c = conn.cursor()
            if slug_or_id.isdigit():
                c.execute("SELECT * FROM problems WHERE id = ?", (int(slug_or_id),))
            else:
                c.execute("SELECT * FROM problems WHERE slug = ?", (slug_or_id,))
            prob = c.fetchone()
            conn.close()
            if not prob:
                self.send_json_error(404, "Problem not found")
                return
            raw_problem = dict(prob)
            # Keep reference solutions, hidden tests, and answer-bearing stage
            # configuration on the backend at every stage of the workflow.
            prob_dict = {key: raw_problem[key] for key in (
                "id", "slug", "title", "language", "difficulty", "concept",
                "mistake_category_slug", "description", "buggy_code")}
            self.send_json_response(200, {"problem": prob_dict})
            return

        # 6. Student Debugging DNA and authenticated report download.
        if path == "/api/dna/report.pdf":
            user_id = self.auth_user["id"]
            dna = analyzer.get_student_dna(user_id)
            advanced = analyzer.get_advanced_insights(user_id)
            if dna.get("error"):
                self.send_json_error(404, "Debugging DNA profile not found")
                return
            pdf = _build_text_pdf("CodeMentor Debugging DNA Report", _dna_report_lines(dna, advanced))
            self.send_download_response(200, pdf, "application/pdf", "codementor-debugging-dna.pdf")
            return
        if path.startswith("/api/dna/"):
            parts = path.split("/api/dna/")[1].strip().split("/")
            user_id = int(parts[0]) if parts[0].isdigit() else 1
            dna = analyzer.get_student_dna(user_id)
            self.send_json_response(200, dna)
            return

        # 7. Get Attempt Details
        if path.startswith("/api/insights/"):
            user_id = int(path.rsplit("/", 1)[-1]) if path.rsplit("/", 1)[-1].isdigit() else 0
            self.send_json_response(200, analyzer.get_advanced_insights(user_id))
            return

        if path.startswith("/api/replay/"):
            user_id = int(path.rsplit("/", 1)[-1]) if path.rsplit("/", 1)[-1].isdigit() else 0
            insights = analyzer.get_advanced_insights(user_id)
            self.send_json_response(200, {"attempts": insights.get("error_replay", []), "error": insights.get("error")})
            return

        if path == "/api/bug-challenges/history":
            self.send_json_response(200, {"attempts": feature5.list_history(self.auth_user["id"])})
            return

        if path.startswith("/api/bug-challenges/"):
            suffix = path[len("/api/bug-challenges/"):].strip("/")
            if suffix.isdigit():
                result, status = feature5.get_session(self.auth_user["id"], int(suffix))
                self.send_json_response(status, result)
                return

        if path in ("/api/competition/leaderboard", "/api/competition/leaderboard.csv", "/api/competition/leaderboard.pdf"):
            rows = global_practice_leaderboard()
            if path.endswith(".csv"):
                payload = _leaderboard_csv(["Rank", "Student", "Solved", "Hints", "Duration seconds"],
                    [[row["rank"], row["student"], "Yes" if row["solved"] else "No", row["hints"], row["duration_seconds"]] for row in rows])
                self.send_download_response(200, payload, "text/csv; charset=utf-8", "codementor-global-practice-leaderboard.csv")
            elif path.endswith(".pdf"):
                lines = ["Scope: Global timed-practice leaderboard", "Ranking: solved first, then fewer hints and faster completion.", ""]
                lines.extend([f"#{row['rank']} {row['student']} | {'Solved' if row['solved'] else 'In progress'} | {row['hints']} hints | {row['duration_seconds']} seconds" for row in rows]
                             or ["No timed practice records are available yet."])
                self.send_download_response(200, _build_text_pdf("Global Timed Practice Leaderboard", lines), "application/pdf", "codementor-global-practice-leaderboard.pdf")
            else:
                self.send_json_response(200, {"leaderboard": rows})
            return

        # 8. Get Attempt Details
        if path.startswith("/api/attempts/"):
            parts = path.split("/api/attempts/")[1].strip().split("/")
            attempt_id = int(parts[0]) if parts[0].isdigit() else 0
            lang = query.get("lang", ["en"])[0]
            stage = int(query.get("stage", [0])[0]) or None
            stage_content = tutor_engine.get_stage_content(attempt_id, target_stage=stage, lang=lang)
            if "error" in stage_content:
                self.send_json_error(404, stage_content["error"])
                return
            self.send_json_response(200, {"attempt_stage": stage_content})
            return

        self.send_json_error(404, "Endpoint not found")

    def handle_api_post(self, path: str, query: dict, payload: dict):
        # Feature 11: all records use the existing signed-in user's identity.
        if path == "/api/interviews":
            result, status = create_interview(self.auth_user["id"], payload)
            self.send_json_response(status, result)
            return
        interview_action = re.fullmatch(r"/api/interviews/([a-f0-9]{32})/questions/(\d+)/(draft|run|submit|ask)", path)
        if interview_action:
            interview_id, position, action = interview_action.groups()
            position = int(position)
            if action == "draft":
                result, status = save_interview_code(self.auth_user["id"], interview_id, position, payload.get("code"))
            elif action == "ask":
                result, status = interview_question_ai(self.auth_user["id"], interview_id, position, payload)
            else:
                result, status = interview_run(self.auth_user["id"], interview_id, position, payload, submit=action == "submit")
            self.send_json_response(status, result)
            return

        # Feature 12: creator is the currently authenticated user; no second login.
        if path == "/api/competition-challenges":
            result, status = create_user_competition_challenge(self.auth_user["id"], payload)
            self.send_json_response(status, result)
            return
        if path == "/api/competitions":
            result, status = create_competition(self.auth_user["id"], payload)
            self.send_json_response(status, result)
            return
        if path == "/api/competitions/join-by-code":
            result, status = join_competition_code(self.auth_user["id"], payload.get("room_code"))
            self.send_json_response(status, result)
            return
        competition_round_action = re.fullmatch(r"/api/competitions/([a-f0-9]{32})/rounds/(\d+)/(run|submit)", path)
        if competition_round_action:
            competition_id, position, action = competition_round_action.groups()
            if action == "run":
                result, status = run_competition_round_sample(self.auth_user["id"], competition_id, position, payload.get("code"))
            else:
                result, status = submit_competition_round(self.auth_user["id"], competition_id, position,
                    payload.get("code"), payload.get("identified_category"), payload.get("explanation"))
            self.send_json_response(status, result)
            return
        competition_action = re.fullmatch(r"/api/competitions/([a-f0-9]{32})/(join|start|run|submit|ready|host-start|leave|cancel|remove)", path)
        if competition_action:
            competition_id, action = competition_action.groups()
            if action == "join":
                result, status = join_competition(self.auth_user["id"], competition_id)
            elif action == "start":
                result, status = start_competition_challenge(self.auth_user["id"], competition_id)
            elif action == "run":
                result, status = run_competition_sample(self.auth_user["id"], competition_id, payload.get("code"))
            elif action == "ready":
                result, status = set_competition_ready(self.auth_user["id"], competition_id, payload.get("ready", True))
            elif action == "host-start":
                result, status = start_competition_room(self.auth_user["id"], competition_id)
            elif action == "leave":
                result, status = leave_competition(self.auth_user["id"], competition_id)
            elif action == "cancel":
                result, status = manage_competition_room(self.auth_user["id"], competition_id, "cancel")
            elif action == "remove":
                result, status = manage_competition_room(self.auth_user["id"], competition_id, "remove", payload.get("user_id"))
            else:
                result, status = submit_competition(self.auth_user["id"], competition_id,
                    payload.get("code"), payload.get("identified_category"), payload.get("explanation"))
            self.send_json_response(status, result)
            return

        # 1. Register User
        if path == "/api/auth/register":
            if self.auth_rate_limited():
                self.send_json_error(429, "Too many sign-in attempts. Wait 15 minutes and try again.")
                return
            email = payload.get("email", payload.get("username", ""))
            username = payload.get("username")
            display_name = payload.get("display_name", "")
            password = payload.get("password", "")
            password_confirm = payload.get("password_confirm", payload.get("confirm_password", ""))
            if username is not None and not isinstance(username, str):
                self.send_json_error(400, "Username must be text.")
                return
            if not all(isinstance(value, str) for value in (email, display_name, password, password_confirm)):
                self.send_json_error(400, "Name, email and password fields must be text.")
                return
            email, display_name, password = email.strip().lower(), display_name.strip(), password
            username = (username if username is not None else email).strip().lower()
            if not email or not display_name or not password:
                self.send_json_error(400, "Name, email and password are required")
                return
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254 or len(display_name) > 80 or len(password) < 10 or len(password) > 256:
                self.send_json_error(400, "Enter a valid email, a name up to 80 characters, and a password from 10 to 256 characters.")
                return
            if (not username or len(username) > 254 or
                    (payload.get("username") is not None and not re.fullmatch(r"[a-z0-9_.-]{3,32}", username))):
                self.send_json_error(400, "Username must be 3-32 characters using letters, numbers, dots, underscores, or hyphens.")
                return
            if password != password_confirm:
                self.send_json_error(400, "Passwords do not match.")
                return
            conn = database.get_connection()
            c = conn.cursor()
            try:
                c.execute("""
                INSERT INTO users (username, email, display_name, password_hash)
                VALUES (?, ?, ?, ?)
                """, (username, email, display_name, database.hash_password(password)))
                conn.commit()
                new_id = c.lastrowid
                conn.close()
                token = self.make_session(new_id)
                self.send_json_response(201, {
                    "success": True,
                    "user": {"id": new_id, "username": username, "email": email, "display_name": display_name}
                }, extra_headers=[("Set-Cookie", self.session_cookie_header(token))])
            except Exception as e:
                conn.close()
                self.send_json_error(409, "An account with this username or email already exists")
            return

        # 2. Login User
        if path == "/api/auth/login":
            if self.auth_rate_limited():
                self.send_json_error(429, "Too many sign-in attempts. Wait 15 minutes and try again.")
                return
            username = payload.get("username", "")
            password = payload.get("password", "")
            if not isinstance(username, str) or not isinstance(password, str):
                self.note_auth_failure()
                self.send_json_error(401, "Invalid username or password")
                return
            username, password = username.strip(), password
            conn = database.get_connection()
            c = conn.cursor()
            c.execute("SELECT id, username, email, display_name, password_hash FROM users WHERE lower(username) = ? OR lower(email) = ? ORDER BY id LIMIT 1", (username.lower(), username.lower()))
            user = c.fetchone()
            if not user or not database.verify_password(password, user["password_hash"]):
                conn.close()
                self.note_auth_failure()
                self.send_json_error(401, "Invalid username or password")
                return
            if not user["password_hash"].startswith("pbkdf2_sha256$"):
                c.execute("UPDATE users SET password_hash=? WHERE id=?", (database.hash_password(password), user["id"]))
                conn.commit()
            conn.close()
            _login_failures.pop(self.client_address[0], None)
            token = self.make_session(user["id"])
            self.send_json_response(200, {
                "success": True,
                "user": {"id": user["id"], "username": user["username"], "email": user["email"], "display_name": user["display_name"]}
            }, extra_headers=[("Set-Cookie", self.session_cookie_header(token))])
            return

        if path == "/api/auth/logout":
            cookie = SimpleCookie()
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""
            if token:
                conn = database.get_connection()
                conn.execute("DELETE FROM auth_sessions WHERE token_hash=?", (hashlib.sha256(token.encode("utf-8")).hexdigest(),))
                conn.commit()
                conn.close()
            self.send_json_response(200, {"success": True}, extra_headers=[("Set-Cookie", self.session_cookie_header("", clear=True))])
            return

        if path == "/api/diagnose":
            code = payload.get("code", "")
            language = payload.get("language", "python")
            if not isinstance(code, str) or not code.strip() or len(code) > 100000:
                self.send_json_error(400, "Provide a code snippet between 1 and 100,000 characters")
                return
            result = analyzer.classify_mistakes(code, str(language)[:20],
                error_message=str(payload.get("error", ""))[:2000],
                compiler_error=str(payload.get("compiler_error", ""))[:2000])
            if str(payload.get("lang", "en")).lower() in ("hi", "hinglish"):
                hinglish = {"array_indexing": "Array ko valid index range ke bahar access kiya ja sakta hai; size N ke liye indices 0 se N-1 tak hote hain.",
                    "off_by_one": "Loop ki boundary me ek extra ya missing step ho sakta hai.", "loop_logic": "Loop ki condition ya counter update check karo; shayad loop ruk nahi raha.",
                    "recursion_misunderstanding": "Recursive function ka base case aur rukne ki condition check karo.", "syntax_errors": "Compiler/parser ne code ke syntax me problem detect ki hai.",
                    "variable_scope": "Koi variable use hone se pehle define ya initialize nahi hua ho sakta.", "incorrect_conditions": "Condition me comparison operator aur logical direction check karo.",
                    "algorithm_logic": "Algorithm ke starting value aur edge cases ko dobara check karo."}
                for item in result.get("diagnostics", []):
                    item["evidence"] = hinglish.get(item["category"], item["evidence"])
            self.send_json_response(200, {"classification": result, "source": "local_rule_engine"})
            return

        if path == "/api/challenges/generate":
            requested_id = payload.get("problem_id")
            if requested_id is None:
                result, status = _generate_diverse_bug_challenge(self.auth_user["id"], payload)
                self.send_json_response(status, result)
                return
            if requested_id is not None:
                try:
                    conn = database.get_connection()
                    base_row = conn.execute("SELECT * FROM problems WHERE id=?", (int(requested_id),)).fetchone()
                    conn.close()
                except (TypeError, ValueError):
                    self.send_json_error(400, "problem_id must be a valid ID")
                    return
                if not base_row:
                    self.send_json_error(404, "Challenge not found")
                    return
                base = dict(base_row)
                tests = json.loads(base["test_cases_json"])
                base["source"] = "existing_curated"
                base["test_cases"] = tests
            else:
                language = str(payload.get("language", "python")).lower().strip()
                language = {"py": "python", "cpp": "c", "c++": "c"}.get(language, language)
                difficulty = str(payload.get("difficulty", "Easy")).strip().title()
                if language not in ("python", "c", "java"):
                    self.send_json_error(400, "Choose Python, C, or Java for a bug challenge")
                    return
                if difficulty not in ("Easy", "Medium", "Hard"):
                    self.send_json_error(400, "Choose Easy, Medium, or Hard difficulty")
                    return
                supported_categories = {"syntax_errors", "incorrect_conditions", "array_indexing", "off_by_one",
                    "null_pointer_errors", "string_handling_errors", "function_method_errors", "data_structure_errors",
                    "recursion_misunderstanding"}
                requested_category = str(payload.get("category", "any")).strip().lower()
                if requested_category == "random":
                    requested_category = secrets.choice(sorted(supported_categories))
                if requested_category != "any" and requested_category not in supported_categories:
                    self.send_json_error(400, "Choose one of the supported bug challenge categories")
                    return
                base = _local_bug_challenge(language, difficulty, None if requested_category == "any" else requested_category)
                if not base:
                    self.send_json_error(503, "No local fallback is available for this language and category")
                    return
                tests = base["test_cases"]
            if base.get("language") in ("java", "c"):
                base["buggy_code"] = _format_semicolon_challenge_code(base["buggy_code"], base["language"])
                base["solution_code"] = _format_semicolon_challenge_code(base["solution_code"], base["language"])
            elif base.get("language") == "python":
                base["buggy_code"] = _format_python_challenge_code(base["buggy_code"])
                base["solution_code"] = _format_python_challenge_code(base["solution_code"])
            if not requested_id and base.get("source") == "local_curated":
                base["slug"] += "-readable-v1"
            reference_check = code_executor.execute_code(base["solution_code"], base["language"], tests)
            if not reference_check.get("is_compiled") or not reference_check.get("all_passed"):
                self.send_json_error(503, "This language runtime is unavailable or the reference challenge failed validation")
                return
            local_bug_check = code_executor.execute_code(base["buggy_code"], base["language"], tests)
            if not _challenge_failure_is_valid(base["mistake_category_slug"], local_bug_check):
                self.send_json_error(503, "The selected local challenge did not reproduce its intended bug, so no challenge was started")
                return

            prompt = ("Return one JSON object only with keys title, buggy_code, and category. "
                f"Create a realistic {base['language']} {base['difficulty']} debugging challenge. Preserve the reference program's intended input/output behavior and test cases. "
                f"Inject one clear bug in category '{base['mistake_category_slug']}'. For syntax_errors, the expected failure is a compiler/parser error; for every other category the program must compile and fail at least one listed test. "
                "Use the exact category provided; do not add file, network, shell, or process-control behavior. "
                f"Known tests: {json.dumps(tests)}\nValidated reference solution:\n{base['solution_code']}")
            ai = tutor_engine.generate_ai(prompt, "You create safe educational debugging challenges. Return valid JSON only; never claim validation has happened.", "legacy_challenge_generation")
            generated = None
            fallback_reason = "AI unavailable" if not ai.get("text") else "AI challenge did not pass execution validation"
            if ai.get("text"):
                raw = ai["text"].strip()
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I).strip()
                try:
                    candidate = json.loads(raw)
                    buggy = candidate.get("buggy_code", "")
                    if isinstance(buggy, str):
                        if base["language"] in ("java", "c"):
                            buggy = _format_semicolon_challenge_code(buggy, base["language"])
                        elif base["language"] == "python":
                            buggy = _format_python_challenge_code(buggy)
                    category = str(candidate.get("category", "")).strip().lower()
                    valid_category = category == base["mistake_category_slug"]
                    check = code_executor.execute_code(buggy, base["language"], tests) if isinstance(buggy, str) and 0 < len(buggy) <= 100000 and valid_category else None
                    if _challenge_failure_is_valid(category, check):
                        conn = database.get_connection()
                        c = conn.cursor()
                        slug = "ai-" + uuid.uuid4().hex[:14]
                        title = str(candidate.get("title") or f"AI {base['title']}").strip()[:120]
                        if not title:
                            title = f"AI {base['title']}"
                        c.execute("""INSERT INTO problems (slug,title,language,difficulty,concept,mistake_category_slug,description,buggy_code,solution_code,test_cases_json,stages_config_json)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (slug,title,base["language"],base["difficulty"],base["concept"],category,
                            base["description"] + " (AI-generated; compilation and saved tests validated.)",buggy,base["solution_code"],json.dumps(tests),base.get("stages_config_json", "{}")))
                        new_id = c.lastrowid
                        conn.commit()
                        conn.close()
                        generated = {"id": new_id, "title": title, "language": base["language"], "difficulty": base["difficulty"],
                            "category": category, "description": base["description"], "source": ai["provider"], "is_ai_generated": True,
                            "validation": {"reference_passed": True, "buggy_code_compiled": bool(check.get("is_compiled")),
                                "at_least_one_test_failed": not bool(check.get("all_passed")), "intended_failure_observed": True}}
                except (ValueError, TypeError, KeyError):
                    fallback_reason = "AI response was not valid challenge JSON"
                    generated = None
            if not generated:
                if base.get("id"):
                    local_id = base["id"]
                else:
                    conn = database.get_connection()
                    c = conn.cursor()
                    c.execute("""INSERT OR IGNORE INTO problems (slug,title,language,difficulty,concept,mistake_category_slug,description,buggy_code,solution_code,test_cases_json,stages_config_json)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (base["slug"],base["title"],base["language"],base["difficulty"],base["concept"],base["mistake_category_slug"],
                        base["description"],base["buggy_code"],base["solution_code"],json.dumps(tests),base.get("stages_config_json", "{}")))
                    conn.commit()
                    local_row = c.execute("SELECT id FROM problems WHERE slug=?", (base["slug"],)).fetchone()
                    local_id = local_row["id"]
                    conn.close()
                generated = {"id": local_id, "title": base["title"], "language": base["language"], "difficulty": base["difficulty"],
                    "category": base["mistake_category_slug"], "description": base["description"], "source": "local_curated", "is_ai_generated": False,
                    "validation": {"reference_passed": True, "buggy_code_compiled": bool(local_bug_check.get("is_compiled")),
                        "at_least_one_test_failed": not bool(local_bug_check.get("all_passed")), "intended_failure_observed": True},
                    "fallback_reason": fallback_reason}
            self.send_json_response(200, {"challenge": generated, "ai_status": tutor_engine.get_ai_provider_status()})
            return

        # 3. Start a Debugging Session (on a curated problem)
        if path == "/api/attempts/start":
            user_id = self.auth_user["id"]
            problem_id = payload.get("problem_id")
            if not problem_id:
                self.send_json_error(400, "problem_id is required")
                return
            try:
                problem_id = int(problem_id)
            except (TypeError, ValueError):
                self.send_json_error(400, "problem_id must be a valid ID")
                return

            conn = database.get_connection()
            c = conn.cursor()
            c.execute("SELECT 1 FROM users WHERE id=?", (user_id,))
            if not c.fetchone():
                conn.close()
                self.send_json_error(404, "Student profile not found")
                return
            c.execute("SELECT * FROM problems WHERE id = ?", (problem_id,))
            prob = c.fetchone()
            if not prob:
                conn.close()
                self.send_json_error(404, "Problem not found")
                return

            prob = dict(prob)
            initial_code = prob["buggy_code"]
            lang = prob["language"]

            # Classify initial bug
            classification = analyzer.classify_mistakes(
                initial_code, lang, expected_category=prob["mistake_category_slug"]
            )
            categories_json = json.dumps(classification["categories"])

            c.execute("""
            INSERT INTO attempts (user_id, problem_id, language, initial_code, current_code,
                                  mistake_categories_json, current_stage, hints_used, is_solved,
                                  solved_independently, solution_unlocked, verification_status, is_completed)
            VALUES (?, ?, ?, ?, ?, ?, 1, 0, 0, 0, 0, 'unverified', 0)
            """, (user_id, problem_id, lang, initial_code, initial_code, categories_json))
            attempt_id = c.lastrowid
            conn.commit()
            mode = payload.get("mode", "practice")
            if mode in ("interview", "competition"):
                c.execute("INSERT INTO hint_logs (attempt_id, stage_number, hint_type, prompt_shown, student_response, explanation_lang) VALUES (?, 1, 'mode', ?, '', ?)",
                    (attempt_id, f"Mode: {mode}", payload.get("lang", "en")))
                conn.commit()
            conn.close()

            stage_data = tutor_engine.get_stage_content(attempt_id, 1, lang=payload.get("lang", "en"))
            safe_problem = {key: prob[key] for key in ("id", "slug", "title", "language", "difficulty", "concept", "mistake_category_slug", "description", "buggy_code")}
            self.send_json_response(201, {
                "attempt_id": attempt_id,
                "problem": safe_problem,
                "classification": classification,
                "stage": stage_data,
                "mode": mode if mode in ("interview", "competition") else "practice"
            })
            return

        # 4. Custom Code Debugging Submission
        if path == "/api/custom-debug":
            user_id = self.auth_user["id"]
            code = payload.get("code", "")
            language = payload.get("language", "python").strip()
            title = payload.get("title", "").strip()
            question_or_error = payload.get("description", "").strip()

            if language.lower() not in {"python", "c", "cpp", "java"}:
                self.send_json_error(400, "Unsupported language. Choose Python, C, C++ or Java.")
                return

            if not code or not code.strip():
                self.send_json_error(400, "Code snippet cannot be empty")
                return

            if len(code) > 100000:
                self.send_json_error(400, "Code snippet exceeds maximum allowed size (100KB)")
                return

            custom_title = title or question_or_error or "Custom Debugging Snippet"

            # Classify mistake
            classification = analyzer.classify_mistakes(
                code=code,
                language=language,
                error_message=question_or_error
            )
            categories_json = json.dumps(classification["categories"])

            conn = database.get_connection()
            c = conn.cursor()
            c.execute("""
            INSERT INTO attempts (user_id, problem_id, custom_title, language, initial_code, current_code,
                                  mistake_categories_json, current_stage, hints_used, is_solved,
                                  solved_independently, solution_unlocked, verification_status, is_completed)
            VALUES (?, NULL, ?, ?, ?, ?, ?, 1, 0, 0, 0, 0, 'unverified', 0)
            """, (user_id, custom_title, language, code, code, categories_json))
            attempt_id = c.lastrowid
            if question_or_error:
                c.execute("INSERT INTO hint_logs (attempt_id, stage_number, hint_type, prompt_shown, student_response, explanation_lang) VALUES (?, 1, 'diagnosis_context', ?, '', ?)",
                    (attempt_id, question_or_error[:2000], payload.get("lang", "en")))
            conn.commit()
            conn.close()

            stage_data = tutor_engine.get_stage_content(attempt_id, 1, lang=payload.get("lang", "en"))
            self.send_json_response(201, {
                "attempt_id": attempt_id,
                "classification": classification,
                "stage": stage_data
            })
            return

        # 5. Interact Within Attempt Stage
        if path.startswith("/api/attempts/") and path.endswith("/interact"):
            parts = path.split("/api/attempts/")[1].split("/")
            if not parts[0].isdigit():
                self.send_json_error(400, "Invalid attempt ID")
                return
            attempt_id = int(parts[0])
            user_input = payload.get("user_input", "")
            action = payload.get("action", "")
            lang = payload.get("lang", "en")
            if action == "request_hint":
                if not isinstance(user_input, str) or len(user_input) > 1000:
                    self.send_json_error(400, "Hint request context must be text under 1,000 characters.")
                    return
                if not isinstance(lang, str):
                    lang = "en"
            if action == "debug_reply":
                if not isinstance(user_input, str) or not user_input.strip() or len(user_input) > 2000:
                    self.send_json_error(400, "Send a debugging response between 1 and 2,000 characters.")
                    return
                code_snapshot = payload.get("code_snapshot")
                if code_snapshot is not None and (not isinstance(code_snapshot, str) or len(code_snapshot) > 100000):
                    self.send_json_error(400, "Current code snapshot must be text under 100 KB.")
                    return
                if not isinstance(lang, str):
                    lang = "en"
            else:
                code_snapshot = None

            result = tutor_engine.interact_stage(attempt_id, user_input, action, lang, code_snapshot)
            if action == "request_hint" and result.get("error"):
                self.send_json_error(409, result["error"])
                return
            if action == "debug_reply" and result.get("error"):
                self.send_json_error(409, result["error"])
                return
            self.send_json_response(200, result)
            return

        # 6. Verify Student Code Correction
        if path.startswith("/api/attempts/") and path.endswith("/verify"):
            parts = path.split("/api/attempts/")[1].split("/")
            if not parts[0].isdigit():
                self.send_json_error(400, "Invalid attempt ID")
                return
            attempt_id = int(parts[0])
            code = payload.get("code", "")
            lang = payload.get("lang", "en")

            if not code or not code.strip():
                self.send_json_error(400, "Submitted code cannot be empty")
                return

            if len(code) > 100000:
                self.send_json_error(400, "Submitted code exceeds maximum allowed size (100KB)")
                return

            code_revision = payload.get("code_revision")
            if not isinstance(code_revision, int) or code_revision < 0:
                code_revision = None
            challenge_gate = feature5.before_verify(self.auth_user["id"], attempt_id)
            if challenge_gate:
                self.send_json_error(409, challenge_gate["error"])
                return
            result = tutor_engine.verify_student_code(attempt_id, code, lang, code_revision=code_revision)
            if result.get("mode_expired"):
                self.send_json_error(410, result["error"])
                return
            if result.get("error"):
                self.send_json_error(409, result["error"])
                return
            challenge_result = feature5.record_verification(self.auth_user["id"], attempt_id, code, result)
            if challenge_result:
                result["challenge_result"] = challenge_result
            self.send_json_response(200, result)
            return

        if path == "/api/bug-challenges/start":
            result, status = feature5.start_challenge(self.auth_user["id"], payload.get("challenge_id"), payload.get("lang", "en"))
            self.send_json_response(status, result)
            return

        if path.startswith("/api/bug-challenges/") and path.endswith("/prediction"):
            parts = path[len("/api/bug-challenges/"):].split("/")
            if not parts[0].isdigit():
                self.send_json_error(400, "Invalid challenge attempt ID")
                return
            result, status = feature5.submit_prediction(self.auth_user["id"], int(parts[0]), payload.get("prediction"))
            self.send_json_response(status, result)
            return

        if path.startswith("/api/bug-challenges/") and path.endswith("/identification"):
            parts = path[len("/api/bug-challenges/"):].split("/")
            if not parts[0].isdigit():
                self.send_json_error(400, "Invalid challenge attempt ID")
                return
            result, status = feature5.submit_identification(self.auth_user["id"], int(parts[0]), payload.get("line"), payload.get("explanation"))
            self.send_json_response(status, result)
            return

        if path.startswith("/api/attempts/") and path.endswith("/draft"):
            parts = path.split("/api/attempts/")[1].split("/")
            if not parts[0].isdigit():
                self.send_json_error(400, "Invalid attempt ID")
                return
            attempt_id = int(parts[0])
            code = payload.get("code")
            revision = payload.get("revision")
            if not isinstance(code, str) or len(code) > 100000:
                self.send_json_error(400, "Draft code must not exceed 100,000 characters")
                return
            if not isinstance(revision, int) or revision < 1:
                self.send_json_error(400, "Invalid draft revision")
                return
            conn = database.get_connection()
            row = conn.execute("SELECT code_revision,verified_revision,is_completed FROM attempts WHERE id=? AND user_id=?",
                (attempt_id, self.auth_user["id"])).fetchone()
            if not row:
                conn.close()
                self.send_json_error(404, "Attempt not found")
                return
            if row["is_completed"]:
                conn.close()
                self.send_json_error(409, "Completed attempts are read-only")
                return
            saved = revision > row["code_revision"] and revision > row["verified_revision"]
            if saved:
                conn.execute("UPDATE attempts SET current_code=?,code_revision=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND code_revision<? AND verified_revision<?",
                    (code, revision, attempt_id, self.auth_user["id"], revision, revision))
                saved = conn.total_changes > 0
                conn.commit()
            current = conn.execute("SELECT code_revision FROM attempts WHERE id=? AND user_id=?", (attempt_id, self.auth_user["id"])).fetchone()
            conn.close()
            self.send_json_response(200, {"saved": saved, "revision": current["code_revision"]})
            return

        if path.startswith("/api/attempts/") and path.endswith("/explain"):
            parts = path.split("/api/attempts/")[1].split("/")
            if not parts[0].isdigit():
                self.send_json_error(400, "Invalid attempt ID")
                return
            explanation = payload.get("explanation", "")
            if not isinstance(explanation, str) or len(explanation.strip()) < 15 or len(explanation) > 4000:
                self.send_json_error(400, "Explain your fix in 15 to 4,000 characters")
                return
            attempt_id = int(parts[0])
            conn = database.get_connection()
            c = conn.cursor()
            c.execute("SELECT a.*, p.concept, p.description FROM attempts a LEFT JOIN problems p ON p.id=a.problem_id WHERE a.id=?", (attempt_id,))
            attempt = c.fetchone()
            if not attempt:
                conn.close()
                self.send_json_error(404, "Attempt not found")
                return
            if attempt["user_id"] != self.auth_user["id"]:
                conn.close()
                self.send_json_error(404, "Attempt not found")
                return
            if not attempt["is_solved"] or attempt["verification_status"] != "verified":
                conn.close()
                self.send_json_error(409, "Pass all required tests before explaining and completing this debugging attempt.")
                return
            previous = c.execute("SELECT student_response,context_json FROM hint_logs WHERE attempt_id=? AND hint_type='explain_fix' ORDER BY id DESC LIMIT 1", (attempt_id,)).fetchone()
            if previous:
                try:
                    previous_context = json.loads(previous["context_json"] or "{}")
                except (TypeError, ValueError):
                    previous_context = {}
                if previous_context.get("evaluation_status") == "correct":
                    conn.close()
                    self.send_json_response(200, {**previous_context, "feedback": previous_context.get("feedback", "Your explanation was already verified."),
                        "feedback_source": previous_context.get("feedback_source", "saved_evaluation"), "is_completed": True,
                        "already_verified": True})
                    return
                if previous["student_response"] == explanation.strip() and previous_context.get("evaluation_status"):
                    conn.close()
                    self.send_json_response(200, {**previous_context, "feedback": previous_context.get("feedback", "Review your feedback and try again."),
                        "feedback_source": previous_context.get("feedback_source", "saved_evaluation"), "is_completed": False,
                        "already_evaluated": True})
                    return
            try:
                categories = json.loads(attempt["mistake_categories_json"] or "[]")
            except ValueError:
                categories = []
            category_value = categories[0] if categories else "algorithm_logic"
            category = category_value.get("slug", category_value.get("category", "algorithm_logic")) if isinstance(category_value, dict) else str(category_value)
            evaluation = _assess_fix_explanation(category, explanation)
            status = evaluation["status"]
            status_labels = {"correct": "Correct Understanding", "partially_correct": "Partially Correct",
                "incorrect": "Incorrect Understanding", "incomplete": "Incomplete Explanation"}
            local_feedback_by_status = {
                "correct": "Correct understanding: your explanation connects the bug to why the fix works. Your verified tests remain the proof of code correctness.",
                "partially_correct": f"Partially correct: you identified {', '.join(evaluation['matched_concepts']) or 'part of the issue'}. Now explain how that caused the observed failure and how your change prevents it.",
                "incorrect": "I do not see a supported connection to the diagnosed bug yet. Which behavior changed between the original and corrected code, and what test evidence shows it?",
                "incomplete": "Please add a little more: what caused the bug, what did your change alter, and why does that prevent the failure?"
            }
            try:
                saved_tests = json.loads(attempt["last_test_results_json"] or "{}")
            except (TypeError, ValueError):
                saved_tests = {}
            history_rows = c.execute("SELECT hint_type,prompt_shown,student_response,created_at FROM hint_logs WHERE attempt_id=? AND hint_type IN ('verification','hint_request','debug_conversation') ORDER BY id DESC LIMIT 25", (attempt_id,)).fetchall()
            prior_history = []
            for history_row in reversed(history_rows):
                item = {"type": history_row["hint_type"], "created_at": history_row["created_at"]}
                if history_row["hint_type"] == "verification":
                    try:
                        verification = json.loads(history_row["student_response"] or "{}")
                    except (TypeError, ValueError):
                        verification = {}
                    execution = verification.get("execution_result", {})
                    item.update({"summary": history_row["prompt_shown"], "compile_error": execution.get("compile_error"),
                        "overall_error": execution.get("overall_error"), "tests": execution.get("test_results", [])})
                else:
                    item.update({"tutor": history_row["prompt_shown"], "student": history_row["student_response"]})
                prior_history.append(item)
            context = {"evaluation_status": status, "status_label": status_labels[status], "score": evaluation["score"],
                "matched_concepts": evaluation["matched_concepts"], "missing_concepts": evaluation["missing_concepts"],
                "rubric": evaluation["rubric"], "is_completed": status == "correct"}
            ai = {"text": None, "provider": "local_rule_engine"}
            ai_assessment = None
            if status in ("correct", "partially_correct", "incorrect"):
                review_context = {"language": attempt["language"], "problem": attempt["description"],
                    "concept": attempt["concept"], "bug_category": category,
                    "original_buggy_code": str(attempt["initial_code"])[:12000],
                    "corrected_code": str(attempt["current_code"])[:12000],
                    "original_and_prior_execution_evidence": prior_history,
                    "latest_passing_execution": saved_tests,
                    "student_debugging_conversation_and_hints": prior_history,
                    "student_explanation": explanation}
                ai = tutor_engine.generate_ai(
                    "Evaluate the student's explanation against the actual bug and saved evidence. Decide one status: correct, partially_correct, incorrect, or incomplete. "
                    "Return one JSON object only: {\"evaluation_status\":\"...\",\"score\":0,\"feedback\":\"...\"}. "
                    "Consider the problem statement, language, original code, corrected code, actual passing test results, and prior debugging conversation. "
                    "Do not grade by keywords alone, invent test evidence, reveal a different solution, or include code. Keep feedback under 100 words; for incomplete/incorrect/partial answers, give one next step. "
                    "Context JSON: " + json.dumps(review_context, ensure_ascii=False),
                    "You are a careful debugging educator. Assess reasoning from the supplied context, not word overlap.",
                    "explanation_feedback")
                if ai.get("text"):
                    try:
                        raw_assessment = ai["text"].strip()
                        raw_assessment = raw_assessment[raw_assessment.find("{"):raw_assessment.rfind("}") + 1]
                        parsed_assessment = json.loads(raw_assessment)
                        candidate_status = parsed_assessment.get("evaluation_status")
                        candidate_feedback = parsed_assessment.get("feedback")
                        candidate_score = parsed_assessment.get("score")
                        if (candidate_status not in ("correct", "partially_correct", "incorrect", "incomplete")
                                or not isinstance(candidate_feedback, str) or not candidate_feedback.strip()
                                or len(candidate_feedback) > 800 or "```" in candidate_feedback
                                or (attempt["current_code"] and attempt["current_code"].strip() in candidate_feedback)
                                or isinstance(candidate_score, bool) or not isinstance(candidate_score, int)
                                or not 0 <= candidate_score <= 100):
                            raise ValueError("ai_assessment_schema_rejected")
                        status = candidate_status
                        evaluation["score"] = candidate_score
                        ai_assessment = {"evaluation_status": candidate_status, "score": candidate_score}
                        feedback = candidate_feedback.strip()
                    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                        logger.warning("explanation_ai_validation outcome=rejected reason=invalid_assessment_schema final_provider=local")
                        feedback = local_feedback_by_status[status]
                        ai = {"text": None, "provider": "local_rule_engine"}
                else:
                    feedback = local_feedback_by_status[status]
            else:
                feedback = local_feedback_by_status[status]
            context.update({"evaluation_status": status, "status_label": status_labels[status],
                "score": evaluation["score"], "is_completed": status == "correct",
                "matched_concepts": evaluation["matched_concepts"], "missing_concepts": evaluation["missing_concepts"],
                "rubric": evaluation["rubric"], "feedback": feedback,
                "evaluation_source": ai.get("provider") if ai.get("text") else "local_rule_engine",
                "ai_assessment": ai_assessment,
                "feedback_source": ai.get("provider") if ai.get("text") else "local_rule_engine"})
            c.execute("INSERT INTO hint_logs (attempt_id, stage_number, hint_type, prompt_shown, student_response, explanation_lang, context_json) VALUES (?,7,'explain_fix',?,?,?,?)",
                (attempt_id, f"Explain Your Fix: {status_labels[status]} ({evaluation['score']}/100)", explanation.strip(), payload.get("lang", "en"), json.dumps(context, ensure_ascii=False)))
            if status == "correct":
                c.execute("UPDATE attempts SET is_completed=1, updated_at=CURRENT_TIMESTAMP WHERE id=? AND is_solved=1 AND verification_status='verified'", (attempt_id,))
            conn.commit()
            conn.close()
            context.update({"scoring_method": "concept evidence (up to 50) + bug-to-fix reasoning (35) + test/edge-case reasoning (15); status uses required concept and causal evidence",
                "feedback_source": ai.get("provider") if ai.get("text") else "local_rule_engine"})
            self.send_json_response(200, context)
            return

        # 7. Jump or Select Stage Directly
        if path.startswith("/api/attempts/") and path.endswith("/stage"):
            parts = path.split("/api/attempts/")[1].split("/")
            if not parts[0].isdigit():
                self.send_json_error(400, "Invalid attempt ID")
                return
            attempt_id = int(parts[0])
            stage_num = int(payload.get("stage", 1))
            lang = payload.get("lang", "en")

            result = tutor_engine.get_stage_content(attempt_id, stage_num, lang)
            if result.get("error"):
                self.send_json_error(409, result["error"])
                return
            self.send_json_response(200, result)
            return

        self.send_json_error(404, "Endpoint not found")

    # ------------------ Utility Helpers ------------------

    def send_json_response(self, status: int, data: dict, extra_headers=None):
        payload = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def send_download_response(self, status: int, payload: bytes, content_type: str, filename: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def send_json_error(self, status: int, message: str):
        self.send_json_response(status, {"error": message, "status": status})

    def serve_file(self, full_path: str, content_type: str):
        try:
            with open(full_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            if full_path.endswith("index.html"):
                self.serve_missing_static_page(full_path)
                return
            self.send_json_error(404, f"File not found: {str(e)}")

    def serve_missing_static_page(self, missing_path: str):
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>CodeMentor — Static Frontend Folder Missing</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0b0f19;
      color: #e2e8f0;
      padding: 40px 20px;
      display: flex;
      justify-content: center;
      align-items: center;
      min-height: 80vh;
      margin: 0;
    }}
    .box {{
      max-width: 640px;
      background: #131b2e;
      border: 1px solid #1e293b;
      border-radius: 12px;
      padding: 32px;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
    }}
    h1 {{ color: #f59e0b; font-size: 22px; margin-top: 0; }}
    code {{ background: #080d1a; color: #38bdf8; padding: 2px 6px; border-radius: 4px; font-family: monospace; }}
    ol {{ line-height: 1.8; color: #cbd5e1; padding-left: 20px; }}
    .status {{ display: inline-block; background: #059669; color: white; padding: 4px 12px; border-radius: 20px; font-size: 13px; font-weight: 600; margin-bottom: 16px; }}
  </style>
</head>
<body>
  <div class="box">
    <div class="status">✓ Python Backend Running Successfully</div>
    <h1>⚠️ Static Frontend Folder Missing in Repository</h1>
    <p>Render started your application, but could not find <code>index.html</code> at <code>{missing_path}</code>.</p>
    <p>This happens when only the Python files were uploaded to GitHub without the <code>static/</code> directory.</p>
    <h3>👉 How to fix in 1 minute:</h3>
    <ol>
      <li>Open your project repository on <b>GitHub</b>.</li>
      <li>Click <b>Add file</b> &gt; <b>Upload files</b>.</li>
      <li>Drag and drop the entire <code>static</code> folder (which includes <code>index.html</code>, <code>app.js</code>, and <code>style.css</code>).</li>
      <li>Click <b>Commit changes</b>. Render will automatically redeploy and load the application!</li>
    </ol>
  </div>
</body>
</html>"""
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

def run_server():
    database.init_db()
    init_store()
    server_address = (HOST, PORT)
    try:
        httpd = ThreadedHTTPServer(server_address, CodeMentorHandler)
    except OSError as e:
        if getattr(e, "winerror", None) == 10048 or "Address already in use" in str(e):
            print(f"\n❌ [CodeMentor Error] Port {PORT} is already in use by another process.")
            print(f"👉 You can run with another port, for example:")
            print(f"   $env:PORT=8080; python server.py  (PowerShell)")
            print(f"   set PORT=8080 && python server.py (CMD)")
            return
        raise

    display_host = "localhost" if HOST in ("0.0.0.0", "") else HOST
    print(f"\n=============================================================")
    print(f"🚀 CodeMentor Platform running successfully!")
    print(f"   👉 Open in Browser: http://localhost:{PORT}")
    print(f"   👉 Network URL:     http://{display_host}:{PORT}")
    print(f"   - Database:    {database.DB_PATH}")
    print(f"   - Active Mode: {tutor_engine.get_ai_provider_status()['mode_label']}")
    print(f"   - Press Ctrl+C to stop the server")
    print(f"=============================================================\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[CodeMentor] Shutting down gracefully...")
        httpd.server_close()

if __name__ == "__main__":
    run_server()
