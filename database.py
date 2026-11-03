"""
CodeMentor - Database Layer (SQLite3)
Persistent storage for users, mistake taxonomy, curated problems, student attempts,
hint logs, and Debugging DNA records.
"""

import sqlite3
import json
import os
import hashlib
import secrets
from datetime import datetime

def _load_local_env_file():
    """Read a private project .env without printing or overriding shell settings."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip("\"'")
                if key in {"OPENAI_API_KEY", "OPENAI_MODEL", "GROQ_API_KEY", "AI_PROVIDER", "FREE_ONLY_MODE", "DATABASE_PATH", "HOST", "PORT"} and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        pass

_load_local_env_file()
DB_PATH = os.environ.get("DATABASE_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "codementor.db")

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310000)
    return "pbkdf2_sha256$310000$%s$%s" % (salt.hex(), digest.hex())

def verify_password(password: str, stored_hash: str) -> bool:
    """Verify new PBKDF2 hashes and legacy SHA-256 hashes from existing users."""
    if not stored_hash:
        return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            _, rounds, salt_hex, digest_hex = stored_hash.split("$", 3)
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds)).hex()
            return secrets.compare_digest(actual, digest_hex)
        except (ValueError, TypeError):
            return False
    legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return secrets.compare_digest(legacy, stored_hash)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Users Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT,
        display_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """)
    user_columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    if "email" not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email) WHERE email IS NOT NULL")

    # 2. Mistake Categories Taxonomy
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS mistake_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL,
        color TEXT NOT NULL,
        icon TEXT NOT NULL
    );
    """)

    # 3. Practice Problems Catalog
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS problems (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        language TEXT NOT NULL,
        difficulty TEXT NOT NULL,
        concept TEXT NOT NULL,
        mistake_category_slug TEXT NOT NULL,
        description TEXT NOT NULL,
        buggy_code TEXT NOT NULL,
        solution_code TEXT NOT NULL,
        test_cases_json TEXT NOT NULL,
        stages_config_json TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (mistake_category_slug) REFERENCES mistake_categories (slug)
    );
    """)

    # 4. Student Debugging Attempts
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        problem_id INTEGER,
        custom_title TEXT,
        language TEXT NOT NULL,
        initial_code TEXT NOT NULL,
        current_code TEXT NOT NULL,
        mistake_categories_json TEXT NOT NULL,
        current_stage INTEGER DEFAULT 1,
        hints_used INTEGER DEFAULT 0,
        is_solved BOOLEAN DEFAULT 0,
        solved_independently BOOLEAN DEFAULT 0,
        solution_unlocked BOOLEAN DEFAULT 0,
        verification_status TEXT DEFAULT 'unverified',
        last_test_results_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id),
        FOREIGN KEY (problem_id) REFERENCES problems (id)
    );
    """)

    # Additive migration: keep completed legacy attempts readable while new
    # attempts explicitly start incomplete until their fix is explained.
    attempt_columns = {row[1] for row in cursor.execute("PRAGMA table_info(attempts)").fetchall()}
    if "is_completed" not in attempt_columns:
        cursor.execute("ALTER TABLE attempts ADD COLUMN is_completed INTEGER NOT NULL DEFAULT 1")
    if "code_revision" not in attempt_columns:
        cursor.execute("ALTER TABLE attempts ADD COLUMN code_revision INTEGER NOT NULL DEFAULT 0")
    if "verified_revision" not in attempt_columns:
        cursor.execute("ALTER TABLE attempts ADD COLUMN verified_revision INTEGER NOT NULL DEFAULT -1")

    # Feature 5 progress is additive and linked to existing owner-scoped attempts.
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bug_challenge_progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        attempt_id INTEGER NOT NULL UNIQUE,
        challenge_id INTEGER NOT NULL,
        difficulty TEXT NOT NULL,
        topic TEXT NOT NULL,
        bug_category TEXT NOT NULL,
        original_code TEXT NOT NULL,
        final_code TEXT,
        expected_prediction TEXT NOT NULL,
        prediction TEXT,
        prediction_correct INTEGER NOT NULL DEFAULT 0,
        prediction_attempts INTEGER NOT NULL DEFAULT 0,
        bug_line INTEGER NOT NULL,
        bug_line_end INTEGER NOT NULL,
        identified_line INTEGER,
        bug_line_correct INTEGER,
        bug_explanation TEXT,
        code_attempts INTEGER NOT NULL DEFAULT 0,
        hints_used INTEGER NOT NULL DEFAULT 0,
        verification_passed INTEGER NOT NULL DEFAULT 0,
        score INTEGER NOT NULL DEFAULT 0,
        score_breakdown_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'in_progress',
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (attempt_id) REFERENCES attempts(id),
        FOREIGN KEY (challenge_id) REFERENCES problems(id)
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bug_challenge_user_created ON bug_challenge_progress(user_id, created_at DESC)")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bug_challenge_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        attempt_id INTEGER NOT NULL,
        event_type TEXT NOT NULL CHECK(event_type IN ('prediction', 'identification')),
        prediction TEXT,
        identified_line INTEGER,
        explanation TEXT,
        is_correct INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (attempt_id) REFERENCES attempts(id)
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_bug_challenge_events_attempt ON bug_challenge_events(attempt_id, created_at, id)")

    # 5. Hint and Stage Interaction Logs
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS hint_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        attempt_id INTEGER NOT NULL,
        stage_number INTEGER NOT NULL,
        hint_type TEXT NOT NULL,
        prompt_shown TEXT NOT NULL,
        student_response TEXT,
        explanation_lang TEXT DEFAULT 'en',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (attempt_id) REFERENCES attempts (id)
    );
    """)

    # Additive hint migration: keep every old hint and assign it a sequence
    # number. Levels 2–4 share stage 4 in the existing eight-stage UI, so the
    # dedicated field is the source of truth for the four-level ladder.
    hint_columns = {row[1] for row in cursor.execute("PRAGMA table_info(hint_logs)").fetchall()}
    if "hint_level" not in hint_columns:
        cursor.execute("ALTER TABLE hint_logs ADD COLUMN hint_level INTEGER")
    if "hint_provider" not in hint_columns:
        cursor.execute("ALTER TABLE hint_logs ADD COLUMN hint_provider TEXT NOT NULL DEFAULT 'local'")
    if "hint_model" not in hint_columns:
        cursor.execute("ALTER TABLE hint_logs ADD COLUMN hint_model TEXT NOT NULL DEFAULT 'legacy-hint'")
    if "context_json" not in hint_columns:
        cursor.execute("ALTER TABLE hint_logs ADD COLUMN context_json TEXT")
    legacy_hints = cursor.execute("""
        SELECT id, attempt_id FROM hint_logs
        WHERE hint_type='hint_request' AND hint_level IS NULL
        ORDER BY attempt_id, id
    """).fetchall()
    level_by_attempt = {}
    for hint_row in legacy_hints:
        attempt_id = hint_row["attempt_id"]
        level_by_attempt[attempt_id] = level_by_attempt.get(attempt_id, 0) + 1
        cursor.execute("UPDATE hint_logs SET hint_level=? WHERE id=?", (level_by_attempt[attempt_id], hint_row["id"]))
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_hint_logs_attempt_level_language
        ON hint_logs(attempt_id, hint_level, explanation_lang)
        WHERE hint_type='hint_request' AND hint_level IS NOT NULL
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS auth_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
    );
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_expiry ON auth_sessions(user_id, expires_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_attempts_user_created ON attempts(user_id, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_hint_logs_attempt_id ON hint_logs(attempt_id, id)")

    conn.commit()
    seed_data(conn)
    conn.close()
    print(f"[CodeMentor DB] Initialized database at {DB_PATH}")

def seed_data(conn):
    cursor = conn.cursor()

    # Seed Mistake Taxonomy
    categories = [
        ("off_by_one", "Off-by-One Errors", "Loop boundary conditions exceeding or falling short by 1 (e.g. <= length)", "#f59e0b", "ruler"),
        ("array_indexing", "Array Indexing Errors", "Out-of-bounds array access, negative indexing, or incorrect offset calculation", "#ef4444", "brackets"),
        ("loop_logic", "Loop Logic Errors", "Infinite loops, incorrect termination conditions, or missing step increments", "#8b5cf6", "repeat"),
        ("recursion_misunderstanding", "Recursion Misunderstandings", "Missing or faulty base cases, or failing to converge toward termination", "#ec4899", "git-merge"),
        ("variable_scope", "Variable Scope Errors", "Variable shadowing, using uninitialized variables, or outer/inner scope confusion", "#3b82f6", "box"),
        ("syntax_errors", "Syntax Errors", "Missing colons, unmatched brackets, or malformed language structures", "#6b7280", "terminal"),
        ("incorrect_conditions", "Incorrect Conditions", "Using assignment '=' instead of comparison '==', or inverted boolean logic", "#10b981", "check-circle"),
        ("null_pointer_errors", "Null Reference Errors", "Using an object or pointer before it refers to a valid value", "#f97316", "circle-alert"),
        ("string_handling_errors", "String Handling Errors", "Incorrect string normalization, comparison, or transformation", "#14b8a6", "text"),
        ("function_method_errors", "Function and Method Errors", "Incorrect function or method behavior, return values, or calls", "#6366f1", "braces"),
        ("data_structure_errors", "Data Structure Errors", "Incorrect queue, stack, collection, or structure operations", "#a855f7", "layers"),
        ("algorithm_logic", "Algorithm & Logic Errors", "Incorrect mathematical formulas, wrong accumulator initializations, or flawed flow", "#06b6d4", "cpu")
    ]

    for slug, name, desc, color, icon in categories:
        cursor.execute("""
        INSERT OR IGNORE INTO mistake_categories (slug, name, description, color, icon)
        VALUES (?, ?, ?, ?, ?)
        """, (slug, name, desc, color, icon))

    # Seed Default Users
    cursor.execute("SELECT COUNT(*) as cnt FROM users")
    if cursor.fetchone()["cnt"] == 0:
        cursor.execute("""
        INSERT INTO users (username, display_name, password_hash)
        VALUES (?, ?, ?)
        """, ("alex_student", "Alex Sharma", hash_password("password123")))

        cursor.execute("""
        INSERT INTO users (username, display_name, password_hash)
        VALUES (?, ?, ?)
        """, ("priya_dev", "Priya Patel", hash_password("password123")))

        cursor.execute("""
        INSERT INTO users (username, display_name, password_hash)
        VALUES (?, ?, ?)
        """, ("new_learner", "Rohan Verma", hash_password("password123")))

    # Seed Curated Problems
    cursor.execute("SELECT COUNT(*) as cnt FROM problems")
    if cursor.fetchone()["cnt"] == 0:
        problems = [
            {
                "slug": "c-array-boundary-overflow",
                "title": "Print Elements of an Array",
                "language": "c",
                "difficulty": "Beginner",
                "concept": "Array Boundaries & Iteration",
                "mistake_category_slug": "array_indexing",
                "description": "The function `printArray` is supposed to print the elements of an integer array of size 5 separated by spaces. However, it crashes or prints garbage values at the end due to an out-of-bounds boundary check.",
                "buggy_code": """#include <stdio.h>

void printArray(int arr[], int size) {
    // BUG: Loop condition goes past valid bounds
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
""",
                "solution_code": """#include <stdio.h>

void printArray(int arr[], int size) {
    // FIX: Valid indices for size 5 are 0, 1, 2, 3, 4 (strictly i < size)
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
""",
                "test_cases_json": json.dumps([
                    {
                        "input": "",
                        "expected_output": "10 20 30 40 50 \n",
                        "description": "Prints exactly the 5 elements without accessing arr[5]"
                    }
                ]),
                "stages_config_json": json.dumps({
                    "stage1_understand": {
                        "en": "In C, arrays are zero-indexed. If an array has 5 elements, what are the exact valid indices that exist in memory?",
                        "hinglish": "C me arrays zero-index hote hain. Agar array ka size 5 hai, to memory me valid indices kaun-kaun se honge? Socho kya 5th index exist karta hai?",
                        "question": "What happens when you access arr[5] in an array of size 5?",
                        "options": [
                            "It accesses memory outside the array (undefined behavior / garbage value)",
                            "It prints the last element 50",
                            "It prints 0 automatically",
                            "It expands the array size"
                        ],
                        "correct_option_index": 0
                    },
                    "stage2_investigate": {
                        "en": "Look closely at line 5: `for (int i = 0; i <= size; i++)`. Check the loop condition operator. How many iterations will this loop attempt for `size = 5`?",
                        "hinglish": "Line 5 ko dhyan se dekho: `for (int i = 0; i <= size; i++)`. Jab `size = 5` hai, to loop kitni baar chalega?",
                        "suspicious_line": 5
                    },
                    "stage3_first_hint": {
                        "en": "Notice the comparison operator `<=`. When `i` reaches 5, the condition `5 <= 5` is still TRUE!",
                        "hinglish": "Dhyan do `<=` operator par. Jab `i` 5 banega, to `5 <= 5` TRUE hoga aur `arr[5]` access ho jayega!"
                    },
                    "stage4_stronger_hint": {
                        "en": "Change `<= size` to `< size`. This ensures `i` only takes values 0, 1, 2, 3, and 4.",
                        "hinglish": "`<= size` ko badal kar `< size` kar do taaki `i` sirf 0 se 4 tak hi chale."
                    },
                    "explanation": {
                        "en": "In C, an array declared as `int numbers[N]` has valid indices from `0` to `N - 1`. Using `i <= N` runs the loop N+1 times and causes an off-by-one buffer over-read (array indexing error), accessing unauthorized memory.",
                        "hinglish": "C me `int numbers[N]` ke valid indices `0` se `N - 1` tak hote hain. `i <= N` lagane se loop N+1 baar chalta hai aur `arr[N]` access karta hai jo invalid memory hai."
                    }
                })
            },
            {
                "slug": "c-recursive-factorial-bug",
                "title": "Recursive Factorial Function",
                "language": "c",
                "difficulty": "Beginner",
                "concept": "Recursion & Base Cases",
                "mistake_category_slug": "recursion_misunderstanding",
                "description": "The function `factorial(n)` computes n! recursively. However, the program crashes with a segmentation fault (stack overflow) or returns incorrect results for small numbers because the base case condition is flawed.",
                "buggy_code": """#include <stdio.h>

int factorial(int n) {
    // BUG: Flawed base case causes infinite recursion or incorrect calculation
    if (n == 0) {
        return 0; // Wait, is 0! equal to 0? Also what about n <= 1?
    }
    return n * factorial(n - 1);
}

int main() {
    int result = factorial(5);
    printf("5! = %d\\n", result);
    return 0;
}
""",
                "solution_code": """#include <stdio.h>

int factorial(int n) {
    // FIX: Base case: 0! = 1 and 1! = 1
    if (n <= 1) {
        return 1;
    }
    return n * factorial(n - 1);
}

int main() {
    int result = factorial(5);
    printf("5! = %d\\n", result);
    return 0;
}
""",
                "test_cases_json": json.dumps([
                    {
                        "input": "",
                        "expected_output": "5! = 120\n",
                        "description": "5! = 120"
                    }
                ]),
                "stages_config_json": json.dumps({
                    "stage1_understand": {
                        "en": "Mathematically, what is 0! (zero factorial)? Remember that factorial is a product: 5! = 5 * 4 * 3 * 2 * 1.",
                        "hinglish": "Mathematics me 0! (zero factorial) ki value kya hoti hai? Yaadr rahe ki factorial product hota hai.",
                        "question": "What is the mathematical value of 0!?",
                        "options": ["1", "0", "Undefined", "-1"],
                        "correct_option_index": 0
                    },
                    "stage2_investigate": {
                        "en": "Examine line 6: `return 0;`. If `factorial(0)` returns 0, what does `1 * factorial(0)` become when multiplied?",
                        "hinglish": "Line 6 dekho: `return 0;`. Agar `factorial(0)` 0 return karega, to `1 * 0` kya ho jayega?",
                        "suspicious_line": 6
                    },
                    "stage3_first_hint": {
                        "en": "Because everything gets multiplied down to the base case, multiplying any number by 0 turns the entire answer into 0!",
                        "hinglish": "Kyunki sabhi numbers multiply hote hain, agar aakhri step me 0 se multiply hoga to poora result 0 ho jayega!"
                    },
                    "stage4_stronger_hint": {
                        "en": "In recursive base case `if (n <= 1)`, return 1 instead of 0.",
                        "hinglish": "Base case me `if (n <= 1)` par `return 1;` karo, 0 nahi."
                    },
                    "explanation": {
                        "en": "In recursive multiplicative problems (like factorials), the identity element is 1, not 0. Returning 0 from the base case collapses the entire product chain `5 * 4 * 3 * 2 * 1 * 0` into 0.",
                        "hinglish": "Multiplication wale recursive functions me base identity 1 hoti hai. 0 return karne se poori chain `5*4*3*2*1*0 = 0` ban jaati hai."
                    }
                })
            },
            {
                "slug": "python-loop-boundary-sum",
                "title": "Sum of First N Natural Numbers",
                "language": "python",
                "difficulty": "Beginner",
                "concept": "Loop Boundaries in Range",
                "mistake_category_slug": "off_by_one",
                "description": "The function `sum_numbers(n)` should calculate the sum of numbers from 1 to n inclusive. For n = 5, the answer should be 1 + 2 + 3 + 4 + 5 = 15, but it currently returns 10.",
                "buggy_code": """def sum_numbers(n):
    total = 0
    # BUG: Python's range(start, stop) excludes the stop value!
    for i in range(1, n):
        total += i
    return total

if __name__ == "__main__":
    result = sum_numbers(5)
    print(f"Sum = {result}")
""",
                "solution_code": """def sum_numbers(n):
    total = 0
    # FIX: range(1, n + 1) includes n
    for i in range(1, n + 1):
        total += i
    return total

if __name__ == "__main__":
    result = sum_numbers(5)
    print(f"Sum = {result}")
""",
                "test_cases_json": json.dumps([
                    {
                        "input": "",
                        "expected_output": "Sum = 15\n",
                        "description": "Sum of 1 to 5 should equal 15"
                    }
                ]),
                "stages_config_json": json.dumps({
                    "stage1_understand": {
                        "en": "In Python, `range(1, 5)` generates a sequence. What is the last number generated in this sequence?",
                        "hinglish": "Python me `range(1, 5)` sequence banata hai. Is sequence ka aakhri number kya hota hai?",
                        "question": "What numbers does range(1, 5) actually produce?",
                        "options": [
                            "[1, 2, 3, 4] (stops before 5)",
                            "[1, 2, 3, 4, 5] (includes 5)",
                            "[0, 1, 2, 3, 4]",
                            "[2, 3, 4, 5]"
                        ],
                        "correct_option_index": 0
                    },
                    "stage2_investigate": {
                        "en": "Look at line 4: `for i in range(1, n):`. If `n = 5`, which number is missed in the sum?",
                        "hinglish": "Line 4 dekho: `for i in range(1, n):`. Jab `n = 5` hai, to kaunsa number sum me judne se chhut gaya?",
                        "suspicious_line": 4
                    },
                    "stage3_first_hint": {
                        "en": "In Python, `range(a, b)` is half-open: `[a, b)`. It includes `a` but excludes `b`.",
                        "hinglish": "Python ka `range(a, b)` `a` ko include karta hai lekin `b` se pehle ruk jaata hai."
                    },
                    "stage4_stronger_hint": {
                        "en": "To include `n`, your upper boundary must be `n + 1`: `for i in range(1, n + 1):`.",
                        "hinglish": "`n` ko shamil karne ke liye boundary ko `n + 1` likhna hoga: `for i in range(1, n + 1):`."
                    },
                    "explanation": {
                        "en": "Python's `range(start, stop)` function excludes the `stop` parameter. When iterating up to `n` inclusive, programmers frequently make this off-by-one error by forgetting to write `range(start, n + 1)`.",
                        "hinglish": "Python me `range(start, stop)` stop value ko exclude karta hai. `n` tak loop chalane ke liye hamesha `n + 1` specify karna padta hai."
                    }
                })
            },
            {
                "slug": "python-find-max-negative",
                "title": "Find Maximum in List",
                "language": "python",
                "difficulty": "Intermediate",
                "concept": "Algorithm Accumulator Initialization",
                "mistake_category_slug": "algorithm_logic",
                "description": "The function `find_max(nums)` should return the maximum integer in a list. When tested on a list of negative numbers like `[-10, -5, -20]`, it incorrectly returns 0 instead of -5.",
                "buggy_code": """def find_max(nums):
    # BUG: Initializing max_val to 0 fails when all numbers are negative!
    max_val = 0
    for num in nums:
        if num > max_val:
            max_val = num
    return max_val

if __name__ == "__main__":
    nums = [-10, -5, -20, -8]
    print(f"Max: {find_max(nums)}")
""",
                "solution_code": """def find_max(nums):
    # FIX: Initialize max_val with the first element or negative infinity
    if not nums:
        return None
    max_val = nums[0]
    for num in nums[1:]:
        if num > max_val:
            max_val = num
    return max_val

if __name__ == "__main__":
    nums = [-10, -5, -20, -8]
    print(f"Max: {find_max(nums)}")
""",
                "test_cases_json": json.dumps([
                    {
                        "input": "",
                        "expected_output": "Max: -5\n",
                        "description": "Maximum of [-10, -5, -20, -8] is -5"
                    }
                ]),
                "stages_config_json": json.dumps({
                    "stage1_understand": {
                        "en": "Why does initializing `max_val = 0` cause a problem when all numbers in the input list are negative?",
                        "hinglish": "`max_val = 0` initialize karne se negative numbers ki list me kya dikkat hoti hai?",
                        "question": "What is wrong with assuming 0 as the starting maximum for negative lists?",
                        "options": [
                            "0 is greater than all negative numbers, so max_val will never update",
                            "Python does not allow 0 in lists",
                            "Negative numbers cause a syntax error",
                            "The loop won't execute"
                        ],
                        "correct_option_index": 0
                    },
                    "stage2_investigate": {
                        "en": "Look at line 3: `max_val = 0`. Notice that 0 is not in `[-10, -5, -20, -8]`. What value should be used as the starting candidate?",
                        "hinglish": "Line 3 dekho: `max_val = 0`. 0 to hamari list me hai hi nahi. To starting maximum kise manna chahiye?",
                        "suspicious_line": 3
                    },
                    "stage3_first_hint": {
                        "en": "Instead of assuming an arbitrary number like 0, pick a real value that exists in the list.",
                        "hinglish": "Kisi arbitrary number (jaise 0) ke bajaye list ke pehle element `nums[0]` ko initial maximum mano."
                    },
                    "stage4_stronger_hint": {
                        "en": "Initialize `max_val = nums[0]`, then iterate through the remaining elements `for num in nums[1:]:`.",
                        "hinglish": "`max_val = nums[0]` se start karo aur baaki elements `nums[1:]` ko compare karo."
                    },
                    "explanation": {
                        "en": "An accumulator pattern for extremum search (max or min) must be initialized with an identity or a domain element (such as `nums[0]` or `-float('inf')`). Using an arbitrary literal like 0 introduces an invisible constraint that fails when numbers lie entirely below 0.",
                        "hinglish": "Max ya Min dhoondhte samay kabhi bhi arbitrary 0 se initialize nahi karna chahiye. Hamesha list ke pehle element `nums[0]` ya `-infinity` se initialize karna chahiye."
                    }
                })
            },
            {
                "slug": "python-infinite-while-loop",
                "title": "Countdown Timer Loop",
                "language": "python",
                "difficulty": "Beginner",
                "concept": "Loop Step & Termination",
                "mistake_category_slug": "loop_logic",
                "description": "The function `countdown(start)` prints numbers from start down to 1. However, the loop never terminates and runs forever because the counter is incremented instead of decremented.",
                "buggy_code": """def countdown(start):
    count = start
    # BUG: Incrementing inside countdown creates an infinite loop!
    while count > 0:
        print(count, end=" ")
        count += 1 # Oops!
    print("Done!")

if __name__ == "__main__":
    countdown(3)
""",
                "solution_code": """def countdown(start):
    count = start
    # FIX: Decrement count towards termination
    while count > 0:
        print(count, end=" ")
        count -= 1
    print("Done!")

if __name__ == "__main__":
    countdown(3)
""",
                "test_cases_json": json.dumps([
                    {
                        "input": "",
                        "expected_output": "3 2 1 Done!\n",
                        "description": "Counts down 3 2 1 Done!"
                    }
                ]),
                "stages_config_json": json.dumps({
                    "stage1_understand": {
                        "en": "For a `while count > 0` loop to finish, does `count` need to get larger or smaller?",
                        "hinglish": "`while count > 0` loop ko khatam hone ke liye `count` ko badhna chahiye ya ghatna?",
                        "question": "What must happen to `count` so that `count > 0` eventually becomes false?",
                        "options": [
                            "count must decrease until it reaches 0",
                            "count must increase towards infinity",
                            "count must stay constant",
                            "while loops terminate automatically"
                        ],
                        "correct_option_index": 0
                    },
                    "stage2_investigate": {
                        "en": "Look at line 6: `count += 1`. If `count` starts at 3, what values does it take on subsequent steps?",
                        "hinglish": "Line 6 dekho: `count += 1`. Agar `count` 3 se shuru ho raha hai, to agle steps me iski value kya hogi?",
                        "suspicious_line": 6
                    },
                    "stage3_first_hint": {
                        "en": "`count += 1` increases the counter (3, 4, 5, 6...). The condition `count > 0` will stay TRUE forever!",
                        "hinglish": "`count += 1` karne se count badh raha hai (3, 4, 5, ...). Condition `count > 0` hamesha TRUE rahegi aur loop kabhi nahi rukega!"
                    },
                    "stage4_stronger_hint": {
                        "en": "Change `count += 1` to `count -= 1` so the loop converges toward 0.",
                        "hinglish": "`count += 1` ko badal kar `count -= 1` karo taaki count ghat kar 0 tak pahunch sake."
                    },
                    "explanation": {
                        "en": "Every while loop requires progress toward its termination condition. Modifying the loop variant in the wrong direction (`+= 1` instead of `-= 1`) causes an infinite loop, freezing execution.",
                        "hinglish": "Har while loop me loop variable ko termination condition ki taraf aage badhana zaroori hai. Ulte direction me change karne se infinite loop ho jata hai."
                    }
                })
            }
        ]

        for p in problems:
            cursor.execute("""
            INSERT INTO problems (slug, title, language, difficulty, concept, mistake_category_slug,
                                  description, buggy_code, solution_code, test_cases_json, stages_config_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (p["slug"], p["title"], p["language"], p["difficulty"], p["concept"], p["mistake_category_slug"],
                  p["description"], p["buggy_code"], p["solution_code"], p["test_cases_json"], p["stages_config_json"]))

    # Seed some realistic initial attempts for "alex_student" to showcase real Debugging DNA data
    cursor.execute("SELECT id FROM users WHERE username = 'alex_student'")
    alex = cursor.fetchone()
    if alex:
        cursor.execute("SELECT COUNT(*) as cnt FROM attempts WHERE user_id = ?", (alex["id"],))
        if cursor.fetchone()["cnt"] == 0:
            # 1. Past attempt on array indexing (solved after 2 hints)
            cursor.execute("""
            INSERT INTO attempts (user_id, problem_id, language, initial_code, current_code,
                                  mistake_categories_json, current_stage, hints_used, is_solved,
                                  solved_independently, solution_unlocked, verification_status,
                                  created_at, updated_at)
            VALUES (?, 1, 'c', '...', '...', '["array_indexing", "off_by_one"]', 6, 2, 1, 1, 0, 'verified',
                    datetime('now', '-2 days'), datetime('now', '-2 days'))
            """, (alex["id"],))

            # 2. Another past attempt on array indexing (unsolved)
            cursor.execute("""
            INSERT INTO attempts (user_id, problem_id, language, initial_code, current_code,
                                  mistake_categories_json, current_stage, hints_used, is_solved,
                                  solved_independently, solution_unlocked, verification_status,
                                  created_at, updated_at)
            VALUES (?, 1, 'c', '...', '...', '["array_indexing"]', 4, 3, 0, 0, 0, 'failed',
                    datetime('now', '-1 day'), datetime('now', '-1 day'))
            """, (alex["id"],))

            # 3. Solved loop logic problem (solved after 1 hint)
            cursor.execute("""
            INSERT INTO attempts (user_id, problem_id, language, initial_code, current_code,
                                  mistake_categories_json, current_stage, hints_used, is_solved,
                                  solved_independently, solution_unlocked, verification_status,
                                  created_at, updated_at)
            VALUES (?, 5, 'python', '...', '...', '["loop_logic"]', 6, 1, 1, 1, 0, 'verified',
                    datetime('now', '-4 hours'), datetime('now', '-4 hours'))
            """, (alex["id"],))

    conn.commit()

if __name__ == "__main__":
    init_db()
