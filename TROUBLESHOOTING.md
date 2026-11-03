# Troubleshooting

## Browser says the site cannot be reached

- Keep the `start_windows.bat` terminal open; the backend serves the page.
- Open the host and port printed by the process. Default is `http://localhost:8000`.
- If the port is already in use, stop the other local server or set another `PORT` in `.env`, then restart.

## Python is not found

Install Python 3.10+ and enable its PATH option. Open a new terminal, run `python --version`, and rerun `setup_windows.bat`.

## C/C++/Java run is unavailable

Install and put `gcc`, `g++`, or the JDK `javac`/`java` tools on PATH. Restart the server after updating PATH. On Windows, application-control policy may block a newly compiled binary; the executor only falls back for supported safe patterns and reports unsupported cases as failures.

## Sign-in or account does not persist

Check the configured `DATABASE_PATH`. A local SQLite database survives a normal application restart when it remains at the same writable path. Hosted ephemeral filesystems may be reset on redeploy; use the hosting provider's persistent volume and set `DATABASE_PATH` inside its mount. Back up any existing database before changing configuration. This portable archive does not include the original project's user records.

## AI tutor or challenge generation falls back

Check the provider name, model, and credential in `.env` without posting the secret in a bug report. Restart the server after changing values. A configured key does not prove network connectivity or account/model access. Live requests can incur charges. AI challenge generation validates the reference implementation and can reject unsuitable AI output; rejection does not mean that invalid challenges were accepted.

## Test isolation

The integration suite changes SQLite rows. Set `DATABASE_PATH` to a disposable path under `%TEMP%` before running tests, and initialize that test database first. Never run these tests against a database containing real users.
