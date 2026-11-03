# Feature status audit

This status is based on the current source tree and the local automated tests listed in `AUTOMATED_TEST_REPORT.md`. It is not a claim that every interaction was manually clicked in a browser. A full UI browser session could not be opened in this environment.

| Feature | Existing implementation | Local evidence | Status |
|---|---|---|---|
| 1. Debugging DNA dashboard | `static/index.html`, `static/app.js`, `analyzer.py`, `/api/dna/*` | Integration checks cover authenticated DNA data, account ownership, metrics, and report endpoint | API/integration tested; browser rendering not tested |
| 2. Curated practice catalog | `static/index.html`, `static/app.js`, `database.py`, `/api/problems` | Integration suite exercises catalog and starting a practice attempt | API/integration tested; browser rendering not tested |
| 3. Guided debugging and Hint Ladder | `tutor_engine.py`, `server.py`, `static/app.js` | Integration suite exercises stage persistence, hints, verification, explanation and answer restrictions | API/integration tested; live AI not tested |
| 4. Custom diagnosis, editor and execution | `code_executor.py`, `analyzer.py`, `server.py`, `static/app.js` | Executor regression tests cover Python, C, C++, Java, compile/runtime failures, stdin and bounded Windows fallback; integration tests cover diagnosis | Locally tested; native C/C++ execution may be blocked by Windows Application Control, with only the documented safe subset handled by fallback |
| 5. AI Bug Injection Challenge | `server.py`, `feature5.py`, `static/feature5.js` | Integration tests cover local curated fallback, challenge persistence, prediction, identification, verification, scoring, history, ownership; validation tests cover AI-candidate parsing and execution | Local paths tested; real provider and Render endpoint not tested |
| 6. Debugging IQ / learning metrics | `analyzer.py`, `static/app.js`, dashboard API | Integration tests validate saved-data-derived metrics; the distinct Feature 6 numbering is not consistently defined in source | Data/API tested; feature numbering remains ambiguous |
| 7. Error Replay | `analyzer.py`, `static/app.js`, insights API | Integration suite checks saved replay events, chronology, and Feature 5 replay restoration | Data/API tested; browser interaction not tested |
| 8. Personalized learning insights / study plan | `analyzer.py`, `static/features9_10.js`, `static/index.html` | Integration tests exercise real saved evidence and insight/plan output | Data/API tested; distinct Feature 8 contract is not consistently defined in source |
| 9. Confusion Heatmap | `analyzer.py`, `static/features9_10.js`, progress API | Focused analytics tests and full integration suite cover saved-data-derived statuses and ownership | Data/API tested; browser rendering not tested |
| 10. Programmer Skill Tree | `analyzer.py`, `static/features9_10.js`, progress API | Focused analytics tests and full integration suite cover prerequisite/progression evidence | Data/API tested; browser rendering not tested |
| 11. Interview Mode | `server.py`, `static/features11_12.js`, interview tables | Full unittest suite exercises owner scoping, timer/expiry, run/submit, report and history | API/integration tested; browser interaction not tested |
| 12. Hidden Bug Competition | `server.py`, `static/features11_12.js`, competition tables | Full unittest suite exercises creation, invite/join, lobby, ready/start, rounds, submissions, scoring, history and leaderboard | API/integration tested; browser interaction not tested |

The source explicitly identifies Features 1–5 and 9–12; separate definitions for Features 6 and 8 are not reliably named. The table maps their current UI/data surfaces without inventing additional behavior or contracts.
