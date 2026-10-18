#!/usr/bin/env python3
"""Run Clang Static Analyzer with on-demand cross translation unit (CTU) imports.

Minimal example (expects a division-by-zero finding across two C files):
    pixi run -e libclang static-analysis --example

Project usage with a configured CMake Makefile/Ninja build:
    cmake -S . -B build -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
    pixi run -e libclang static-analysis
    pixi run -e libclang static-analysis --shallow
    pixi run -e libclang static-analysis --target qagame
    pixi run -e libclang static-analysis --compile-commands build/compile_commands.json

Build normally first if the configuration needs generated headers. Only compiled
sources under src/ are analyzed, using their CMake flags and working directories.
CTU imports stay within each CMake target to avoid mixing engine/mod definitions.
Prefer a Clang-configured build if its GCC-specific flags are incompatible.
New diagnostics print after each tool invocation, with color on terminals.
SARIF reports and a complete findings.jsonl history stay in build/static-analysis/run-*.
Duplicates are suppressed across phases, translation units and targets in a run.
Use -v/--verbose for per-file indexing and analysis progress.
Analysis defaults to deep; --shallow reduces exploration while retaining CTU.
Analyzer checkers are selected exclusively by CHECKERS below.
Compiler warnings are selected by COMPILER_WARNINGS, replacing build warning flags.
Clang processes run concurrently, using half the available CPUs up to 12.
Use --parallel N to set the process limit, or --parallel 0 to run serially.
The indexer's structured diagnostic log is converted to SARIF because its native
SARIF diagnostic mode crashes; analysis uses Clang's SARIF output directly.
Per-file failures are summarized after the remaining files and targets finish.
Findings do not change the exit status; any failed step returns nonzero.

CTU protocol: https://clang.llvm.org/docs/analyzer/user-docs/CrossTranslationUnit.html
"""

import argparse
import json
import linecache
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
from xml.parsers.expat import ExpatError

from clang.cindex import CompilationDatabase


ROOT = Path(__file__).resolve().parent.parent

# Explicit defaults for the pinned Clang release, including modeling helpers.
# C, C++ and Darwin defaults share one allowlist across build targets. List
# individual checkers so package changes cannot silently enable new checks.
CHECKERS = (
    # "apiModeling.Errno",
    # "apiModeling.OpaqueSTLFunctionsModeling",
    # "apiModeling.TrustNonnull",
    # "apiModeling.TrustReturnsNonnull",
    # "apiModeling.google.GTest",
    # "apiModeling.llvm.CastValue",
    # "apiModeling.llvm.ReturnValue",
    # "core.BitwiseShift",
    "core.CallAndMessage",
    "core.DivideZero",
    # "core.DynamicTypePropagation",
    # "core.NonNullParamChecker",
    # "core.NonnilStringConstants",
    # "core.NullDereference", # TODO
    # "core.NullPointerArithm",
    # "core.StackAddressEscape", # TODO
    # "core.UndefinedBinaryOperatorResult",
    # "core.VLASize",
    # "core.builtin.AssumeModeling",
    # "core.builtin.BuiltinFunctions",
    # Stop paths at fatal errors and failed assertions before checking arguments.
    "core.builtin.NoReturnFunctions",
    # "core.uninitialized.ArraySubscript",
    # "core.uninitialized.Assign",
    # "core.uninitialized.Branch",
    # "core.uninitialized.CapturedBlockVariable",
    # "core.uninitialized.NewArraySize",
    # "core.uninitialized.UndefReturn",
    # "cplusplus.ArrayDelete",
    # "cplusplus.InnerPointer",
    # "cplusplus.Move",
    # "cplusplus.NewDelete",
    # "cplusplus.NewDeleteLeaks",
    # "cplusplus.PlacementNew",
    # "cplusplus.PureVirtualCall",
    # "cplusplus.SelfAssignment",
    # "cplusplus.SmartPtrModeling",
    # "cplusplus.StringChecker",
    # "deadcode.DeadStores",
    # "nullability.NullPassedToNonnull",
    # "nullability.NullReturnedFromNonnull",
    # "osx.API",
    # "osx.MIG",
    # "osx.NumberObjectConversion",
    # "osx.OSObjectRetainCount",
    # "osx.ObjCProperty",
    # "osx.SecKeychainAPI",
    # "osx.cocoa.AutoreleaseWrite",
    # "osx.cocoa.ClassRelease",
    # "osx.cocoa.Dealloc",
    # "osx.cocoa.IncompatibleMethodTypes",
    # "osx.cocoa.Loops",
    # "osx.cocoa.MissingSuperCall",
    # "osx.cocoa.NSError",
    # "osx.cocoa.NilArg",
    # "osx.cocoa.NonNilReturnValue",
    # "osx.cocoa.ObjCGenerics",
    # "osx.cocoa.RetainCount",
    # "osx.cocoa.RetainCountBase",
    # "osx.cocoa.RunLoopAutoreleaseLeak",
    # "osx.cocoa.SelfInit",
    # "osx.cocoa.SuperDealloc",
    # "osx.cocoa.UnusedIvars",
    # "osx.cocoa.VariadicMethodTypes",
    # "osx.coreFoundation.CFError",
    # "osx.coreFoundation.CFNumber",
    # "osx.coreFoundation.CFRetainRelease",
    # "osx.coreFoundation.containers.OutOfBounds",
    # "osx.coreFoundation.containers.PointerSizedValues",
    # "security.ArrayBound", # TODO
    # "security.insecureAPI.SecuritySyntaxChecker",
    # "security.insecureAPI.UncheckedReturn",
    # "security.insecureAPI.decodeValueOfObjCType",
    # "security.insecureAPI.getpw",
    # "security.insecureAPI.gets",
    # "security.insecureAPI.mkstemp",
    # "security.insecureAPI.mktemp",
    # "security.insecureAPI.vfork",
    # "unix.API",
    # "unix.BlockInCriticalSection",
    # "unix.Chroot",
    # "unix.DynamicMemoryModeling",
    # "unix.Errno",
    "unix.Malloc",
    # "unix.MallocSizeof",
    # "unix.MismatchedDeallocator",
    # "unix.StdCLibraryFunctions",
    # "unix.Stream",
    # "unix.Vfork",
    # "unix.cstring.BadSizeArg",
    # "unix.cstring.CStringModeling",
    # "unix.cstring.NotNullTerminated",
    # "unix.cstring.NullArg",
    # "unix.cstring.UninitializedRead",
)


