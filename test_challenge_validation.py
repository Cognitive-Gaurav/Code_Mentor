"""Focused regression tests for AI-generated challenge validation and retries."""

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import code_executor
import server


class ChallengeValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._sandbox = tempfile.TemporaryDirectory(prefix="codementor_challenge_tests_")
        cls._original_sandbox = code_executor.BASE_SANDBOX_DIR
        code_executor.BASE_SANDBOX_DIR = cls._sandbox.name

    @classmethod
    def tearDownClass(cls):
        code_executor.BASE_SANDBOX_DIR = cls._original_sandbox
        cls._sandbox.cleanup()

    @staticmethod
    def candidate(language, solution=None, tests=None, title="Boundary Label"):
        programs = {
            "python": (
                "number = int(input())\nprint('Non-negative' if number >= 0 else 'Negative')\n",
                "number = int(input())\nprint('Non-negative' if number > 0 else 'Negative')\n",
            ),
            "c": (
                '#include <stdio.h>\nint main(void) { int number; if (scanf("%d", &number) != 1) return 1; puts(number >= 0 ? "Non-negative" : "Negative"); return 0; }\n',
                '#include <stdio.h>\nint main(void) { int number; if (scanf("%d", &number) != 1) return 1; puts(number > 0 ? "Non-negative" : "Negative"); return 0; }\n',
            ),
            "java": (
                'import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner scanner = new Scanner(System.in); int number = scanner.nextInt(); System.out.println(number >= 0 ? "Non-negative" : "Negative"); } }\n',
                'import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner scanner = new Scanner(System.in); int number = scanner.nextInt(); System.out.println(number > 0 ? "Non-negative" : "Negative"); } }\n',
            ),
        }
        correct, buggy = programs[language]
        return {
            "title": title,
            "description": "Read an integer and label it Non-negative when it is at least zero; otherwise label it Negative.",
            "topic": "conditional logic",
            "category": "incorrect_conditions",
            "solution_code": solution or correct,
            "buggy_code": buggy,
            "test_cases": tests or [
                {"input": "0\n", "expected_output": "Non-negative\n"},
                {"input": "-2\n", "expected_output": "Negative\n"},
                {"input": "7\n", "expected_output": "Non-negative\n"},
            ],
        }

    def test_reference_and_bug_are_verified_for_each_challenge_language(self):
        for language in ("python", "c", "java"):
            with self.subTest(language=language):
                validated, reason = server._validate_generated_challenge(
                    self.candidate(language), language, "incorrect_conditions"
                )
                self.assertIsNone(reason)
                self.assertIsNotNone(validated)
                self.assertTrue(validated["reference_result"]["all_passed"])
                self.assertTrue(validated["buggy_result"]["is_compiled"])
                self.assertFalse(validated["buggy_result"]["all_passed"])

    def test_c_challenge_validation_survives_windows_binary_execution_block(self):
        real_run = code_executor.subprocess.run

        def block_compiled_executable(command, *args, **kwargs):
            executable = command[0] if isinstance(command, (list, tuple)) and command else ""
            if str(executable).lower().endswith(("main.exe", "main.out")):
                raise PermissionError("Simulated Windows Application Control block")
            return real_run(command, *args, **kwargs)

        with patch.object(code_executor.subprocess, "run", side_effect=block_compiled_executable):
            validated, reason = server._validate_generated_challenge(
                self.candidate("c"), "c", "incorrect_conditions"
            )
        self.assertIsNone(reason)
        self.assertIsNotNone(validated)
        self.assertTrue(validated["reference_result"]["all_passed"])
        self.assertFalse(validated["buggy_result"]["all_passed"])

    def test_incorrect_reference_solution_is_rejected(self):
        challenge = self.candidate("python", solution="number = int(input())\nprint('Negative')\n")
        diagnostics = {}
        validated, reason = server._validate_generated_challenge(challenge, "python", "incorrect_conditions", diagnostics)
        self.assertIsNone(validated)
        self.assertEqual(reason, "reference_solution_failed_tests")
        self.assertGreater(diagnostics["failed_case_count"], 0)

    def test_incorrect_expected_output_is_rejected_with_actual_output_feedback(self):
        challenge = self.candidate("python", tests=[
            {"input": "0\n", "expected_output": "Negative\n"},
            {"input": "-2\n", "expected_output": "Negative\n"},
        ])
        diagnostics = {}
        validated, reason = server._validate_generated_challenge(challenge, "python", "incorrect_conditions", diagnostics)
        self.assertIsNone(validated)
        self.assertEqual(reason, "reference_solution_failed_tests")
        self.assertEqual(diagnostics["failed_cases"][0]["expected"], "Negative\n")
        self.assertEqual(diagnostics["failed_cases"][0]["actual"], "Non-negative\n")
        self.assertEqual(diagnostics["failed_cases"][0]["failure_type"], "output_mismatch")

    def test_blank_expected_output_is_rejected_before_run_only_mode_can_accept_it(self):
        challenge = self.candidate("python", tests=[
            {"input": "0\n", "expected_output": "  \n"},
            {"input": "-2\n", "expected_output": "Negative\n"},
        ])
        validated, reason = server._validate_generated_challenge(challenge, "python", "incorrect_conditions")
        self.assertIsNone(validated)
        self.assertEqual(reason, "empty_expected_output")

    def test_one_failed_case_is_identified_without_dropping_other_cases(self):
        challenge = self.candidate("python", solution="number = int(input())\nprint(number * 2)\n", tests=[
            {"input": "2\n", "expected_output": "4\n"},
            {"input": "3\n", "expected_output": "6\n"},
            {"input": "4\n", "expected_output": "9\n"},
        ])
        diagnostics = {}
        validated, reason = server._validate_generated_challenge(challenge, "python", "incorrect_conditions", diagnostics)
        self.assertIsNone(validated)
        self.assertEqual(reason, "reference_solution_failed_tests")
        self.assertEqual(diagnostics["failed_case_count"], 1)
        self.assertEqual(diagnostics["failed_cases"][0]["case"], 3)

    def test_input_format_mismatch_reports_runtime_failure(self):
        challenge = self.candidate("python", solution="left, right = map(int, input().split())\nprint(left + right)\n", tests=[
            {"input": "4\n", "expected_output": "4\n"},
            {"input": "3 1\n", "expected_output": "4\n"},
        ])
        diagnostics = {}
        validated, reason = server._validate_generated_challenge(challenge, "python", "incorrect_conditions", diagnostics)
        self.assertIsNone(validated)
        self.assertEqual(reason, "reference_solution_failed_tests")
        self.assertIn("error", diagnostics["failed_cases"][0])
        self.assertEqual(diagnostics["failed_cases"][0]["input"], "4\n")

    def test_retry_runtime_feedback_keeps_exception_and_omits_traceback_source(self):
        traceback = (
            "Runtime Exit Code 1: Traceback (most recent call last):\n"
            '  File "<sandbox>/solution.py", line 1, in <module>\n'
            "    raise ValueError('PRIVATE_SOURCE_SNIPPET')\n"
            "ValueError: invalid literal for int() with base 10: 'bad input'\n"
        )
        feedback = server._reference_test_failure_feedback({
            "is_compiled": True,
            "test_results": [{
                "passed": False, "error": traceback, "actual_output": "",
            }],
        }, [{"input": "bad input\n", "expected_output": "0\n"}])
        error = feedback["failed_cases"][0]["error"]
        self.assertIn("ValueError:", error)
        self.assertIn("invalid literal for int()", error)
        self.assertNotIn("Traceback", error)
        self.assertNotIn("PRIVATE_SOURCE_SNIPPET", error)
        self.assertLessEqual(len(error), 180)

    def test_java_runtime_stderr_is_safely_classified_for_logs(self):
        challenge = self.candidate("java", solution=(
            'public class Main { public static void main(String[] args) {'
            'throw new NumberFormatException("PRIVATE_INPUT_VALUE"); } }'
        ))
        validation = {}
        execution = {}
        validated, reason = server._validate_generated_challenge(
            challenge, "java", "incorrect_conditions", validation, execution
        )
        self.assertIsNone(validated)
        self.assertEqual(reason, "reference_solution_failed_tests")
        self.assertEqual(len(execution["failed_errors"]), 3)
        details = server._reference_failure_log_details("java", validation, execution)
        case = details["failed_cases"][0]
        self.assertEqual(case["error_category"], "java_runtime_exception")
        self.assertEqual(case["exception_class"], "NumberFormatException")
        self.assertEqual(case["exception_message"], "numeric input format is invalid (input value redacted)")
        serialized = json.dumps(details)
        self.assertNotIn("PRIVATE_INPUT_VALUE", serialized)
        self.assertNotIn("Main.java", serialized)

    def test_java_compiler_stderr_is_safely_classified_for_logs(self):
        challenge = self.candidate("java", solution=(
            'public class Main { public static void main(String[] args) {'
            'int count = "PRIVATE_SOURCE_VALUE"; System.out.println(count); } }'
        ))
        validation = {}
        execution = {}
        validated, reason = server._validate_generated_challenge(
            challenge, "java", "incorrect_conditions", validation, execution
        )
        self.assertIsNone(validated)
        self.assertEqual(reason, "reference_solution_failed_tests")
        details = server._reference_failure_log_details("java", validation, execution)
        case = details["failed_cases"][0]
        self.assertEqual(case["error_category"], "java_compile_type_mismatch")
        self.assertEqual(case["exception_class"], "JavacError")
        self.assertEqual(case["exception_message"], "javac found incompatible types")
        serialized = json.dumps(details)
        self.assertNotIn("PRIVATE_SOURCE_VALUE", serialized)
        self.assertNotIn("Main.java", serialized)

    def test_reference_failure_log_redacts_values_paths_and_control_characters(self):
        details = server._reference_failure_log_details("python", {
            "failed_case_count": 1,
            "failed_cases": [{
                "case": 2,
                "failure_type": "runtime_error",
                "error": "Runtime Exit Code 1: Traceback (most recent call last):\n"
                         '  File "C:\\private\\candidate.py", line 1\n'
                         "ValueError: invalid value 'PRIVATE_INPUT' at C:\\private\\data.txt\r\n",
            }],
        })
        serialized = json.dumps(details)
        self.assertEqual(details["failed_cases"][0]["error_category"], "runtime_exception")
        self.assertEqual(details["failed_cases"][0]["exception_class"], "ValueError")
        self.assertFalse(details["failed_cases"][0]["timed_out"])
        self.assertFalse(details["failed_cases"][0]["process_launch_failed"])
        self.assertNotIn("PRIVATE_INPUT", serialized)
        self.assertNotIn("C:\\\\private", serialized)
        self.assertNotIn("candidate.py", serialized)
        self.assertNotIn("\\r", serialized)
        self.assertLessEqual(len(details["failed_cases"][0]["exception_message"]), 160)

        failure_flags = server._reference_failure_log_details("java", {
            "failed_case_count": 2,
            "failed_cases": [
                {"case": 1, "failure_type": "runtime_error",
                 "error": "Execution timed out (> 3.0s). Suspected infinite loop."},
                {"case": 2, "failure_type": "runtime_error",
                 "error": "Execution failed: [Errno 2] No such file or directory: '/usr/bin/java'"},
            ],
        })["failed_cases"]
        self.assertEqual(failure_flags[0]["error_category"], "timeout")
        self.assertTrue(failure_flags[0]["timed_out"])
        self.assertFalse(failure_flags[0]["process_launch_failed"])
        self.assertEqual(failure_flags[1]["error_category"], "process_launch_failure")
        self.assertTrue(failure_flags[1]["process_launch_failed"])
        self.assertFalse(failure_flags[1]["timed_out"])

        java_failures = [
            ("Runtime Exit Code 1: Could not reserve enough space for 262144KB object heap",
             "jvm_memory_reservation_failure", "JVMStartupError", "Java VM memory reservation failed"),
            ("Runtime Exit Code 1: Error: Main method not found in class Main; private details",
             "java_entry_point_failure", "MainMethodError", "Java entry point is missing or invalid"),
            ("Runtime Exit Code 1: Error: Could not find or load main class Main",
             "java_class_loading_failure", "ClassNotFoundException", "Java class loading failed"),
        ]
        for error, expected_category, expected_class, expected_message in java_failures:
            with self.subTest(category=expected_category):
                details = server._reference_failure_log_details("java", {
                    "failed_case_count": 1,
                    "failed_cases": [{"case": 1, "failure_type": "runtime_error", "error": error}],
                })["failed_cases"][0]
                self.assertEqual(details["error_category"], expected_category)
                self.assertEqual(details["exception_class"], expected_class)
                self.assertEqual(details["exception_message"], expected_message)
                self.assertNotIn("private details", details["exception_message"])

    def test_reference_runtime_failure_emits_safe_structured_diagnostics(self):
        broken = self.candidate(
            "python", solution="raise ValueError('PRIVATE_GENERATED_CODE')\n",
            tests=[
                {"input": "secret-one\n", "expected_output": "1\n"},
                {"input": "secret-two\n", "expected_output": "2\n"},
                {"input": "secret-three\n", "expected_output": "3\n"},
            ],
        )
        ai_result = {"text": json.dumps(broken), "provider": "openai", "model": "test", "provider_attempts": []}
        with self.assertLogs("codementor.ai", level="WARNING") as captured_logs:
            with patch.object(server, "_recent_bug_challenges", return_value=[]), \
                 patch.object(server, "_local_bug_challenge", return_value=None), \
                 patch.object(server.tutor_engine, "generate_ai", return_value=ai_result):
                response, status = server._generate_diverse_bug_challenge(17, {
                    "language": "python", "difficulty": "Easy", "category": "incorrect_conditions",
                })

        self.assertEqual(status, 503)
        self.assertIn("error", response)
        logged = "\n".join(captured_logs.output)
        self.assertIn("challenge_reference_validation_failed", logged)
        self.assertIn('"language":"python"', logged)
        self.assertIn('"failed_case_count":3', logged)
        self.assertIn('"error_category":"runtime_exception"', logged)
        self.assertIn('"exception_class":"ValueError"', logged)
        self.assertIn('"timed_out":false', logged)
        self.assertIn('"process_launch_failed":false', logged)
        self.assertNotIn("PRIVATE_GENERATED_CODE", logged)
        self.assertNotIn("secret-one", logged)
        self.assertNotIn("secret-two", logged)
        self.assertNotIn("secret-three", logged)

    def test_only_line_endings_and_trailing_whitespace_are_normalized(self):
        valid = code_executor.execute_code("print('A  B')", "python", [{
            "input": "", "expected_output": "A  B  \r\n",
        }])
        invalid = code_executor.execute_code("print('A B')", "python", [{
            "input": "", "expected_output": "A  B\n",
        }])
        self.assertTrue(valid["all_passed"])
        self.assertFalse(invalid["all_passed"])

    def test_compilation_and_runtime_failures_are_rejected(self):
        compile_failure = self.candidate("c", solution="#include <stdio.h>\nint main( { return 0; }\n")
        diagnostics = {}
        validated, reason = server._validate_generated_challenge(compile_failure, "c", "incorrect_conditions", diagnostics)
        self.assertIsNone(validated)
        self.assertEqual(reason, "reference_solution_failed_tests")
        self.assertFalse(diagnostics["compiled"])
        self.assertEqual(diagnostics["failed_cases"][0]["failure_type"], "compilation_error")
        self.assertIn("input", diagnostics["failed_cases"][0])
        self.assertIn("expected", diagnostics["failed_cases"][0])
        self.assertIn("actual", diagnostics["failed_cases"][0])
        self.assertIn("error", diagnostics["failed_cases"][0])
        self.assertTrue(diagnostics.get("execution_error"))

        runtime_failure = self.candidate("python", solution="raise RuntimeError('expected test failure')\n")
        diagnostics = {}
        validated, reason = server._validate_generated_challenge(runtime_failure, "python", "incorrect_conditions", diagnostics)
        self.assertIsNone(validated)
        self.assertEqual(reason, "reference_solution_failed_tests")
        self.assertTrue(diagnostics["failed_cases"][0].get("error"))
        self.assertEqual(diagnostics["failed_cases"][0]["failure_type"], "runtime_error")
        self.assertIn("actual", diagnostics["failed_cases"][0])

        syntax_failure = self.candidate("python", solution="if True print('missing colon')\n")
        diagnostics = {}
        validated, reason = server._validate_generated_challenge(syntax_failure, "python", "incorrect_conditions", diagnostics)
        self.assertIsNone(validated)
        self.assertEqual(reason, "reference_solution_failed_tests")
        self.assertFalse(diagnostics["compiled"])
        self.assertEqual(diagnostics["failed_cases"][0]["failure_type"], "compilation_error")

    def test_retry_prompt_includes_specific_reference_failure_feedback(self):
        first = self.candidate("python", solution="number = int(input())\nprint('wrong')\n", title="Broken Reference")
        second = self.candidate("python", title="Corrected Reference")

        class FakeConnection:
            def __init__(self):
                self.inserted = False

            def execute(self, sql, params=()):
                self.inserted = "INSERT INTO problems" in sql
                return SimpleNamespace(lastrowid=812)

            def commit(self):
                pass

            def close(self):
                pass

        fake_connection = FakeConnection()
        ai_results = [
            {"text": json.dumps(first), "provider": "openai", "model": "test", "provider_attempts": []},
            {"text": json.dumps(second), "provider": "openai", "model": "test", "provider_attempts": []},
        ]
        with self.assertLogs("codementor.ai", level="WARNING") as captured_logs:
            with patch.object(server, "_recent_bug_challenges", return_value=[]), \
                 patch.object(server.database, "get_connection", return_value=fake_connection), \
                 patch.object(server.tutor_engine, "generate_ai", side_effect=ai_results) as generate:
                response, status = server._generate_diverse_bug_challenge(17, {
                    "language": "python", "difficulty": "Easy", "category": "incorrect_conditions",
                })

        self.assertEqual(status, 200)
        self.assertTrue(fake_connection.inserted)
        self.assertEqual(response["challenge"]["source"], "openai")
        self.assertEqual(generate.call_count, 2)
        retry_prompt = generate.call_args_list[1].args[0]
        self.assertIn("reference_solution_failed_tests", retry_prompt)
        self.assertIn('"case": 1', retry_prompt)
        self.assertIn('"input": "0\\n"', retry_prompt)
        self.assertIn('"expected": "Non-negative\\n"', retry_prompt)
        self.assertIn('"actual": "wrong\\n"', retry_prompt)
        self.assertIn("wrong", retry_prompt)
        self.assertIn('"failure_type": "output_mismatch"', retry_prompt)
        self.assertIn('"error":', retry_prompt)
        log_text = "\n".join(captured_logs.output)
        self.assertIn("failed_case_count=3", log_text)
        self.assertIn("failure_types=output_mismatch", log_text)
        self.assertNotIn("wrong", log_text)
        self.assertNotIn("Non-negative", log_text)

    def test_retry_prompt_includes_expected_output_mismatch_details(self):
        challenge = self.candidate("python", tests=[
            {"input": "0\n", "expected_output": "Negative\n"},
            {"input": "-2\n", "expected_output": "Negative\n"},
        ])
        diagnostics = {}
        validated, reason = server._validate_generated_challenge(
            challenge, "python", "incorrect_conditions", diagnostics
        )
        retry_prompt = server._challenge_generation_prompt("python", "Easy", "incorrect_conditions", [], [{
            "category": "incorrect_conditions", "reason": reason, "validation_feedback": diagnostics,
        }])
        self.assertIsNone(validated)
        self.assertIn('"case": 1', retry_prompt)
        self.assertIn('"input": "0\\n"', retry_prompt)
        self.assertIn('"expected": "Negative\\n"', retry_prompt)
        self.assertIn('"actual": "Non-negative\\n"', retry_prompt)
        self.assertIn('"failure_type": "output_mismatch"', retry_prompt)


if __name__ == "__main__":
    unittest.main()
