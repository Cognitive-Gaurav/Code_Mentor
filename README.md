# CodeMentor

CodeMentor is a Python learning application that helps students investigate and repair programming mistakes through guided debugging, code execution, challenges, and saved progress.

## Features

- Dashboard with account-scoped Debugging DNA, metrics, and personalized insights.
- Practice catalog, custom diagnosis, and a code editor with Python, C, C++, and Java execution where the toolchain is installed.
- An eight-stage debugging journey: Understand, Investigate, First Hint, Stronger Hint, Code Attempt, Verification, Explanation, and Answer.
- Hint Ladder, Explain Your Fix, and backend-controlled complete-solution access.
- Bug Injection challenges with prediction, bug identification, validation, and saved history.
- Error Replay, progress analytics, confusion heatmap, study plan, and skill tree.
- Timed Interview Mode and Hidden Bug Competition with saved reports/history.
- Four primary UI sections: Dashboard, Practice Lab, Challenges, and My Progress.

## Technology and requirements

- Python 3.10 or newer; standard-library HTTP server and SQLite.
- HTML, CSS, and vanilla JavaScript.
- No third-party Python packages are required (`requirements.txt` is informational only).
- Optional AI providers: OpenAI and Groq. External requests may incur provider charges.
- Optional code toolchains: GCC for C, G++ for C++, and a JDK (`javac` and `java`) for Java. Python uses the running Python interpreter.

The code executor uses temporary directories, bounded process timeouts, sanitized child environments, output/path sanitization, and available OS resource limits. It is **not** a production-grade sandbox for arbitrary untrusted public code; it does not provide per-run container or syscall/network isolation.

## Project layout

```text
server.py                 HTTP routes and startup
database.py               SQLite schema, initialization, and authentication helpers
analyzer.py               Mistake classification and progress analytics
code_executor.py           Python/C/C++/Java compilation and execution
tutor_engine.py            AI provider routing, hint progression, local fallback
feature5.py                Bug Injection challenge persistence and scoring
static/                    HTML, CSS, and browser JavaScript
test_suite.py              In-process API/integration checks
test_challenge_validation.py  Generated challenge validation tests
test_code_executor.py      Code execution regression tests
test_features9_10.py       Progress analytics tests
test_feature11_12.py       Interview/competition tests
test_auth_persistence.py   Authentication persistence tests
```

Private/local state such as `.env`, SQLite files, backups, build outputs, and Python caches is not part of the portable package. The server creates a new configured database on first startup when one is absent; it does not import your existing local user records into a new install.

## Start on Windows

Extract the portable ZIP to a writable folder. Open PowerShell or Command Prompt in the extracted folder and run:

```bat
setup_windows.bat
start_windows.bat
```

Open the URL printed by the server (default: `http://localhost:8000`). `setup_windows.bat` checks Python and creates `.env` from the safe placeholder template only if `.env` does not already exist. It does not install packages or touch a database. The server initializes its configured database at startup. Keep the server terminal open while using the application; press Ctrl+C to stop it.

You can also start the backend directly:

```powershell
python server.py
```

The existing `run.bat` launcher remains available.

## Configuration

Set environment variables in an untracked `.env` file or in the shell before starting the server. The backend loads `.env` without overriding shell variables. Never commit real API keys or private databases.

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `127.0.0.1` on Windows; `0.0.0.0` on Linux | Bind address |
| `PORT` | `8000` | HTTP listening port |
| `DATABASE_PATH` | `codementor.db` in project root | SQLite database file; use persistent storage on hosted deployments |
| `AI_PROVIDER` | `openai` | Preferred provider: `openai`, `groq`, or `local` |
| `OPENAI_API_KEY` | unset | Optional OpenAI credential |
| `OPENAI_MODEL` | `gpt-6-luna` | OpenAI model name used by the backend |
| `GROQ_API_KEY` | unset | Optional Groq credential/fallback |
| `FREE_ONLY_MODE` | `true` | Enables configured external provider routing; it does not guarantee free usage |

See `.env.example` for a credential-free template. An AI key being configured does not mean the provider connection has been tested.

## Tests (disposable database only)

Tests write to SQLite. Never point `DATABASE_PATH` at your real `codementor.db` when running the integration suite. In PowerShell:

```powershell
$env:DATABASE_PATH = Join-Path $env:TEMP ("codementor-test-" + [guid]::NewGuid().ToString("N") + ".sqlite3")
$env:OPENAI_API_KEY = ""
$env:GROQ_API_KEY = ""
$env:FREE_ONLY_MODE = "false"
python -c "import database, server; database.init_db(); server.init_store()"
python -m unittest -v test_challenge_validation test_code_executor
python -m unittest
python test_suite.py
```

Each command must run in the same shell so the disposable database environment remains set. Tests that construct their own temporary databases continue to use isolated files. Do not copy the resulting test database into a deployment or the portable ZIP.

## Deployment notes

`Dockerfile`, `docker-entrypoint.sh`, `render.yaml`, and `Procfile` are included for deployment environments. For Render, SQLite data survives redeployment only when the service uses persistent storage and `DATABASE_PATH` points inside the configured mount. Review and back up an existing database before changing its path. Do not assume that a successful local test verifies the deployed service or provider configuration.

## Privacy and safety

- `.env`, SQLite databases, backups, logs, caches, and local sandbox contents are excluded from the portable ZIP.
- Do not force-add those private files to Git.
- Do not run public arbitrary code without an actual isolation sandbox.
- No commit, push, or deployment is performed by the local setup or test commands above.
