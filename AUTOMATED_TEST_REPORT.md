# Automated test report

Run locally on 2026-11-03 using Python 3.14.7, GCC/G++ 16.2.0, and Java/Javac 26. Tests used disposable databases under the Windows temporary directory. The existing `codementor.db` was not used by the test commands.

| Command/check | Result |
|---|---|
| `python -m py_compile server.py code_executor.py` | PASS |
| `python -m unittest -v test_challenge_validation test_code_executor` | PASS — 26 tests |
| `python -m unittest` | PASS — 52 tests |
| `python test_suite.py` | PASS — 260/260 integration assertions |
| Extracted ZIP setup script (`cmd /c setup_windows.bat`) | PASS — exit code 0; created `.env` from the placeholder template in the disposable extracted copy |
| Extracted ZIP server smoke | PASS — `/api/health` and `/` both returned 200 using a disposable database |
| Extracted ZIP focused suite | PASS — 26 tests |
| ZIP integrity and private-file scan | PASS — 40 files; no `.env`, SQLite files, caches, backups, or source database |
| `git diff --check` before documentation changes | PASS; no whitespace errors |
| HTML parse via Python standard-library `HTMLParser` | PASS |
| Duplicate static HTML IDs | PASS — none found (132 IDs inspected) |
| Static assets in the HTML | PASS after excluding backend `/api/...` routes from file-asset checks |
| JavaScript parser/syntax check | NOT TESTED — Node.js and an installed JS parser were unavailable |
| CSS parser check | NOT TESTED — no CSS parser was installed |
| Browser console, responsive layout, click-through manual test | NOT TESTED — browser automation host unavailable |
| Live OpenAI/Groq request | NOT RUN — it may incur provider charges; test suite used local mocks/fallbacks |
| Render endpoint verification | NOT RUN — no deployment request was made |

The integration suite logs a sanitized rejection for one generated/curated C candidate during fallback selection but completes all 260 assertions successfully using the existing validation/fallback behavior. It does not establish that every possible generated challenge will validate or that the live provider succeeds.

## Database safety

- `DATABASE_PATH` was set to unique disposable temporary SQLite files for the test processes.
- The integration suite's record cleanup ran only against its disposable test database.
- The project's existing `codementor.db` was checked separately with SQLite opened in read-only mode; `PRAGMA integrity_check` returned `ok` and the users table exists.
- No production/local records were deleted, reset, migrated, or copied into the package.
