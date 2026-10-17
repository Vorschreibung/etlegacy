#!/usr/bin/env python3
"""Run Clang Static Analyzer with on-demand cross translation unit (CTU) imports.

Minimal example (expects a division-by-zero finding across two C files):
    pixi run -e libclang static-analysis --example

Project usage with a configured CMake Makefile/Ninja build:
    cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    pixi run -e libclang static-analysis
    pixi run -e libclang static-analysis --target qagame
    pixi run -e libclang static-analysis --compile-commands build/compile_commands.json

Build normally first if the configuration needs generated headers. Only compiled
sources under src/ are analyzed, using their CMake flags and working directories.
CTU imports stay within each CMake target to avoid mixing engine/mod definitions.
Prefer a Clang-configured build if its GCC-specific flags are incompatible.
Diagnostics go to the terminal and plist files under build/static-analysis/run-*.
Per-file failures are summarized after the remaining files and targets finish.
Findings do not change the exit status; any failed step returns nonzero.

CTU protocol: https://clang.llvm.org/docs/analyzer/user-docs/CrossTranslationUnit.html
"""

import argparse
import json
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from xml.parsers.expat import ExpatError

from clang.cindex import CompilationDatabase


ROOT = Path(__file__).resolve().parent.parent


def record_failure(failures, source, phase, error):
    """Keep failures concise; Clang already prints its diagnostics to stderr."""
    if isinstance(error, subprocess.CalledProcessError):
        reason = (
            f"exit status {error.returncode}"
            if error.returncode >= 0
            else f"terminated by signal {-error.returncode}"
        )
    else:
        reason = str(error)
    failures.append((source, phase, reason))
    print(f"Failed {phase} for {source}: {reason}", file=sys.stderr, flush=True)