# Explicit warning controls from the pinned Clang defaults and the build's
# -Wall, -Wdeclaration-after-statement and -Wunused-but-set-variable settings.
# Individual switches keep this list editable without broad warning groups.
# Preserve error-level diagnostics with -Werror= entries.
COMPILER_WARNINGS = (
    # "-W#pragma-messages",
    # "-W#warnings",
    # "-WCFString-literal",
    # "-WIndependentClass-attribute",
    # "-WNSObject-attribute",
    # "-WTU-local-entity-exposure",
    # "-Waarch64-sme-attributes",
    # "-Wabsolute-value",
    # "-Wabstract-final-class",
    # "-Waddress-of-packed-member",
    # "-Werror=address-of-temporary",
    # "-Waix-compat",
    # "-Walias-template-in-declaration-name",
    # "-Walign-mismatch",
    # "-Walloc-size",
    # "-Walloca-with-align-alignof",
    # "-Walways-inline-coroutine",
    # "-Wambiguous-delete",
    # "-Wambiguous-ellipsis",
    # "-Wambiguous-macro",
    # "-Wambiguous-member-template",
    # "-Wambiguous-reversed-operator",
    # "-Wamdgpu-wait-event-mask",
    # "-Wanalyzer-incompatible-plugin",
    # "-Wandroid-unversioned-fallback",
    # "-Wanonymous-pack-parens",
    # "-Wapinotes",
    # "-Warc-bridge-casts-disallowed-in-nonarc",
    # "-Warc-non-pod-memaccess",
    # "-Warc-performSelector-leaks",
    # "-Warc-retain-cycles",
    # "-Warc-unsafe-retained-assign",
    # "-Werror=argument-outside-range",
    # "-Wargument-undefined-behaviour",
    # "-Warm-interrupt-save-fp-no-vfp-unit",
    # "-Warm-interrupt-vfp-clobber",
    # "-Warray-bounds",
    # "-Warray-compare",
    # "-Werror=array-compare-cxx26",
    # "-Warray-parameter",
    # "-Wasm-operand-widths",
    # "-Wassume",
    # "-Watimport-in-framework-header",
    # "-Werror=atomic-access",
    # "-Watomic-alignment",
    # "-Watomic-memory-ordering",
    # "-Watomic-property-with-user-defined-accessor",
    # "-Wattribute-alias",
    # "-Wattribute-packed-for-bitfield",
    # "-Werror=attribute-statement",
    # "-Wattribute-warning",
    # "-Wauto-disable-vptr-sanitizer",
    # "-Wauto-storage-class",
    # "-Wauto-var-id",
    # "-Wavailability",
    # "-Wavr-rtlib-linking-quirks",
    # "-Wbackend-plugin",
    # "-Wbackslash-newline-escape",
    # "-Wbitfield-constant-conversion",
    # "-Wbitfield-width",
    # "-Wbitwise-conditional-parentheses",
    # "-Wbitwise-instead-of-logical",
    # "-Wbitwise-op-parentheses",
    # "-Wblock-capture-autoreleasing",
    # "-Wbool-conversion",
    # "-Wbool-operation",
    # "-Wbounds-safety-counted-by-elt-type-unknown-size",
    # "-Wbraced-scalar-init",
    # "-Wbranch-protection",
    # "-Wbridge-cast",
    # "-Wbuiltin-assume-aligned-alignment",
    # "-Wbuiltin-macro-redefined",
    # "-Wbuiltin-memcpy-chk-size",
    # "-Wbuiltin-requires-header",
    # "-Wc++11-compat",
    # "-Wc++11-compat-deprecated-writable-strings",
    # "-Wc++11-extensions",
    # "-Wc++11-inline-namespace",
    # "-Werror=c++11-narrowing",
    # "-Werror=c++11-narrowing-const-reference",
    # "-Wc++14-extensions",
    # "-Wc++17-compat-mangling",
    # "-Wc++17-extensions",
    # "-Wc++20-compat",
    # "-Wc++20-extensions",
    # "-Wc++23-extensions",
    # "-Wc++23-lambda-attributes",
    # "-Wc++26-extensions",
    # "-Wc-attribute-extension",
    # "-Wc23-extensions",
    # "-Wc99-compat",
    # "-Wc99-designator",
    # "-Wc99-extensions",
    # "-Wcall-to-pure-virtual-from-ctor-dtor",
    # "-Wcalled-once-parameter",
    # "-Wcast-calling-convention",
    # "-Wcast-of-sel-type",
    # "-Wcast-qual-unrelated",
    # "-Wcfi-unchecked-callee",
    # "-Wchar-subscripts",
    # "-Wcharacter-conversion",
    # "-Wclang-cl-pch",
    # "-Wclass-conversion",
    # "-Wcmse-union-leak",
    # "-Wcomment",
    # "-Wcompare-distinct-pointer-types",
    # "-Wcompound-token-split-by-macro",
    # "-Wconditional-type-mismatch",
    # "-Wconfig-macros",
    # "-Wconstant-conversion",
    # "-Wconstant-evaluated",
    # "-Wconstant-logical-operand",
    # "-Wconstexpr-not-const",
    # "-Wcoro-non-aligned-allocation-function",
    # "-Wcoro-type-aware-allocation-function",
    # "-Wcoroutine",
    # "-Wcoroutine-missing-unhandled-exception",
    # "-Wcoroutines-unsupported-target",
    # "-Wctu",
    # "-Wcuda-compat",
    # "-Wcxx-attribute-extension",
    # "-Wdangling",
    # "-Wdangling-assignment",
    # "-Wdangling-assignment-gsl",
    # "-Wdangling-capture",
    # "-Wdangling-else",
    # "-Wdangling-field",
    # "-Wdangling-gsl",
    # "-Wdangling-initializer-list",
    # "-Wdarwin-sdk-settings",
    # "-Wdealloc-in-category",
    # "-Wdebug-compression-unavailable",
    # "-Wdeclaration-after-statement",
    # "-Wdefault-const-init-field-unsafe",
    # "-Wdefault-const-init-var-unsafe",
    # "-Wdefaulted-function-deleted",
    # "-Wdelayed-template-parsing-in-cxx20",
    # "-Werror=delegating-ctor-cycles",
    # "-Wdelete-abstract-non-virtual-dtor",
    # "-Wdelete-incomplete",
    # "-Wdelete-non-abstract-non-virtual-dtor",
    # "-Wdeprecate-lax-vec-conv-all",
    # "-Wdeprecated",
    # "-Wdeprecated-altivec-src-compat",
    # "-Wdeprecated-anon-enum-enum-conversion",
    # "-Wdeprecated-array-compare",
    # "-Wdeprecated-attributes",
    # "-Wdeprecated-builtins",
    # "-Wdeprecated-comma-subscript",
    # "-Wdeprecated-coroutine",
    # "-Wdeprecated-declarations",
    # "-Wdeprecated-declarations-switch-case",
    # "-Wdeprecated-enum-compare",
    # "-Wdeprecated-enum-compare-conditional",
    # "-Wdeprecated-enum-enum-conversion",
    # "-Wdeprecated-enum-float-conversion",
    # "-Wdeprecated-increment-bool",
    # "-Wdeprecated-literal-operator",
    # "-Wdeprecated-missing-comma-variadic-parameter",
    # "-Wdeprecated-module-dot-map",
    # "-Wdeprecated-non-prototype",
    # "-Wdeprecated-objc-isa-usage",
    # "-Wdeprecated-objc-pointer-introspection",
    # "-Wdeprecated-objc-pointer-introspection-performSelector",
    # "-Wdeprecated-octal-literals",
    # "-Wdeprecated-ofast",
    # "-Wdeprecated-pragma",
    # "-Wdeprecated-register",
    # "-Wdeprecated-this-capture",
    # "-Wdeprecated-type",
    # "-Wdeprecated-volatile",
    # "-Wdistributed-object-modifiers",
    # "-Wdivision-by-zero",
    # "-Wdll-attribute-on-redeclaration",
    # "-Wdllexport-explicit-instantiation",
    # "-Wdllexport-explicit-instantiation-decl",
    # "-Wdllimport-static-field-def",
    # "-Wdtor-name",
    # "-Werror=dtor-typedef",
    # "-Wduplicate-decl-specifier",
    # "-Wduplicate-protocol",
    # "-Wdxil-validation",
    # "-Wdynamic-class-memaccess",
    # "-Werror=dynamic-exception-spec",
    # "-Weager-load-cxx-named-modules",
    # "-Werror=elaborated-enum-base",
    # "-Werror=elaborated-enum-class",
    # "-Wempty-body",
    # "-Wempty-decomposition",
    # "-Wencode-type",
    # "-Wenum-compare",
    # "-Wenum-compare-switch",
    # "-Wenum-compare-typo",
    # "-Werror=enum-enum-conversion",
    # "-Wenum-too-large",
    # "-Wexceptions",
    # "-Wexcess-initializers",
    # "-Wexcessive-regsave",
    # "-Wexpansion-to-defined",
    # "-Wexperimental-header-units",
    # "-Wexperimental-option",
    # "-Wexplicit-initialize-call",
    # "-Wexplicit-specialization-storage-class",
    # "-Wext-cxx-type-aware-allocators",
    # "-Wextern-c-compat",
    # "-Wextern-initializer",
    # "-Wextra-qualification",
    # "-Wextra-tokens",
    # "-Wextractapi-misuse",
    # "-Werror=extraneous-template-head",
    # "-Wfinal-dtor-non-final-class",
    # "-Wfinal-macro",
    # "-Wfixed-point-overflow",
    # "-Wflag-enum",
    # "-Wfor-loop-analysis",
    # "-Wformat",
    # "-Wformat-extra-args",
    # "-Wformat-insufficient-args",
    # "-Wformat-invalid-specifier",
    # "-Wformat-overflow",
    # "-Wformat-overflow-non-kprintf",
    # "-Wformat-security",
    # "-Wformat-truncation",
    # "-Wformat-truncation-non-kprintf",
    # "-Wformat-zero-length",
    # "-Wfortify-source",
    # "-Wframe-address",
    # "-Wframework-include-private-from-public",
    # "-Wfree-nonheap-object",
    # "-Wfriend-enum",
    # "-Wfunction-def-in-objc-container",
    # "-Wfunction-multiversion",
    # "-Wgcc-compat",
    # "-Wgcc-install-dir-libstdcxx",
    # "-Wglobal-isel",
    # "-Wgnu-alignof-expression",
    # "-Werror=gnu-array-member-paren-init",
    # "-Wgnu-designator",
    # "-Wgnu-folding-constant",
    # "-Wgnu-inline-cpp-without-extern",
    # "-Wgnu-static-float-init",
    # "-Wgnu-string-literal-operator-template",
    # "-Wgnu-variable-sized-type-not-at-end",
    # "-Wgpu-maybe-wrong-side",
    # "-Werror=groupshared-initializer",
    # "-Wheader-guard",
    # "-Whexagon-reserved-caller-saved",
    # "-Whigher-precision-for-complex-division",
    # "-Werror=hip-omp-target-directives",
    # "-Whip-only",
    # "-Whlsl-202y-extensions",
    # "-Werror=hlsl-availability",
    # "-Whlsl-dxc-compatability",
    # "-Whlsl-explicit-binding",
    # "-Whlsl-extensions",
    # "-Wignored-attributes",
    # "-Wignored-availability-without-sdk-settings",
    # "-Wignored-gch",
    # "-Wignored-optimization-argument",
    # "-Wignored-pragma-intrinsic",
    # "-Wignored-pragmas",
    # "-Wignored-qualifiers",
    # "-Wignored-reference-qualifiers",
    # "-Wimplicit-const-int-float-conversion",
    # "-Wimplicit-conversion-floating-point-to-bool",
    # "-Wimplicit-enum-enum-cast",
    # "-Wimplicit-exception-spec-mismatch",
    # "-Wimplicit-fixed-point-conversion",
    # "-Werror=implicit-function-declaration",
    # "-Werror=implicit-int",
    # "-Wimplicitly-unsigned-literal",
    # "-Wimport-implementation-partition-unit-in-interface-unit",
    # "-Winaccessible-base",
    # "-Winclude-angled-in-module-purview",
    # "-Winclude-next-absolute-path",
    # "-Winclude-next-outside-header",
    # "-Wincompatible-exception-spec",
    # "-Werror=incompatible-function-pointer-types",
    # "-Wincompatible-library-redeclaration",
    # "-Wincompatible-ms-pragma-section",
    # "-Werror=incompatible-ms-struct",
    # "-Werror=incompatible-pointer-types",
    # "-Wincompatible-pointer-types-discards-overflow-behavior",
    # "-Wincompatible-pointer-types-discards-qualifiers",
    # "-Wincompatible-property-type",
    # "-Wincompatible-sysroot",
    # "-Wincomplete-framework-module-declaration",
    # "-Wincomplete-implementation",
    # "-Wincomplete-setjmp-declaration",
    # "-Wincomplete-umbrella",
    # "-Winconsistent-dllimport",
    # "-Winconsistent-missing-override",
    # "-Werror=increment-bool",
    # "-Winfinite-recursion",
    # "-Werror=init-priority-reserved",
    # "-Werror=initializer-overrides",
    # "-Winjected-class-name",
    # "-Winline-asm",
    # "-Winline-namespace-reopened-noninline",
    # "-Winline-new-delete",
    # "-Winstallapi-violation",
    # "-Winstantiation-after-specialization",
    # "-Werror=int-conversion",
    # "-Wint-in-bool-context",
    # "-Wint-to-pointer-cast",
    # "-Wint-to-void-pointer-cast",
    # "-Winteger-overflow",
    # "-Winvalid-command-line-argument",
    # "-Werror=invalid-constexpr",
    # "-Winvalid-feature-combination",
    # "-Werror=invalid-gnu-asm-cast",
    # "-Winvalid-iboutlet",
    # "-Winvalid-initializer-from-system-header",
    # "-Werror=invalid-ios-deployment-target",
    # "-Winvalid-no-builtin-names",
    # "-Winvalid-noreturn",
    # "-Winvalid-offsetof",
    # "-Werror=invalid-partial-specialization",
    # "-Winvalid-pp-token",
    # "-Winvalid-source-encoding",
    # "-Werror=invalid-specialization",
    # "-Werror=invalid-static-assert-message",
    # "-Werror=invalid-token-paste",
    # "-Winvalid-unevaluated-string",
    # "-Winvalid-version-availability",
    # "-Wjump-seh-finally",
    # "-Wkeyword-compat",
    # "-Wknr-promoted-parameter",
    # "-Wlarge-by-value-copy",
    # "-Werror=legacy-constant-register-binding",
    # "-Wlinker-warnings",
    # "-Wliteral-conversion",
    # "-Wliteral-range",
    # "-Wlocal-type-template-args",
    # "-Wlogical-not-parentheses",
    # "-Wlogical-op-parentheses",
    # "-Wmacro-redefined",
    # "-Wmain",
    # "-Wmain-attached-to-named-module",
    # "-Wmain-return-type",
    # "-Wmalformed-warning-check",
    # "-Wmany-braces-around-scalar-init",
    # "-Wmath-errno-enabled-with-veclib",
    # "-Wmathematical-notation-identifier-extension",
    # "-Wmatrix-conversion",
    # "-Wmax-unsigned-zero",
    # "-Wmemset-transposed-args",
    # "-Wmemsize-comparison",
    # "-Wmicrosoft-abstract",
    # "-Wmicrosoft-anon-tag",
    # "-Wmicrosoft-cast",
    # "-Wmicrosoft-const-init",
    # "-Wmicrosoft-default-arg-redefinition",
    # "-Wmicrosoft-drectve-section",
    # "-Wmicrosoft-enum-forward-reference",
    # "-Wmicrosoft-exception-spec",
    # "-Wmicrosoft-exists",
    # "-Wmicrosoft-explicit-constructor-call",
    # "-Wmicrosoft-extra-qualification",
    # "-Wmicrosoft-goto",
    # "-Wmicrosoft-inaccessible-base",
    # "-Wmicrosoft-include",
    # "-Wmicrosoft-init-from-predefined",
    # "-Wmicrosoft-inline-on-non-function",
    # "-Wmicrosoft-mutable-reference",
    # "-Wmicrosoft-pure-definition",
    # "-Wmicrosoft-sealed",
    # "-Wmicrosoft-string-literal-from-predefined",
    # "-Wmicrosoft-template",
    # "-Wmicrosoft-template-shadow",
    # "-Wmicrosoft-union-member-reference",
    # "-Wmicrosoft-unqualified-friend",
    # "-Wmicrosoft-using-decl",
    # "-Wmicrosoft-void-pseudo-dtor",
    # "-Wmisexpect",
    # "-Wmisleading-indentation",
    # "-Wmismatched-new-delete",
    # "-Wmismatched-parameter-types",
    # "-Wmismatched-return-types",
    # "-Wmismatched-tags",
    # "-Wmissing-braces",
    # "-Wmissing-constinit",
    # "-Wmissing-declarations",
    # "-Wmissing-exception-spec",
    # "-Wmissing-multilib",
    # "-Wmissing-noescape",
    # "-Wmissing-prototype-for-cc",
    # "-Wmissing-selector-name",
    # "-Wmissing-sysroot",
    # "-Werror=missing-template-arg-list-after-template-kw",
    # "-Wmisspelled-assumption",
    # "-Wmix-packoffset",
    # "-Wmodule-conflict",
    # "-Werror=module-file-config-mismatch",
    # "-Wmodule-file-extension",
    # "-Wmodule-import-in-extern-c",
    # "-Wmodules-ambiguous-internal-linkage",
    # "-Werror=modules-import-nested-redundant",
    # "-Wmsvc-not-found",
    # "-Wmulti-gpu",
    # "-Wmultichar",
    # "-Wmultilib-not-found",
    # "-Wmultiple-move-vbase",
    # "-Wnan-infinity-disabled",
    # "-Wnew-returns-null",
    # "-Wnoderef",
    # "-Wnon-c-typedef-for-linkage",
    # "-Wnon-literal-null-conversion",
    # "-Werror=non-pod-varargs",
    # "-Wnon-power-of-two-alignment",
    # "-Wnonnull",
    # "-Wnonportable-include-path",
    # "-Wnonportable-private-apinotes-path",
    # "-Werror=nonportable-sycl",
    # "-Wnonportable-vector-initialization",
    # "-Wnontrivial-memaccess",
    # "-Wnontrivial-memcall",
    # "-Wnsconsumed-mismatch",
    # "-Wnsreturns-mismatch",
    # "-Wnull-arithmetic",
    # "-Wnull-character",
    # "-Wnull-conversion",
    # "-Wnull-dereference",
    # "-Wnullability",
    # "-Wnullability-completeness",
    # "-Wnullability-completeness-on-arrays",
    # "-Werror=nullability-declspec",
    # "-Wnullability-inferred-on-nested-type",
    # "-Wobjc-autosynthesis-property-ivar-name-match",
    # "-Wobjc-bool-constant-conversion",
    # "-Wobjc-boxing",
    # "-Wobjc-circular-container",
    # "-Wobjc-designated-initializers",
    # "-Wobjc-dictionary-duplicate-keys",
    # "-Wobjc-duplicate-category-definition",
    # "-Wobjc-flexible-array",
    # "-Wobjc-forward-class-redefinition",
    # "-Wobjc-literal-compare",
    # "-Wobjc-literal-conversion",
    # "-Wobjc-macro-redefinition",
    # "-Wobjc-method-access",
    # "-Wobjc-missing-super-calls",
    # "-Wobjc-multiple-method-names",
    # "-Wobjc-noncopy-retain-block-property",
    # "-Wobjc-nonunified-exceptions",
    # "-Wobjc-property-implementation",
    # "-Wobjc-property-implicit-mismatch",
    # "-Wobjc-property-matches-cocoa-ownership-rule",
    # "-Wobjc-property-no-attribute",
    # "-Wobjc-property-synthesis",
    # "-Wobjc-protocol-method-implementation",
    # "-Wobjc-protocol-property-synthesis",
    # "-Wobjc-protocol-qualifiers",
    # "-Wobjc-readonly-with-setter-property",
    # "-Wobjc-redundant-literal-use",
    # "-Wobjc-root-class",
    # "-Wobjc-signed-char-bool-implicit-float-conversion",
    # "-Wobjc-string-compare",
    # "-Wobjc-string-concatenation",
    # "-Wobjc-unsafe-perform-selector",
    # "-Werror=odr",
    # "-Wopenacc-cache-var-inside-loop",
    # "-Wopenacc-confusing-routine-name",
    # "-Wopenacc-deprecated-clause-alias",
    # "-Wopenacc-self-if-potential-conflict",
    # "-Werror=openacc-var-lacks-operation",
    # "-Wopenacc-var-non-const-array",
    # "-Wopencl-unsupported-rgba",
    # "-Wopenmp-51-extensions",
    # "-Wopenmp-clauses",
    # "-Wopenmp-extensions",
    # "-Wopenmp-future",
    # "-Wopenmp-loop-form",
    # "-Wopenmp-mapping",
    # "-Wopenmp-target",
    # "-Wopenmp-target-exception",
    # "-Woption-ignored",
    # "-Wordered-compare-function-pointers",
    # "-Werror=out-of-line-declaration",
    # "-Wout-of-scope-function",
    # "-Woverflow-behavior-attribute-ignored",
    # "-Woverloaded-shift-op-parentheses",
    # "-Woverloaded-virtual",
    # "-Woverride-module",
    # "-Woverriding-complex-range",
    # "-Woverriding-deployment-version",
    # "-Woverriding-option",
    # "-Wpacked-non-pod",
    # "-Werror=parentheses",
    # "-Wparentheses-equality",
    # "-Wpass-failed",
    # "-Wpch-date-time",
    # "-Wpch-vfs-diff",
    # "-Wpessimizing-move",
    # "-Wpgo-coverage",
    # "-Wpointer-arith",
    # "-Wpointer-bool-conversion",
    # "-Wpointer-compare",
    # "-Wpointer-integer-compare",
    # "-Wpointer-sign",
    # "-Wpointer-to-enum-cast",
    # "-Wpointer-to-int-cast",
    # "-Wpointer-type-mismatch",
    # "-Wpotentially-direct-selector",
    # "-Wpotentially-evaluated-expression",
    # "-Wpragma-clang-attribute",
    # "-Wpragma-once-outside-header",
    # "-Wpragma-pack",
    # "-Wpragma-system-header-outside-header",
    # "-Wpragmas",
    # "-Wpre-c++26-compat",
    # "-Wpredefined-identifier-outside-function",
    # "-Wpreferred-type-bitfield-enum-conversion",
    # "-Wprivate-extern",
    # "-Werror=private-header",
    # "-Wprivate-module",
    # "-Wprofile-instr-out-of-date",
    # "-Wprofile-instr-unprofiled",
    # "-Wproperty-access-dot-syntax",
    # "-Wproperty-attribute-mismatch",
    # "-Wprotocol",
    # "-Wprotocol-property-synthesis-ambiguity",
    # "-Wpsabi",
    # "-Wptrauth-null-pointers",
    # "-Wqualified-void-return-type",
    # "-Wrange-loop-construct",
    # "-Wread-only-types",
    # "-Wreadonly-iboutlet-property",
    # "-Wreceiver-expr",
    # "-Wreceiver-forward-class",
    # "-Wredeclared-class-member",
    # "-Wreduced-bmi-output-overrided",
    # "-Wredundant-attribute",
    # "-Wredundant-consteval-if",
    # "-Wredundant-move",
    # "-Wreference-tu-local-entity-in-other-tu",
    # "-Werror=register",
    # "-Wreinterpret-base-class",
    # "-Wreorder-ctor",
    # "-Wreorder-init-list",
    # "-Wrequires-super-attribute",
    # "-Wreserved-identifier",
    # "-Wreserved-module-identifier",
    # "-Werror=reserved-user-defined-literal",
    # "-Wrestrict-expansion",
    # "-Werror=return-mismatch",
    # "-Wreturn-stack-address",
    # "-Wreturn-type",
    # "-Wreturn-type-c-linkage",
    # "-Wrewrite-not-bool",
    # "-Wriscv-be-experimental",
    # "-Wrtti",
    # "-Wsarif-format-unstable",
    # "-Werror=scalable-static-analysis-framework",
    # "-Wsection",
    # "-Wself-assign",
    # "-Wself-assign-field",
    # "-Wself-assign-overloaded",
    # "-Wself-move",
    # "-Wsentinel",
    # "-Wserialized-diagnostics",
    # "-Wshadow-ivar",
    # "-Wshift-count-negative",
    # "-Wshift-count-overflow",
    # "-Wshift-negative-value",
    # "-Wshift-op-parentheses",
    # "-Wshift-overflow",
    # "-Werror=signed-unsigned-wchar",
    # "-Wsimdlen",
    # "-Wsingle-bit-bitfield-constant-conversion",
    # "-Wsizeof-array-argument",
    # "-Wsizeof-array-decay",
    # "-Wsizeof-array-div",
    # "-Wsizeof-pointer-div",
    # "-Wsizeof-pointer-memaccess",
    # "-Wslash-u-filename",
    # "-Wslh-asm-goto",
    # "-Wsometimes-uninitialized",
    # "-Wsource-mgr",
    # "-Wsource-uses-openacc",
    # "-Wsource-uses-openmp",
    # "-Wspir-compat",
    # "-Wspirv-validation",
    # "-Wstack-exhausted",
    # "-Wstack-protector",
    # "-Werror=static-float-init",
    # "-Wstatic-in-inline",
    # "-Wstatic-inline-explicit-instantiation",
    # "-Wstatic-local-in-inline",
    # "-Wstatic-self-init",
    # "-Wstdlibcxx-not-found",
    # "-Werror=strict-primary-template-shadow",
    # "-Wstring-compare",
    # "-Wstring-plus-char",
    # "-Wstring-plus-int",
    # "-Wstrlcpy-strlcat-size",
    # "-Wstrncat-size",
    # "-Wsuspicious-bzero",
    # "-Wswift-name-attribute",
    # "-Wswitch",
    # "-Wswitch-bool",
    # "-Wsync-alignment",
    # "-Wsync-fetch-and-nand-semantics-changed",
    # "-Wtarget-clones-mixed-specifiers",
    # "-Wtautological-bitwise-compare",
    # "-Wtautological-compare",
    # "-Wtautological-constant-compare",
    # "-Wtautological-constant-out-of-range-compare",
    # "-Wtautological-negation-compare",
    # "-Wtautological-objc-bool-compare",
    # "-Wtautological-overlap-compare",
    # "-Wtautological-pointer-compare",
    # "-Wtautological-undefined-compare",
    # "-Wtcb-enforcement",
    # "-Wtemplate-in-declaration-name",
    # "-Wtentative-definition-array",
    # "-Wtrigraphs",
    # "-Wtrivial-auto-var-init",
    # "-Wtype-safety",
    # "-Wtypedef-redefinition",
    # "-Wtypename-missing",
    # "-Wunable-to-open-stats-file",
    # "-Wunaligned-qualifier-implicit-cast",
    # "-Wunavailable-declarations",
    # "-Wundef-true",
    # "-Wundefined-arm-za",
    # "-Wundefined-arm-zt0",
    # "-Wundefined-bool-conversion",
    # "-Wundefined-inline",
    # "-Wundefined-internal",
    # "-Wundefined-var-template",
    # "-Wunderaligned-exception-object",
    # "-Werror=underlying-atomic-qualifier-ignored",
    # "-Wunderlying-cv-qualifier-ignored",
    # "-Wunevaluated-expression",
    # "-Wunguarded-availability-new",
    # "-Wunicode",
    # "-Wunicode-homoglyph",
    # "-Wunicode-whitespace",
    # "-Wunicode-zero-width",
    # "-Wuninitialized",
    # "-Wuninitialized-const-pointer",
    # "-Wuninitialized-const-reference",
    # "-Wuninitialized-explicit-init",
    # "-Wunknown-acc-extension-clause",
    # "-Wunknown-argument",
    # "-Wunknown-assumption",
    # "-Wunknown-attributes",
    # "-Wunknown-cuda-version",
    # "-Wunknown-directives",
    # "-Wunknown-escape-sequence",
    # "-Wunknown-pragmas",
    # "-Wunknown-sanitizers",
    # "-Wunknown-warning-option",
    # "-Wunnamed-type-template-args",
    # "-Wunneeded-internal-declaration",
    # "-Wunqualified-std-cast-call",
    # "-Wunreachable-code-generic-assoc",
    # "-Wunsequenced",
    # "-Wunsupported-abi",
    # "-Wunsupported-abs",
    # "-Wunsupported-availability-guard",
    # "-Wunsupported-cb",
    # "-Wunsupported-floating-point-opt",
    # "-Wunsupported-friend",
    # "-Wunsupported-gpopt",
    # "-Wunsupported-nan",
    # "-Wunsupported-target-opt",
    # "-Wunsupported-visibility",
    # "-Werror=unusable-partial-specialization",
    # "-Wunused-but-set-global",
    # "-Wunused-but-set-variable",
    # "-Wunused-command-line-argument",
    # "-Wunused-comparison",
    # "-Wunused-const-variable",
    # "-Wunused-function",
    # "-Wunused-getter-return-value",
    # "-Wunused-label",
    # "-Wunused-lambda-capture",
    # "-Wunused-local-typedef",
    # "-Wunused-private-field",
    # "-Wunused-property-ivar",
    # "-Wunused-result",
    # "-Wunused-value",
    # "-Wunused-variable",
    # "-Wunused-volatile-lvalue",
    # "-Wuser-defined-literals",
    # "-Wuser-defined-warnings",
    # "-Wvarargs",
    # "-Wvariadic-macros",
    # "-Werror=vec-elem-size",
    # "-Wvector-conversion",
    # "-Wvexing-parse",
    # "-Wvisibility",
    # "-Wvla-cxx-extension",
    # "-Wvla-extension-static-assert",
    # "-Wvoid-pointer-to-enum-cast",
    # "-Wvoid-pointer-to-int-cast",
    # "-Wvoid-ptr-dereference",
    # "-Wwasm-exception-spec",
    # "-Wwritable-strings",
    # "-Wxor-used-as-pow",
)


