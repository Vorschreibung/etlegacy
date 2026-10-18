#!/usr/bin/env python3
"""Regression checks: pixi run -e libclang python misc/test_static_analysis.py."""

import contextlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# The task script has a hyphenated name, so load it without changing its CLI.
spec = importlib.util.spec_from_file_location(
    "static_analysis", Path(__file__).with_name("static-analysis.py")
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class StaticAnalysisTests(unittest.TestCase):
    def test_cli_keyboard_interrupt_exits_without_traceback(self):
        # Exercise the script's actual entry point with a deterministic interrupt
        # while launching its first tool, without racing a short-lived analysis.
        bootstrap = """
import runpy
import sys
from unittest.mock import patch
script = sys.argv.pop(1)
with patch('subprocess.Popen', side_effect=KeyboardInterrupt):
    runpy.run_path(script, run_name='__main__')
"""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    bootstrap,
                    str(Path(runner.__file__).resolve()),
                    "--example",
                    "--output",
                    tmp,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 130)
        self.assertIn("Static analysis interrupted.", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("KeyboardInterrupt", result.stderr)

    def test_parallel_tools_respect_limit_and_deliver_fast_results_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The first child blocks until the parent releases it. The second
            # child must complete first, even with output larger than pipe buffers.
            script = """
import sys
import time
from pathlib import Path
index = sys.argv[1]
Path('started-' + index).touch()
deadline = time.monotonic() + 5
if index == '0':
    while not Path('release').exists():
        if time.monotonic() > deadline:
            sys.exit(1)
        time.sleep(0.01)
elif index == '1':
    while not Path('started-0').exists():
        if time.monotonic() > deadline:
            sys.exit(1)
        time.sleep(0.01)
    sys.stdout.write('o' * 131072)
    sys.stderr.write('e' * 131072)
Path('done-' + index).touch()
"""
            commands = [
                (
                    index,
                    (root / f"{index}.c", root, []),
                    [sys.executable, "-c", script, str(index)],
                )
                for index in range(3)
            ]
            with contextlib.closing(runner.run_commands(commands, 2)) as completed:
                index, _, result = next(completed)
                self.assertEqual(index, 1)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "o" * 131072)
                self.assertEqual(result.stderr, "e" * 131072)
                self.assertFalse((root / "done-0").exists())
                self.assertFalse((root / "started-2").exists())
                (root / "release").touch()
                remaining = list(completed)
            self.assertEqual({index for index, _, _ in remaining}, {0, 2})
            self.assertTrue(all(result.returncode == 0 for _, _, result in remaining))

    def test_zero_parallelism_runs_one_tool_at_a_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = "from pathlib import Path; import sys; Path(sys.argv[1]).touch()"
            commands = [
                (
                    index,
                    (root / f"{index}.c", root, []),
                    [sys.executable, "-c", script, f"started-{index}"],
                )
                for index in range(2)
            ]
            with contextlib.closing(runner.run_commands(commands, 0)) as completed:
                self.assertEqual(next(completed)[0], 0)
                self.assertFalse((root / "started-1").exists())
                self.assertEqual([index for index, _, _ in completed], [1])

    def test_closing_scheduler_reaps_pending_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = (root / "example.c", root, [])
            scripts = [
                "from pathlib import Path; import time; Path('started').touch(); time.sleep(10)",
                """
from pathlib import Path
import time
deadline = time.monotonic() + 5
while not Path('started').exists():
    if time.monotonic() > deadline:
        raise RuntimeError('other child never started')
    time.sleep(0.01)
""",
            ]
            commands = [
                (index, entry, [sys.executable, "-c", script])
                for index, script in enumerate(scripts)
            ]
            children = []
            popen = subprocess.Popen

            def launch(*args, **kwargs):
                # Retain real process handles to verify that closing the result
                # iterator reaps the child that has not completed yet.
                child = popen(*args, **kwargs)
                children.append(child)
                return child

            with patch.object(
                runner.subprocess, "Popen", side_effect=launch
            ), contextlib.closing(runner.run_commands(commands, 2)) as completed:
                self.assertEqual(next(completed)[0], 1)
                self.assertIsNone(children[0].poll())
            self.assertTrue(all(child.poll() is not None for child in children))

    def test_tool_launch_failure_does_not_stop_remaining_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entry = (root / "example.c", root, [])
            commands = [
                (0, entry, [str(root / "missing-tool")]),
                (1, entry, [sys.executable, "-c", "print('completed')"]),
            ]
            results = list(runner.run_commands(commands, 2))
            self.assertIsInstance(results[0][2], OSError)
            self.assertEqual(results[1][2].stdout, "completed\n")

    def test_cli_rejects_negative_parallelism(self):
        with patch(
            "sys.argv", ["static-analysis", "--parallel", "-1"]
        ), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(
            SystemExit
        ) as error:
            runner.main()
        self.assertEqual(error.exception.code, 2)

    def test_cli_example_keeps_ctu_in_deep_and_shallow_modes(self):
        # Neither example TU alone exposes the division by zero. Finding it in
        # both modes verifies that the depth switch preserves CTU imports.
        with tempfile.TemporaryDirectory() as tmp:
            for options in (
                [],
                ["--shallow"],
                ["--parallel", "0"],
                ["--shallow", "--parallel", "0"],
            ):
                with self.subTest(options=options):
                    args = ["static-analysis", "--example", "--output", tmp, *options]
                    with patch("sys.argv", args), contextlib.redirect_stdout(
                        io.StringIO()
                    ) as terminal, contextlib.redirect_stderr(terminal):
                        status = runner.main()
                    self.assertEqual(status, 0)
                    self.assertIn("[core.DivideZero]", terminal.getvalue())
                    self.assertIn(
                        "Analyzed: 2/2 translation units", terminal.getvalue()
                    )
                    self.assertIn("Warnings: 1", terminal.getvalue())

    def test_checker_allowlist_excludes_unlisted_checks_in_c_and_cpp(self):
        # This source has both a dead store and a CTU-only division by zero.
        # Removing DeadStores must disable that check, even though Clang normally
        # enables it by default, without losing the requested cross-file check.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for suffix in ("c", "cpp"):
                caller = root / f"caller.{suffix}"
                callee = root / f"callee.{suffix}"
                caller.write_text(
                    "int example_zero(void);\n"
                    "int example(void) { int unused = 1; unused = 2; return 3 / example_zero(); }\n"
                )
                callee.write_text("int example_zero(void) { return 0; }\n")
                entries = [
                    (source, root, ["--no-default-config"])
                    for source in (caller, callee)
                ]
                for dead_stores in (True, False):
                    with self.subTest(suffix=suffix, dead_stores=dead_stores):
                        checkers = (
                            ("core.DivideZero", "deadcode.DeadStores")
                            if dead_stores
                            else ("core.DivideZero",)
                        )
                        findings = runner.Findings(io.StringIO(), False)
                        with patch.object(runner, "CHECKERS", checkers), patch.object(
                            runner, "COMPILER_WARNINGS", ()
                        ), contextlib.redirect_stdout(
                            io.StringIO()
                        ), contextlib.redirect_stderr(
                            io.StringIO()
                        ):
                            _, analyzed, failures = runner.analyze(
                                entries,
                                root / f"{suffix}-{dead_stores}",
                                shutil.which("clang"),
                                shutil.which("clang-extdef-mapping"),
                                findings,
                                parallel=2,
                            )
                        self.assertFalse(failures)
                        self.assertEqual(len(analyzed), 2)
                        self.assertIn("core.DivideZero", findings.checkers)
                        self.assertEqual(
                            "deadcode.DeadStores" in findings.checkers, dead_stores
                        )
                        if not dead_stores:
                            self.assertEqual(findings.counts["warning"], 1)

    def test_compiler_warning_allowlist_overrides_build_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "example.c"
            source.write_text(
                "#if RUNNER_WARNING_TEST != 1\n#error preprocessor options lost\n#endif\n"
                "#warning unlisted preprocessor warning\n"
                "int example(void) { int unused; int *p = 0; unused = 1; return *p; }\n"
            )
            # Inherited groups, promotions and global suppression must all yield
            # to the explicit list. Analyzer diagnostics remain independent.
            entries = [
                (
                    source,
                    root,
                    [
                        "--no-default-config",
                        "-Wall",
                        "-Weverything",
                        "-Werror",
                        "-Werror=unused-but-set-variable",
                        "-w",
                        "-Xclang",
                        "-Wunused-variable",
                        "-Wp,-DRUNNER_WARNING_TEST=1",
                    ],
                )
            ]
            for enabled in (True, False):
                with self.subTest(enabled=enabled):
                    warnings = ("-Wunused-but-set-variable",) if enabled else ()
                    findings = runner.Findings(io.StringIO(), False)
                    with patch.object(
                        runner, "COMPILER_WARNINGS", warnings
                    ), patch.object(
                        runner, "CHECKERS", ("core.NullDereference",)
                    ), contextlib.redirect_stdout(
                        io.StringIO()
                    ), contextlib.redirect_stderr(
                        io.StringIO()
                    ) as terminal:
                        indexed, analyzed, failures = runner.analyze(
                            entries,
                            root / str(enabled),
                            shutil.which("clang"),
                            shutil.which("clang-extdef-mapping"),
                            findings,
                        )
                    self.assertFalse(failures)
                    self.assertEqual(len(indexed), 1)
                    self.assertEqual(len(analyzed), 1)
                    self.assertEqual("set but not used" in terminal.getvalue(), enabled)
                    self.assertNotIn(
                        "warning: unlisted preprocessor warning", terminal.getvalue()
                    )
                    self.assertIn("core.NullDereference", findings.checkers)
                    self.assertEqual(findings.counts["warning"], 1 + int(enabled))

    def test_artifact_identity_and_colored_output(self):
        history = io.StringIO()
        findings = runner.Findings(history, True)
        directory = Path("/tmp")
        result = {
            "level": "warning",
            "message": {"text": "example"},
            "ruleId": "core.Example",
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {"index": 0},
                        "region": {"startLine": 4, "startColumn": 2},
                    }
                }
            ],
        }
        document = {
            "version": "2.1.0",
            "runs": [
                {
                    "artifacts": [
                        {"location": {"uri": "file:///tmp/shared%20header.h"}}
                    ],
                    "results": [result],
                }
            ],
        }
        with contextlib.redirect_stderr(io.StringIO()) as terminal:
            findings.sarif(document, directory, "client", "a.c", "analysis")
            # Equivalent relative and absolute URIs must deduplicate even when
            # different targets and per-report artifact indexes are involved.
            document["runs"][0]["artifacts"].insert(
                0, {"location": {"uri": "unused.h"}}
            )
            result["locations"][0]["physicalLocation"]["artifactLocation"] = {
                "uri": "shared%20header.h"
            }
            findings.sarif(document, directory, "server", "b.c", "analysis")
            result["level"] = "error"
            findings.sarif(document, directory, "server", "b.c", "compiler")
            findings.publish(
                {"level": "note", "message": "Additional detail"},
                "server",
                "b.c",
                "compiler",
            )
            # Notes stay with the parent; only the next primary starts a group.
            parent_and_note = terminal.getvalue().split("━" * 80)[-1]
            self.assertNotIn("─" * 80, parent_and_note)
            self.assertIn("Additional detail", parent_and_note)
            findings.publish(
                {"level": "warning", "message": "Next diagnostic"},
                "server",
                "b.c",
                "compiler",
            )
        self.assertEqual(findings.counts, {"warning": 2, "error": 1, "note": 1})
        self.assertEqual(findings.duplicates, 1)
        self.assertIn("\033[1;35mwarning:", terminal.getvalue())
        self.assertIn("\033[1;31merror:", terminal.getvalue())
        self.assertEqual(terminal.getvalue().count("─" * 80), 1)
        self.assertEqual(terminal.getvalue().count("━" * 80), 1)
        records = [json.loads(line) for line in history.getvalue().splitlines()]
        self.assertEqual(
            [record["duplicate"] for record in records],
            [False, True, False, False, False],
        )

    def test_shared_header_findings_stream_once_across_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header = root / "shared header.h"
            header.write_text(
                "#warning shared warning\n"
                "static int header_bug(void) { int *p = 0; return *p; }\n"
            )
            entries = []
            for name in ("a", "b"):
                source = root / f"{name}.c"
                source.write_text(
                    f'#include "shared header.h"\nint {name}(void) {{ return header_bug(); }}\n'
                )
                entries.append((source, root, ["--no-default-config", "-std=c90"]))
            history = io.StringIO()
            findings = runner.Findings(history, False)
            with patch.object(
                runner, "COMPILER_WARNINGS", ("-W#warnings",)
            ), contextlib.redirect_stdout(
                io.StringIO()
            ) as terminal, contextlib.redirect_stderr(
                terminal
            ):
                indexed, analyzed, failures = runner.analyze(
                    entries,
                    root / "client",
                    shutil.which("clang"),
                    shutil.which("clang-extdef-mapping"),
                    findings,
                    verbose=True,
                )
                first_warnings = findings.counts["warning"]
                runner.analyze(
                    entries,
                    root / "server",
                    shutil.which("clang"),
                    shutil.which("clang-extdef-mapping"),
                    findings,
                    parallel=2,
                )
            self.assertEqual(len(indexed), 2)
            self.assertEqual(len(analyzed), 2)
            self.assertFalse(failures)
            self.assertEqual(first_warnings, 2)
            self.assertEqual(findings.counts["warning"], 2)
            self.assertGreater(findings.duplicates, 0)
            output = terminal.getvalue()
            self.assertIn("Indexing ", output)
            self.assertEqual(output.count("warning: shared warning"), 1)
            self.assertEqual(output.count("warning: Dereference of null pointer"), 1)
            self.assertLess(
                output.index("warning: Dereference of null pointer"),
                output.index(f"Analyzing {entries[1][0]}"),
            )
            # Raw reports and every primary occurrence survive suppression.
            for report in root.glob("*/*.sarif"):
                self.assertEqual(json.loads(report.read_text())["version"], "2.1.0")
            records = [json.loads(line) for line in history.getvalue().splitlines()]
            self.assertTrue(
                any(
                    record["duplicate"] and record["target"] == "server"
                    for record in records
                )
            )

    def test_compile_errors_are_counted_once_and_do_not_stop_other_tus(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.h").write_text("static int bad(void) { return unknown; }\n")
            entries = []
            for name in ("a", "b"):
                source = root / f"{name}.c"
                source.write_text('#include "broken.h"\n')
                entries.append((source, root, ["--no-default-config"]))
            findings = runner.Findings(io.StringIO(), False)
            with patch.object(
                runner, "COMPILER_WARNINGS", ()
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ) as terminal:
                indexed, analyzed, failures = runner.analyze(
                    entries,
                    root / "errors",
                    shutil.which("clang"),
                    shutil.which("clang-extdef-mapping"),
                    findings,
                    parallel=2,
                )
            self.assertFalse(indexed)
            self.assertFalse(analyzed)
            self.assertEqual(len(failures), 4)
            self.assertEqual(findings.counts["error"], 1)
            self.assertEqual(
                terminal.getvalue().count("error: use of undeclared identifier"), 1
            )

    def test_missing_report_is_not_counted_as_analyzed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "empty.c"
            source.write_text("int f(void) { return 0; }\n")
            entries = [(source, root, ["--no-default-config"])]
            findings = runner.Findings(io.StringIO(), False)
            # Simulate an apparently successful tool that never wrote its report.
            result = subprocess.CompletedProcess([], 0, "", "")
            with patch.object(
                runner,
                "run_commands",
                side_effect=lambda commands, *args: (
                    (index, entry, result) for index, entry, _ in commands
                ),
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                indexed, analyzed, failures = runner.analyze(
                    entries, root / "missing", "clang", "clang-extdef-mapping", findings
                )
            self.assertEqual(len(indexed), 1)
            self.assertFalse(analyzed)
            self.assertEqual([failure[1] for failure in failures], ["report"])

    def test_cli_summary_counts_files_and_tus_and_defaults_to_quiet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            header = root / "src/shared.h"
            header.write_text("#warning shared warning\n")
            commands = []
            for name in ("a", "b"):
                source = root / f"src/{name}.c"
                source.write_text(
                    f'#include "shared.h"\nint {name}(void) {{ return 0; }}\n'
                )
                for target in ("client", "server"):
                    commands.append(
                        {
                            "directory": str(root),
                            "file": str(source),
                            "arguments": [
                                shutil.which("clang"),
                                "-c",
                                str(source),
                                "-o",
                                f"CMakeFiles/{target}.dir/{name}.o",
                            ],
                        }
                    )
            database = root / "compile_commands.json"
            database.write_text(json.dumps(commands))
            args = [
                "static-analysis",
                "--compile-commands",
                str(database),
                "--output",
                str(root / "reports"),
            ]
            # Exercise the actual CLI summary and failure exit status with both
            # successful analysis and the same broken header in multiple TUs.
            for broken in (False, True):
                with self.subTest(broken=broken):
                    if broken:
                        header.write_text("static int bad(void) { return unknown; }\n")
                    with patch.object(runner, "ROOT", root), patch.object(
                        runner, "COMPILER_WARNINGS", ("-W#warnings",)
                    ), patch("sys.argv", args), contextlib.redirect_stdout(
                        io.StringIO()
                    ) as terminal, contextlib.redirect_stderr(
                        terminal
                    ):
                        status = runner.main()
                    output = terminal.getvalue()
                    self.assertEqual(status, int(broken))
                    self.assertNotIn("Indexing ", output)
                    self.assertNotIn("Analyzing ", output)
                    self.assertNotIn("\033[", output)
                    self.assertIn(
                        f"Indexed: {0 if broken else 4}/4 translation units", output
                    )
                    self.assertIn(
                        f"Analyzed: {0 if broken else 4}/4 translation units", output
                    )
                    self.assertIn(f"Files indexed: {0 if broken else 2}/2", output)
                    self.assertIn(f"Files analyzed: {0 if broken else 2}/2", output)
                    self.assertIn(f"Warnings: {0 if broken else 1}", output)
                    self.assertIn(f"Errors: {1 if broken else 0}", output)
                    self.assertEqual(
                        len(list((root / "reports").glob("run-*/findings.jsonl"))),
                        1 + int(broken),
                    )

    def test_driver_errors_and_malformed_sarif(self):
        history = io.StringIO()
        findings = runner.Findings(history, False)
        document = runner.compiler_sarif(
            "clang: error: unknown argument: '-bad-option'\n", Path("/tmp")
        )
        with contextlib.redirect_stderr(io.StringIO()) as terminal:
            findings.sarif(document, Path("/tmp"), "example", "a.c", "compiler")
        self.assertEqual(findings.counts["error"], 1)
        self.assertIn("error: unknown argument", terminal.getvalue())
        with self.assertRaises(ValueError):
            findings.sarif([], Path("/tmp"), "example", "a.c", "analysis")
        with self.assertRaises(ValueError):
            runner.compiler_sarif('{"version":"2.1.0","runs":', Path("/tmp"))


if __name__ == "__main__":
    unittest.main()
