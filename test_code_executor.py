"""Focused, side-effect-free regression tests for supported code execution."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

import code_executor


class CodeExecutorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._sandbox_root = tempfile.TemporaryDirectory(prefix="codementor_executor_tests_")
        cls._previous_sandbox = code_executor.BASE_SANDBOX_DIR
        code_executor.BASE_SANDBOX_DIR = cls._sandbox_root.name

    @classmethod
    def tearDownClass(cls):
        code_executor.BASE_SANDBOX_DIR = cls._previous_sandbox
        cls._sandbox_root.cleanup()

    def run_code(self, code, language, input_text="", expected=""):
        return code_executor.execute_code(
            code, language, [{"input": input_text, "expected_output": expected}]
        )

    def run_with_blocked_native_launch(self, code, language, input_text="", expected=""):
        real_run = code_executor.subprocess.run

        def block_compiled_executable(command, *args, **kwargs):
            executable = command[0] if isinstance(command, (list, tuple)) and command else ""
            if os.path.basename(str(executable)).lower() in ("main.exe", "main.out"):
                raise PermissionError("Simulated Windows Application Control block")
            return real_run(command, *args, **kwargs)

        with patch.object(code_executor.subprocess, "run", side_effect=block_compiled_executable):
            return self.run_code(code, language, input_text, expected)

    def assert_diagnostic_hides_sandbox(self, result):
        diagnostic = "\n".join((result.get("compile_error") or "", result.get("overall_error") or "", result.get("overall_output") or ""))
        self.assertNotIn(self._sandbox_root.name, diagnostic)
        self.assertNotIn(self._sandbox_root.name.replace("\\", "/"), diagnostic)
        self.assertIn("<sandbox>", diagnostic)

    def test_python_output_stdin_and_fresh_results(self):
        first = self.run_code("print(input())", "python", "first", "first")
        second = self.run_code("print('second')", "python", "", "second")
        self.assertTrue(first["all_passed"])
        self.assertTrue(second["all_passed"])
        self.assertEqual(second["overall_output"].strip(), "second")

    def test_python_runtime_error_hides_temporary_host_path(self):
        result = self.run_code("raise RuntimeError('runtime boom')", "python")
        self.assertFalse(result["all_passed"])
        self.assertIn("Runtime Exit Code", result["overall_error"])
        self.assert_diagnostic_hides_sandbox(result)

    def test_python_empty_source_is_a_valid_empty_run(self):
        result = self.run_code("", "python")
        self.assertTrue(result["is_compiled"])
        self.assertTrue(result["all_passed"])

    def test_python_timeout_is_reported(self):
        result = self.run_code("while True: pass", "python")
        self.assertFalse(result["all_passed"])
        self.assertIn("timed out", result["overall_error"].lower())

    @unittest.skipUnless(shutil.which("gcc"), "GCC is unavailable")
    def test_c_compile_run_stdin_and_compile_error_path_sanitization(self):
        valid = self.run_code('#include <stdio.h>\nint main(){int n; scanf("%d", &n); printf("%d\\n", n+1);}', "c", "4\n", "5")
        invalid = self.run_code("int main( { return 0; }", "c")
        self.assertTrue(valid["all_passed"])
        self.assertFalse(invalid["is_compiled"])
        self.assert_diagnostic_hides_sandbox(invalid)

    @unittest.skipUnless(shutil.which("gcc"), "GCC is unavailable")
    def test_c_application_control_fallback_handles_integer_printf(self):
        result = self.run_with_blocked_native_launch(
            '#include <stdio.h>\nint main(){int n; scanf("%d", &n); printf("%d\\n", n+1);}',
            "c", "4\n", "5",
        )
        self.assertTrue(result["is_compiled"])
        self.assertTrue(result["all_passed"], result["test_results"])
        self.assertEqual(result["test_results"][0]["actual_output"], "5\n")

    @unittest.skipUnless(shutil.which("g++"), "G++ is unavailable")
    def test_cpp_application_control_fallback_handles_literal_output(self):
        result = self.run_with_blocked_native_launch(
            '#include <iostream>\nint main(){std::cout << "cpp-ok\\n";}',
            "cpp", expected="cpp-ok",
        )
        self.assertTrue(result["is_compiled"])
        self.assertTrue(result["all_passed"], result["test_results"])
        self.assertEqual(result["test_results"][0]["actual_output"], "cpp-ok\n")

    @unittest.skipUnless(shutil.which("gcc") and shutil.which("g++"), "GCC/G++ is unavailable")
    def test_application_control_fallback_fails_closed_for_unsupported_expressions(self):
        c_result = self.run_with_blocked_native_launch(
            '#include <stdio.h>\nint main(){int n; scanf("%d", &n); printf("%d\\n", n*n);}',
            "c", "4\n", "16",
        )
        cpp_result = self.run_with_blocked_native_launch(
            '#include <iostream>\nint main(){int n=4; std::cout << n << "\\n";}',
            "cpp", expected="4",
        )
        self.assertTrue(c_result["is_compiled"])
        self.assertFalse(c_result["all_passed"])
        self.assertTrue(cpp_result["is_compiled"])
        self.assertFalse(cpp_result["all_passed"])

    @unittest.skipUnless(shutil.which("g++"), "G++ is unavailable")
    def test_cpp_program_runs(self):
        result = self.run_code('#include <iostream>\nint main(){std::cout << "cpp-ok\\n";}', "cpp", expected="cpp-ok")
        self.assertTrue(result["all_passed"])

    @unittest.skipUnless(shutil.which("javac") and shutil.which("java"), "JDK is unavailable")
    def test_java_compile_run_stdin_runtime_and_compile_errors(self):
        valid = self.run_code('import java.util.*; public class Learner { public static void main(String[] a){ Scanner s=new Scanner(System.in); System.out.println("hi " + s.nextLine()); } }', "java", "Ada\n", "hi Ada")
        invalid = self.run_code('public class Main { public static void main(String[] a) { System.out.println("x") } }', "java")
        runtime = self.run_code('public class Main { public static void main(String[] a) { throw new RuntimeException("boom"); } }', "java")
        self.assertTrue(valid["all_passed"])
        self.assertFalse(invalid["is_compiled"])
        self.assert_diagnostic_hides_sandbox(invalid)
        self.assertTrue(runtime["is_compiled"])
        self.assertFalse(runtime["all_passed"])
        self.assertIn("Runtime Exit Code", runtime["overall_error"])


if __name__ == "__main__":
    unittest.main()