# Compiler diagnostics and analyzer reports are separate SARIF documents. Keep
# both: compiler errors can prevent an analyzer report from being produced.
SARIF_FLAGS = [
    "-fdiagnostics-format=sarif",
    "-Wno-sarif-format-unstable",
    "-fno-caret-diagnostics",
]


class Findings:
    """Publish each diagnostic once, retaining every occurrence in the run log."""

    def __init__(self, history, color):
        self.history = history
        self.color = color
        self.seen = set()
        self.counts = Counter()
        self.duplicates = 0
        self.checkers = set()
        self.raw_seen = set()
        self.analyzer_locations = {}
        self.last_file = None

    def raw(self, text):
        """Keep unexpected tool output visible without repeating identical dumps."""
        if text and text not in self.raw_seen:
            self.raw_seen.add(text)
            print(text, file=sys.stderr, flush=True)

    def publish(self, finding, target, source, phase):
        # Target and entry TU are provenance, not identity: shared headers and
        # CTU imports should not print the same issue again under another target.
        key = tuple(
            finding.get(field, "")
            for field in ("file", "line", "column", "level", "checker", "message")
        )
        duplicate = key in self.seen
        self.history.write(
            json.dumps(
                dict(
                    finding,
                    target=target,
                    source=str(source),
                    phase=phase,
                    duplicate=duplicate,
                )
            )
            + "\n"
        )
        self.history.flush()
        if duplicate:
            self.duplicates += 1
            return False
        self.seen.add(key)
        self.counts[finding["level"]] += 1
        self.checkers.add(finding.get("checker", ""))

        # Keep notes attached to their diagnostic, using a heavier divider when
        # the entry TU changes. Suppressed duplicates leave no empty sections.
        current_file = (target, str(source))
        if self.last_file is not None:
            if current_file != self.last_file:
                print("━" * 80, file=sys.stderr, flush=True)
            elif finding["level"] != "note":
                print("─" * 80, file=sys.stderr, flush=True)
        self.last_file = current_file

        # Match Clang's location/severity layout without coloring redirected logs
        # unless explicitly requested. Flush so each completed TU is visible now.
        level = finding["level"]
        path = finding.get("file", "")
        line = finding.get("line", 0)
        column = finding.get("column", 0)
        location = f"{path}:{line}:{column}: " if path else ""
        option = finding.get("checker") or finding.get("option")
        message = finding["message"] + (f" [{option}]" if option else "")
        if self.color:
            shade = {"error": "31", "warning": "35", "note": "36"}.get(level, "36")
            text = f"\033[1m{location}\033[0m\033[1;{shade}m{level}: \033[0m\033[1m{message}\033[0m"
        else:
            text = f"{location}{level}: {message}"
        print(text, file=sys.stderr, flush=True)
        if path and line and column:
            excerpt = linecache.getline(path, line).rstrip("\r\n")
            if excerpt:
                prefix = f"{line:5} | "
                indent = len(excerpt[: column - 1].expandtabs(8))
                caret = " " * indent + "^"
                if self.color:
                    caret = f"\033[1;32m{caret}\033[0m"
                print(prefix + excerpt.expandtabs(8), file=sys.stderr)
                print(" " * len(prefix) + caret, file=sys.stderr, flush=True)
        return True

    def sarif(self, document, directory, target, source, phase):
        """Resolve report-local artifact references before comparing diagnostics."""
        if (
            not isinstance(document, dict)
            or document.get("version") != "2.1.0"
            or not isinstance(document.get("runs"), list)
        ):
            raise ValueError("expected a SARIF 2.1.0 report with runs")
        for run in document["runs"]:
            if not isinstance(run, dict):
                raise ValueError("SARIF runs must contain objects")
            results = run.get("results", [])
            if not isinstance(results, list):
                raise ValueError("SARIF results must be a list")
            for result in results:
                if not isinstance(result, dict) or not isinstance(
                    result.get("message"), dict
                ):
                    raise ValueError("SARIF results must contain diagnostic messages")
                message = result["message"]["text"]
                if not isinstance(message, str):
                    raise ValueError("SARIF diagnostic messages must be text")
                rule = result.get("ruleId", "")
                checker = rule if "." in rule else ""
                # The analyzer also echoes its warnings into compiler stderr
                # using a numeric rule ID and a checker suffix in the message.
                suffix = re.search(r" \[([a-zA-Z][\w.]*\.[\w.]+)\]$", message)
                if suffix:
                    checker = suffix[1]
                    message = message[: suffix.start()]
                level = result.get("level", "warning")
                if level == "none":
                    continue
                finding = dict(
                    self.location(run, (result.get("locations") or [{}])[0], directory),
                    level=level,
                    message=message,
                    checker=checker,
                    option=result.get("properties", {}).get("warningOption", ""),
                )
                # Clang's stderr echo can point at an expression range instead
                # of the analyzer's bug column. Use the report location when
                # the checker, message, file and line identify a known echo.
                echo_key = (finding["file"], finding["line"], checker, message)
                if phase == "analysis":
                    self.analyzer_locations.setdefault(echo_key, finding)
                elif (
                    phase == "compiler"
                    and suffix
                    and echo_key in self.analyzer_locations
                ):
                    finding = self.analyzer_locations[echo_key]
                if not self.publish(finding, target, source, phase):
                    continue

                # Keep execution-path explanations with the first visible issue.
                # They still pass through the same history and deduplication set.
                notes = list(result.get("relatedLocations", []))
                for flow in result.get("codeFlows", []):
                    for thread in flow.get("threadFlows", []):
                        notes.extend(
                            item["location"] for item in thread.get("locations", [])
                        )
                for note in notes:
                    text = note.get("message", {}).get("text")
                    if not text:
                        continue
                    location = self.location(run, note, directory)
                    if text == message and all(
                        location[k] == finding[k] for k in location
                    ):
                        continue
                    self.publish(
                        dict(location, level="note", message=text),
                        target,
                        source,
                        phase,
                    )

    @staticmethod
    def location(run, location, directory):
        physical = location.get("physicalLocation", {})
        artifact = physical.get("artifactLocation", {})
        if "index" in artifact:
            artifact = dict(run["artifacts"][artifact["index"]]["location"], **artifact)
        uri = artifact.get("uri", "")
        base = (
            run.get("originalUriBaseIds", {})
            .get(artifact.get("uriBaseId"), {})
            .get("uri", "")
        )
        uri = urljoin(base, uri)
        path = ""
        if uri:
            parsed = urlsplit(uri)
            if parsed.scheme not in ("", "file"):
                raise ValueError(f"unsupported diagnostic URI: {uri}")
            path = str((directory / unquote(parsed.path)).resolve())
        region = physical.get("region", {})
        return dict(
            file=path,
            line=region.get("startLine", 0),
            column=region.get("startColumn", 0),
        )