def analyze(entries, output, clang, mapping):
    """Index external definitions before analyzing any source in this target."""
    output.mkdir(parents=True)
    definitions = defaultdict(set)
    invocations = {}
    failures = []
    for source, directory, flags in entries:
        try:
            result = subprocess.run(
                [mapping, str(source), "--", *flags],
                cwd=directory,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            )
            source_definitions = []
            for line in result.stdout.splitlines():
                # USRs can contain spaces; their byte length precedes the first ':'.
                length, rest = line.encode().split(b":", 1)
                boundary = int(length)
                source_definitions.append((rest[:boundary], rest[boundary + 1 :]))
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            record_failure(failures, source, "indexing", error)
            continue

        # Publish definitions only after indexing succeeds, so failed files and
        # partial tool output cannot become CTU import candidates.
        for usr, path in source_definitions:
            definitions[usr].add(path)
        # The importer also needs the original working directory for relative -I
        # paths. JSON is valid YAML, the format expected by Clang's CTU importer.
        invocations[str(source)] = [
            clang,
            "-working-directory",
            str(directory),
            *flags,
            str(source),
        ]

    # Multiple definitions are ambiguous (e.g. platform implementations). Omit
    # those symbols rather than importing an arbitrary function body.
    with (output / "externalDefMap.txt").open("wb") as stream:
        for usr, paths in sorted(definitions.items()):
            if len(paths) == 1:
                stream.write(
                    str(len(usr)).encode()
                    + b":"
                    + usr
                    + b" "
                    + next(iter(paths))
                    + b"\n"
                )
    invocation_list = output / "invocations.yaml"
    invocation_list.write_text(json.dumps(invocations, indent=2), encoding="utf-8")

    diagnostics = []
    analyzed = 0
    for index, (source, directory, flags) in enumerate(entries):
        print(f"Analyzing {source}", flush=True)
        report = output / f"{index}-{source.name}.plist"
        try:
            subprocess.run(
                [
                    clang,
                    "--analyze",
                    *flags,
                    "-Xclang",
                    "-analyzer-output=plist-multi-file",
                    "-Xclang",
                    "-analyzer-config",
                    "-Xclang",
                    "experimental-enable-naive-ctu-analysis=true,"
                    f"ctu-dir={output},ctu-invocation-list={invocation_list}",
                    str(source),
                    "-o",
                    str(report),
                ],
                cwd=directory,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            record_failure(failures, source, "analysis", error)
            continue

        # A crashed tool can leave a partial report, and a successful exit alone
        # does not guarantee usable output. Count only complete, readable reports.
        try:
            with report.open("rb") as stream:
                source_diagnostics = plistlib.load(stream)["diagnostics"]
            if not isinstance(source_diagnostics, list):
                raise ValueError("report diagnostics must be a list")
        except (OSError, ValueError, KeyError, TypeError, ExpatError) as error:
            record_failure(failures, source, "report", error)
            continue
        diagnostics.extend(source_diagnostics)
        analyzed += 1
    return diagnostics, len(invocations), analyzed, failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--example", action="store_true", help="run the tiny CTU example"
    )
    parser.add_argument(
        "--compile-commands", type=Path, default=ROOT / "build/compile_commands.json"
    )
    parser.add_argument("--target", help="analyze just this CMake target")
    parser.add_argument("--output", type=Path, default=ROOT / "build/static-analysis")
    args = parser.parse_args()

    # Resolve tools within pixi's environment so system Clang cannot supply
    # a different version than the libclang used to read compilation commands.
    clang = shutil.which("clang")
    mapping = shutil.which("clang-extdef-mapping")
    for tool in (clang, mapping):
        if tool is None or not Path(tool).resolve().is_relative_to(
            Path(sys.prefix).resolve()
        ):
            parser.error("run via: pixi run -e libclang static-analysis")

    database_path = args.compile_commands.resolve()
    if not args.example and not database_path.is_file():
        parser.error(
            f"missing {database_path}; configure CMake with CMAKE_EXPORT_COMPILE_COMMANDS=ON"
        )
    args.output.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="run-", dir=args.output.resolve()))
    source_root = ROOT / "src"
    if args.example:
        # Neither file alone reveals the bug: the caller must import the body
        # of example_zero() from the other translation unit.
        source_root = output / "example-src"
        source_root.mkdir()
        (source_root / "caller.c").write_text(
            "int example_zero(void);\n"
            "int main(void) { return 3 / example_zero(); }\n",
            encoding="utf-8",
        )
        (source_root / "callee.c").write_text(
            "int example_zero(void) { return 0; }\n", encoding="utf-8"
        )
        database_path = source_root / "compile_commands.json"
        database_path.write_text(
            json.dumps(
                [
                    {
                        "directory": str(source_root),
                        "file": str(source),
                        "arguments": [
                            clang,
                            "-std=c90",
                            "-c",
                            str(source),
                            "-o",
                            f"CMakeFiles/example.dir/{source.name}.o",
                        ],
                    }
                    for source in sorted(source_root.glob("*.c"))
                ]
            ),
            encoding="utf-8",
        )

    # libclang handles both 'command' and 'arguments' database entries, including
    # shell quoting of CMake's string-valued preprocessor definitions.
    database = CompilationDatabase.fromDirectory(str(database_path.parent))
    targets = defaultdict(list)
    for command in database.getAllCompileCommands():
        directory = Path(command.directory).resolve()
        source = (directory / command.filename).resolve()
        if not source.is_relative_to(source_root) or source.suffix not in (
            ".c",
            ".cc",
            ".cpp",
            ".cxx",
        ):
            continue
        # Use the build's sysroot and include paths, rather than pixi Clang's
        # default configuration which can select a different C library.
        flags = ["--no-default-config"]
        object_path = ""
        arguments = iter(list(command.arguments)[1:])
        for arg in arguments:
            # Keep semantic flags, dropping object/dependency output options.
            if arg in ("-o", "-MF", "-MT", "-MQ"):
                value = next(arguments)
                if arg == "-o":
                    object_path = value
            elif arg not in ("-c", "-MD", "-MMD", "-MP", command.filename, str(source)):
                flags.append(arg)
        target = re.search(r"(?:^|[/\\])CMakeFiles[/\\](.+?)\.dir[/\\]", object_path)
        if not target:
            parser.error(f"cannot identify CMake target from output: {object_path}")
        if args.target is None or args.target == target[1]:
            targets[target[1]].append((source, directory, flags))
    if not targets:
        parser.error(
            "no matching CMake sources under src/ (check --target and database)"
        )

    diagnostics = []
    indexed = analyzed = 0
    failures = []
    print(f"Reports: {output}", flush=True)
    for target, entries in sorted(targets.items()):
        print(f"Target {target}: {len(entries)} translation units", flush=True)
        target_diagnostics, target_indexed, target_analyzed, target_failures = analyze(
            entries, output / target, clang, mapping
        )
        diagnostics.extend(target_diagnostics)
        indexed += target_indexed
        analyzed += target_analyzed
        failures.extend((target, *failure) for failure in target_failures)
    if args.example and not any(
        diagnostic["check_name"] == "core.DivideZero" for diagnostic in diagnostics
    ):
        failures.append(
            ("example", source_root / "caller.c", "example", "expected core.DivideZero")
        )

    # Keep the final coverage and failure status visible even after later files
    # succeed. Findings from successful reports do not themselves fail the run.
    total = sum(len(entries) for entries in targets.values())
    print("\nStatic analysis summary:")
    print(f"  Indexed: {indexed}/{total} translation units")
    print(f"  Analyzed: {analyzed}/{total} translation units with usable reports")
    print(f"  Diagnostics: {len(diagnostics)}")
    print(f"  Failed steps: {len(failures)}")
    for target, source, phase, reason in failures:
        print(f"  [{target}] {source} ({phase}): {reason}")
    if failures:
        print("  Analysis coverage is incomplete because some steps failed.")
    print(f"Reports: {output}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Static analysis failed: {error}", file=sys.stderr)
        sys.exit(1)
