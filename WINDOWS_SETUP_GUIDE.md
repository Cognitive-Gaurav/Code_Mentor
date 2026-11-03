# Windows setup guide

1. Install Python 3.10 or newer from the official Python distribution and enable **Add Python to PATH**.
2. Extract `CodeMentor_Portable_Final.zip` to a writable directory. Keep the folder in a location where the current Windows account can write the SQLite file.
3. Open the extracted folder and run `setup_windows.bat`.
4. If you want live AI, add your own key to the newly created `.env`. Do not share or commit that file. External requests may incur charges. Local fallback remains available without a key.
5. Run `start_windows.bat` and keep the terminal open.
6. Open the URL printed by the server, normally `http://localhost:8000`.
7. Create a new account in this portable copy. Existing local account data is intentionally not included in the ZIP.

`setup_windows.bat` checks the Python version, creates `.env` only when it does not exist, and reports whether optional GCC/G++/JDK commands are on PATH. It does not install dependencies or create/change a database. On first server startup, CodeMentor initializes the configured SQLite database if needed.

To use a different database location or port, set `DATABASE_PATH`/`PORT` in `.env` or in the shell before starting. Back up the database before changing its location. Do not place the database in a folder synchronized by multiple running server instances.

Optional languages:

- Python works through the Python interpreter running the server.
- C requires GCC (`gcc`).
- C++ requires G++ (`g++`).
- Java requires a JDK providing `javac` and `java`.

The Windows executor may encounter Windows Application Control restrictions on freshly compiled unsigned executables. In that case, a narrowly limited safe fallback covers some basic patterns; unsupported native programs remain failed/unverified rather than being treated as passing.