def indexing_sarif(log, stderr, directory):
    """Convert the indexer's diagnostic log, avoiding its broken SARIF printer."""
    diagnostics = []
    contents = log.read_bytes() if log is not None and log.exists() else b""
    if contents.strip():
        # Clang's diagnostic log is an XML plist fragment, not a plist document.
        diagnostics = plistlib.loads(b"<plist>" + contents + b"</plist>")["diagnostics"]
    results = []
    for diagnostic in diagnostics:
        level = diagnostic["level"]
        if level == "fatal error":
            level = "error"
        option = diagnostic.get("WarningOption", "")
        result = dict(level=level, message={"text": diagnostic["message"]})
        if diagnostic.get("filename"):
            region = {
                key: diagnostic[field]
                for key, field in (("startLine", "line"), ("startColumn", "column"))
                if diagnostic.get(field)
            }
            result["locations"] = [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": (directory / diagnostic["filename"])
                            .resolve()
                            .as_uri()
                        },
                        "region": region,
                    }
                }
            ]
        if option:
            result["properties"] = {"warningOption": f"-W{option}"}
        results.append(result)
    # Driver errors occur before the frontend diagnostic log is installed. Never
    # discard those just because the tool returned success or wrote an empty log.
    for line in stderr.splitlines():
        match = re.fullmatch(
            r"(?:[^:\n]+: )?(fatal error|error|warning|note|remark): (.*)", line
        )
        if match:
            results.append(
                {
                    "level": "error" if match[1] == "fatal error" else match[1],
                    "message": {"text": match[2]},
                }
            )
    return {
        "version": "2.1.0",
        "runs": [
            {"tool": {"driver": {"name": "clang-extdef-mapping"}}, "results": results}
        ],
    }


def compiler_sarif(stderr, directory):
    """Collect complete SARIF documents and handle early, plain driver errors."""
    document = {"version": "2.1.0", "runs": []}
    decoder = json.JSONDecoder()
    remaining = stderr.strip()
    while remaining:
        if remaining.startswith("{"):
            part, end = decoder.raw_decode(remaining)
            if (
                not isinstance(part, dict)
                or part.get("version") != "2.1.0"
                or not isinstance(part.get("runs"), list)
            ):
                raise ValueError("invalid compiler SARIF document")
            document["runs"].extend(part["runs"])
            remaining = remaining[end:].strip()
        else:
            line, _, remaining = remaining.partition("\n")
            part = indexing_sarif(None, line, directory)
            if not part["runs"][0]["results"]:
                raise ValueError(
                    "compiler produced non-SARIF output; see .compiler.stderr"
                )
            document["runs"].extend(part["runs"])
            remaining = remaining.strip()
    return document


def record_failure(failures, source, phase, error):
    """Keep tool failures separate from unique diagnostic counts."""
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


def run_commands(commands, parallel, verbose=False, phase=""):
    """Yield completed tool processes while keeping all report handling serial."""
    commands = iter(commands)
    pending = {}
    exhausted = False
    try:
        while pending or not exhausted:
            while not exhausted and len(pending) < max(1, parallel):
                try:
                    index, entry, command = next(commands)
                except StopIteration:
                    exhausted = True
                    break
                if verbose:
                    print(f"{phase} {entry[0]}", flush=True)

                # File-backed output avoids pipe deadlocks and bounds memory
                # while several Clang processes produce large reports at once.
                stdout = tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8", errors="replace"
                )
                stderr = tempfile.TemporaryFile(
                    mode="w+", encoding="utf-8", errors="replace"
                )
                try:
                    process = subprocess.Popen(
                        command, cwd=entry[1], stdout=stdout, stderr=stderr
                    )
                except OSError as error:
                    stdout.close()
                    stderr.close()
                    yield index, entry, error
                    continue
                pending[process] = (index, entry, command, stdout, stderr)

            completed = False
            for process in list(pending):
                if process.poll() is None:
                    continue
                completed = True
                index, entry, command, stdout, stderr = pending.pop(process)
                try:
                    stdout.seek(0)
                    stderr.seek(0)
                    result = subprocess.CompletedProcess(
                        command, process.returncode, stdout.read(), stderr.read()
                    )
                finally:
                    stdout.close()
                    stderr.close()
                yield index, entry, result
            if pending and not completed:
                time.sleep(0.01)
    finally:
        # Interrupts and report-processing exceptions must not leave compilers
        # running after the parent exits. Kill all first, then reap each child.
        for process in pending:
            if process.poll() is None:
                process.kill()
        for process, (_, _, _, stdout, stderr) in pending.items():
            process.wait()
            stdout.close()
            stderr.close()


def compiler_flags(flags):
    """Retain semantic build flags, replacing compiler warning policy explicitly."""
    result = []
    arguments = iter(flags)
    for flag in arguments:
        forwarded = flag in ("-Xclang", "-Xanalyzer", "-Xpreprocessor")
        value = next(arguments) if forwarded else flag
        # -Wa/-Wl/-Wp forward assembler, linker and preprocessor options; they
        # affect compilation semantics and are not compiler warning controls.
        if (
            value == "-w"
            or value in ("-pedantic", "-pedantic-errors")
            or (
                value.startswith("-W")
                and not value.startswith(("-Wa,", "-Wl,", "-Wp,"))
            )
        ):
            continue
        if forwarded:
            result.append(flag)
        result.append(value)
    # Unlike -w, this permits subsequent -W switches to enable selected warnings.
    return [*result, "-Wno-everything", *COMPILER_WARNINGS]


def analyze(
    entries, output, clang, mapping, findings, verbose=False, shallow=False, parallel=0
):
    """Index external definitions before analyzing any source in this target."""
    output.mkdir(parents=True)
    # Use the same policy for indexing, analysis and imported CTU invocations.
    entries = [
        (source, directory, compiler_flags(flags))
        for source, directory, flags in entries
    ]
    definitions = defaultdict(set)
    invocations = {}
    indexed = []
    failures = []
    commands = (
        (
            index,
            (source, directory, flags),
            [
                mapping,
                str(source),
                "--",
                *flags,
                "-fdiagnostics-format=clang",
                "-fno-color-diagnostics",
                "-Xclang",
                "-diagnostic-log-file",
                "-Xclang",
                str(output / f"{index}-{source.name}.index.xml"),
            ],
        )
        for index, (source, directory, flags) in enumerate(entries)
    )
    for index, (source, directory, flags), result in run_commands(
        commands, parallel, verbose, "Indexing"
    ):
        log = output / f"{index}-{source.name}.index.xml"
        try:
            if isinstance(result, OSError):
                raise result
            (output / f"{index}-{source.name}.index.stderr").write_text(
                result.stderr, encoding="utf-8"
            )
            document = indexing_sarif(log, result.stderr, directory)
            (output / f"{index}-{source.name}.index.sarif").write_text(
                json.dumps(document, indent=2), encoding="utf-8"
            )
            findings.sarif(document, directory, output.name, source, "indexing")
            if result.returncode and not document["runs"][0]["results"]:
                findings.raw(result.stderr)
            result.check_returncode()
            if any(item["level"] == "error" for item in document["runs"][0]["results"]):
                raise ValueError("indexer reported errors despite a successful exit")
            source_definitions = []
            for line in result.stdout.splitlines():
                # USRs can contain spaces; their byte length precedes the first ':'.
                length, rest = line.encode().split(b":", 1)
                boundary = int(length)
                source_definitions.append((rest[:boundary], rest[boundary + 1 :]))
        except (
            OSError,
            subprocess.CalledProcessError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            ExpatError,
        ) as error:
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
        indexed.append(source)

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

    analyzed = []
    # Exhaust indexing and publish the complete target map before starting any
    # analysis process, so CTU imports never see an incomplete symbol index.
    commands = (
        (
            index,
            (source, directory, flags),
            [
                clang,
                "--analyze",
                *flags,
                *SARIF_FLAGS,
                "--analyzer-no-default-checks",
                "-Xclang",
                f"-analyzer-checker={','.join(CHECKERS)}",
                "-Xclang",
                "-analyzer-output=sarif",
                "-Xclang",
                "-analyzer-config",
                "-Xclang",
                # Only exploration depth changes; both modes retain CTU.
                f"mode={'shallow' if shallow else 'deep'},"
                "experimental-enable-naive-ctu-analysis=true,"
                f"ctu-dir={output},ctu-invocation-list={invocation_list}",
                str(source),
                "-o",
                str(output / f"{index}-{source.name}.sarif"),
            ],
        )
        for index, (source, directory, flags) in enumerate(entries)
    )
    for index, (source, directory, flags), result in run_commands(
        commands, parallel, verbose, "Analyzing"
    ):
        report = output / f"{index}-{source.name}.sarif"
        if isinstance(result, OSError):
            record_failure(failures, source, "analysis", result)
            continue

        # Read the richer analyzer report before its stderr echo so execution
        # paths stay attached to the first printed occurrence of each finding.
        usable = False
        if report.exists() or result.returncode == 0:
            try:
                findings.sarif(
                    json.loads(report.read_text(encoding="utf-8")),
                    directory,
                    output.name,
                    source,
                    "analysis",
                )
                usable = True
            except (OSError, ValueError, KeyError, TypeError, IndexError) as error:
                record_failure(failures, source, "report", error)

        # Even failed invocations can have useful compiler diagnostics. Preserve
        # raw stderr separately so every .sarif file is a complete JSON document.
        (output / f"{index}-{source.name}.compiler.stderr").write_text(
            result.stderr, encoding="utf-8"
        )
        try:
            document = compiler_sarif(result.stderr, directory)
            (output / f"{index}-{source.name}.compiler.sarif").write_text(
                json.dumps(document, indent=2), encoding="utf-8"
            )
            findings.sarif(document, directory, output.name, source, "compiler")
        except (ValueError, KeyError, TypeError, IndexError) as error:
            findings.raw(result.stderr)
            record_failure(failures, source, "compiler diagnostics", error)
            usable = False
        if result.returncode:
            record_failure(
                failures,
                source,
                "analysis",
                subprocess.CalledProcessError(result.returncode, result.args),
            )
            continue
        if usable:
            analyzed.append(source)
    return indexed, analyzed, failures


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
    parser.add_argument(
        "--parallel",
        type=int,
        default=min(12, max(1, (os.process_cpu_count() or 1) // 2)),
        help="maximum concurrent Clang processes (default: half the available CPUs, capped at 12; 0 disables parallelism)",
    )
    parser.add_argument(
        "--shallow",
        action="store_true",
        help="use shallow analysis with CTU (default: deep with CTU)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show per-file indexing and analysis progress",
    )
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="color diagnostic output (default: auto)",
    )
    args = parser.parse_args()
    if args.parallel < 0:
        parser.error("--parallel must be zero or greater")

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

    history = (output / "findings.jsonl").open("w", encoding="utf-8")
    color = args.color == "always" or (
        args.color == "auto" and sys.stderr.isatty() and "NO_COLOR" not in os.environ
    )
    findings = Findings(history, color)
    indexed = analyzed = 0
    indexed_files = set()
    analyzed_files = set()
    failures = []
    print(f"Reports: {output}", flush=True)
    for target, entries in sorted(targets.items()):
        print(f"Target {target}: {len(entries)} translation units", flush=True)
        target_indexed, target_analyzed, target_failures = analyze(
            entries,
            output / target,
            clang,
            mapping,
            findings,
            args.verbose,
            args.shallow,
            args.parallel,
        )
        indexed += len(target_indexed)
        indexed_files.update(target_indexed)
        analyzed += len(target_analyzed)
        analyzed_files.update(target_analyzed)
        failures.extend((target, *failure) for failure in target_failures)
    history.close()
    if args.example and "core.DivideZero" not in findings.checkers:
        failures.append(
            ("example", source_root / "caller.c", "example", "expected core.DivideZero")
        )

    # Keep the final coverage and failure status visible even after later files
    # succeed. Findings from successful reports do not themselves fail the run.
    total = sum(len(entries) for entries in targets.values())
    total_files = len(
        {source for entries in targets.values() for source, _, _ in entries}
    )
    print("\nStatic analysis summary:")
    print(f"  Indexed: {indexed}/{total} translation units")
    print(f"  Analyzed: {analyzed}/{total} translation units with usable reports")
    print(f"  Files indexed: {len(indexed_files)}/{total_files}")
    print(f"  Files analyzed: {len(analyzed_files)}/{total_files}")
    print(f"  Unique diagnostics: {sum(findings.counts.values())}")
    for level in ("warning", "error", "note", "remark"):
        print(f"  {level.capitalize()}s: {findings.counts[level]}")
    print(f"  Duplicate diagnostics skipped: {findings.duplicates}")
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
    except KeyboardInterrupt:
        # Allow scheduler cleanup to finish before reporting a normal Ctrl-C exit.
        print("\nStatic analysis interrupted.", file=sys.stderr)
        sys.exit(130)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"Static analysis failed: {error}", file=sys.stderr)
        sys.exit(1)
