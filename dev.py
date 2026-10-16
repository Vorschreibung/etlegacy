#!/usr/bin/env python3
# encoding: utf-8

# ET: Legacy developer entrypoint
#
# Requires Python 3.6 (December 23, 2016) or newer (no third party
# packages needed).
#
# Single cross-platform replacement for the former easybuild.{sh,bat} and
# release.{sh,bat} scripts. Every setting can be given on the command line
# or persisted in a TOML config file (default: ./dev-cfg.toml, see
# ./dev-cfg-example.toml).
#
# Precedence of settings (lowest to highest):
#   built-in defaults < config file < environment variables < command line

import argparse
import copy
import datetime
import fnmatch
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(ROOT, "src")
LIBS_DIR = os.path.join(ROOT, "libs")
DEFAULT_CONFIG = os.path.join(ROOT, "dev-cfg.toml")
MIRROR = "https://mirror.etlegacy.com/etmain/"
RPI_TOOLCHAIN = "cmake/Toolchain-cross-aarch64-linux.cmake"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# Inside GitHub actions the workspace is controlled by the workflow, so we
# must not touch it with git (submodules, cleaning, ...)
ALLOW_GIT = os.environ.get("CI") != "true"

# Well known mods and their URLs, used when only a mod name is given
MOD_URLS = {
    "legacy": "www.etlegacy.com",
    "etpub": "https://etpub.org",
    "jaymod": "https://jaymod.clanfu.org",
    "nq": "http://www.shitstorm.org",
}

# All settings with their defaults. None means "pick a platform default".
DEFAULTS = {
    # Paths, relative ones are resolved against the repository root
    "build_dir": "build",
    "project_dir": "project",
    "install_prefix": "~/etlegacy",
    "game_homepath": None,
    "game_basepath": None,
    # Toolchain
    "build_type": "Release",
    "arch": None,
    "compiler": None,
    "generator": None,
    "toolset": None,
    "toolchain_file": None,
    "jobs": None,
    "build_tool_args": [],
    # macOS
    "macos_deployment_target": "11.0",
    "macos_architectures": None,
    "macos_sysroot": None,
    # Build profile
    "target": "all",
    "rpi": False,
    "mod_name": "legacy",
    "mod_url": None,
    "portable_install": True,
    "lsp": False,
    # Feature toggles, None keeps the platform default
    "ssl": None,
    "auth": None,
    "db": None,
    "renderer2": None,
    "renderer_dynamic": None,
    "bundled_libs": None,
    "bundled_jpeg": None,
    "extra": None,
    "autoupdate": None,
    "mod_pk3": None,
    "zip_only": None,
    # Raw CMake overrides, applied after everything else
    "cmake": {},
    "cmake_args": [],
    # Output
    "silent": False,
    "color": "auto",
    # Version bump / tagging ('release' command)
    "release": {
        "branch": "master",
        "remote": None,
        "sign": False,
    },
}

# Settings holding filesystem paths
PATH_KEYS = (
    "build_dir",
    "project_dir",
    "install_prefix",
    "game_homepath",
    "game_basepath",
    "toolchain_file",
)

# Environment variables honored for backwards compatibility with easybuild.sh
ENV_SETTINGS = {
    "BUILD_DIR": "build_dir",
    "TOOLCHAIN_FILE": "toolchain_file",
    "MAKEFILE_GENERATOR": "generator",
    "MACOS_ARCHITECTURES": "macos_architectures",
}

ALL_BUNDLED = (
    "BUNDLED_LIBS",
    "BUNDLED_SDL",
    "BUNDLED_ZLIB",
    "BUNDLED_MINIZIP",
    "BUNDLED_CURL",
    "BUNDLED_JPEG",
    "BUNDLED_LUA",
    "BUNDLED_OGG_VORBIS",
    "BUNDLED_THEORA",
    "BUNDLED_OPENAL",
    "BUNDLED_GLEW",
    "BUNDLED_FREETYPE",
    "BUNDLED_PNG",
    "BUNDLED_SQLITE3",
    "BUNDLED_OPENSSL",
    "BUNDLED_WOLFSSL",
)

# Feature toggles: setting -> (CMake vars when enabled, when disabled)
TOGGLES = (
    (
        "ssl",
        {"FEATURE_SSL": 1},
        {
            "FEATURE_SSL": 0,
            "BUNDLED_OPENSSL": 0,
            "BUNDLED_WOLFSSL": 0,
            "FEATURE_AUTH": 0,
        },
    ),
    ("auth", {"FEATURE_AUTH": 1}, {"FEATURE_AUTH": 0}),
    ("db", {"FEATURE_DBMS": 1}, {"FEATURE_DBMS": 0, "BUNDLED_SQLITE3": 0}),
    ("renderer2", {"FEATURE_RENDERER2": 1}, {"FEATURE_RENDERER2": 0}),
    ("renderer_dynamic", {"RENDERER_DYNAMIC": 1}, {"RENDERER_DYNAMIC": 0}),
    ("bundled_libs", {"BUNDLED_LIBS": 1}, dict.fromkeys(ALL_BUNDLED, 0)),
    ("bundled_jpeg", {"BUNDLED_JPEG": 1}, {"BUNDLED_JPEG": 0}),
    (
        "extra",
        {"INSTALL_EXTRA": 1},
        {
            "INSTALL_EXTRA": 0,
            "INSTALL_OMNIBOT": 0,
            "INSTALL_GEOIP": 0,
            "INSTALL_WOLFADMIN": 0,
        },
    ),
    ("autoupdate", {"FEATURE_AUTOUPDATE": 1}, {"FEATURE_AUTOUPDATE": 0}),
    ("mod_pk3", {"BUILD_MOD_PK3": 1}, {"BUILD_MOD_PK3": 0}),
    ("zip_only", {"ZIP_ONLY": 1}, {"ZIP_ONLY": 0}),
)

# Raspberry PI / aarch64 preset
RPI_PRESET = {
    "FEATURE_RENDERER_GLES": 0,
    "RENDERER_DYNAMIC": 0,
    "FEATURE_RENDERER2": 0,
    "FEATURE_OGG_VORBIS": 1,
    "FEATURE_THEORA": 1,
    "BUNDLED_GLEW": 1,
    "FEATURE_FREETYPE": 1,
    "FEATURE_LUASQL": 1,
    "FEATURE_PNG": 1,
    "FEATURE_OMNIBOT": 1,
    "INSTALL_OMNIBOT": 1,
}

# Presets for building only a subset of the project
TARGET_PRESETS = {
    "all": {},
    "mod": {
        "BUILD_CLIENT": 0,
        "BUILD_SERVER": 0,
        "FEATURE_RENDERER2": 0,
        "FEATURE_RENDERER_GLES": 0,
        "RENDERER_DYNAMIC": 0,
        "FEATURE_CURL": 0,
        "FEATURE_OGG_VORBIS": 0,
        "FEATURE_THEORA": 0,
        "FEATURE_OPENAL": 0,
        "FEATURE_FREETYPE": 0,
        "FEATURE_PNG": 0,
        "BUNDLED_SDL": 0,
        "BUNDLED_ZLIB": 0,
        "BUNDLED_MINIZIP": 0,
        "BUNDLED_JPEG": 0,
        "BUNDLED_OGG_VORBIS": 0,
        "BUNDLED_THEORA": 0,
        "BUNDLED_GLEW": 0,
        "BUNDLED_FREETYPE": 0,
        "BUNDLED_PNG": 0,
        "BUNDLED_CURL": 0,
        "BUNDLED_OPENAL": 0,
    },
    "server": {
        "BUILD_CLIENT": 0,
        "BUILD_SERVER": 1,
        "FEATURE_RENDERER2": 0,
        "FEATURE_RENDERER_GLES": 0,
        "RENDERER_DYNAMIC": 0,
        "FEATURE_OGG_VORBIS": 0,
        "FEATURE_THEORA": 0,
        "FEATURE_OPENAL": 0,
        "FEATURE_FREETYPE": 0,
        "FEATURE_PNG": 0,
        "BUNDLED_SDL": 0,
        "BUNDLED_ZLIB": 1,
        "BUNDLED_MINIZIP": 1,
        "BUNDLED_JPEG": 0,
        "BUNDLED_OGG_VORBIS": 0,
        "BUNDLED_THEORA": 0,
        "BUNDLED_GLEW": 0,
        "BUNDLED_FREETYPE": 0,
        "BUNDLED_PNG": 0,
        "BUNDLED_OPENAL": 0,
    },
}

# Bundled library directories which may contain in-source build leftovers
# (relative to ./libs, with the make target used to clean them)
LIB_CLEAN_DIRS = (
    ("sdl2", "clean"),
    ("zlib", "distclean"),
    ("minizip", "clean"),
    ("jpegturbo", "clean"),
    ("curl/src", "clean"),
    ("lua/src", "clean"),
    ("ogg", "clean"),
    ("vorbis", "clean"),
    ("openal", "clean"),
    ("libpng", "clean"),
)

# Tokens of the minimal TOML reader: basic string, literal string, boolean,
# integer, array start, array end, separator
TOML_TOKEN_RE = re.compile(
    r"""\s*(?:("(?:[^"\\]|\\.)*")|'([^']*)'|(true|false)\b|([+-]?[0-9]+)\b|(\[)|(\])|(,))"""
)
TOML_KEY_RE = re.compile(r"\s*([A-Za-z0-9_-]+)\s*=")
TOML_TABLE_RE = re.compile(r"\s*\[\s*([A-Za-z0-9_-]+)\s*\]\s*(?:#.*)?$")

COPYRIGHT_RE = re.compile(
    rb"Copyright \(C\) 2012-[0-9]{4} ET:Legacy team <mail@etlegacy\.com>"
)
YEAR_RANGE_RE = re.compile(rb"2012-[0-9]{4}")


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------


# ANSI escape sequences of the colored output
BOLD_GREEN = "\033[1;32m"
BOLD_CYAN = "\033[1;36m"
BOLD_WHITE = "\033[1;37m"
BOLD_YELLOW = "\033[1;33m"
BOLD_RED = "\033[1;31m"
GREEN = "\033[0;32m"
RESET = "\033[0m"


def stream_color(stream, setting):
    # Decide whether ANSI colors are written to the stream. setting is True
    # (force), False (never) or "auto" (only if the terminal supports them).
    if setting is False:
        return False
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        tty = False

    # Windows consoles (conhost) only interpret escape sequences once virtual
    # terminal processing is enabled (Windows 10+), even when forced
    if IS_WINDOWS and tty:
        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            # Own DLL instance with proper signatures, so 64 bit handles are
            # not truncated and the global ctypes.windll stays untouched
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetConsoleMode.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            )
            kernel32.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            handle = msvcrt.get_osfhandle(stream.fileno())
            mode = wintypes.DWORD()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                # No console, e.g. a pipe of an old mintty
                tty = False
            elif not mode.value & 0x0004:
                # Enable ENABLE_VIRTUAL_TERMINAL_PROCESSING, fails before Windows 10
                tty = bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
        except (AttributeError, ImportError, OSError, ValueError):
            tty = False

    if setting is True:
        return True
    # The GitHub actions log viewer renders colors although it is no terminal
    if os.environ.get("GITHUB_ACTIONS") == "true":
        return True
    return tty and os.environ.get("TERM") != "dumb"


class Out:
    # Global output state, configured once the settings are known
    silent = False
    color = False
    color_err = False

    @staticmethod
    def setup(setting, cli=None):
        # Precedence: --color/--no-color > NO_COLOR/FORCE_COLOR env > config
        if cli is not None:
            setting = cli
        elif os.environ.get("NO_COLOR"):
            setting = False
        elif (
            os.environ.get("FORCE_COLOR", "0") != "0"
            or os.environ.get("CLICOLOR_FORCE", "0") != "0"
        ):
            setting = True
        Out.color = stream_color(sys.stdout, setting)
        Out.color_err = stream_color(sys.stderr, setting)

    @staticmethod
    def c(code):
        # Escape sequence for stdout output
        return code if Out.color else ""

    @staticmethod
    def e(code):
        # Escape sequence for stderr output
        return code if Out.color_err else ""


def info(msg):
    if not Out.silent:
        print(
            f"\n{Out.c(BOLD_GREEN)}~~>{Out.c(RESET)} {Out.c(BOLD_WHITE)}{msg}{Out.c(RESET)}",
            flush=True,
        )


def head(msg):
    if not Out.silent:
        print(
            f"{Out.c(BOLD_CYAN)} * {Out.c(BOLD_WHITE)}{msg}{Out.c(RESET)}",
            flush=True,
        )


def warn(msg):
    print(
        f"\n{Out.e(BOLD_RED)} WARNING{Out.e(BOLD_YELLOW)}~~> {Out.e(BOLD_WHITE)}{msg}{Out.e(RESET)}",
        file=sys.stderr,
        flush=True,
    )


def die(msg, code=1):
    print(f"{Out.e(BOLD_RED)}error:{Out.e(RESET)} {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd, cwd=None, env=None, check=True):
    # Echo and run a command, aborting the whole script on failure
    if not Out.silent:
        print(
            f"{Out.c(GREEN)}+ {subprocess.list2cmdline(cmd)}{Out.c(RESET)}",
            flush=True,
        )
    try:
        result = subprocess.run(cmd, cwd=cwd, env=env)
    except FileNotFoundError:
        if check:
            die(f"command not found: {cmd[0]}")
        return 127
    if check and result.returncode != 0:
        print("Exiting!", file=sys.stderr)
        sys.exit(result.returncode)
    return result.returncode


def capture(cmd, cwd=ROOT):
    # Run a command and return its stripped stdout, or None on failure
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def run_git(args, cwd=ROOT):
    if not ALLOW_GIT:
        print("Git usage is not allowed in this environment")
        return
    run(["git"] + args, cwd=cwd)


def rmtree(path):
    # Remove a directory tree, also handling read-only files on Windows
    def on_error(func, failed, _exc):
        os.chmod(failed, 0o700)
        func(failed)

    if os.path.isdir(path):
        shutil.rmtree(path, onerror=on_error)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def parse_toml_subset(text):
    # Minimal TOML reader, avoids depending on tomllib (Python 3.11+) or
    # tomli. Supports comments, single level [tables] and single line
    # key = value pairs with string, integer, boolean and (nested) array
    # values, which covers all dev.py settings.
    def parse_value(line, pos):
        match = TOML_TOKEN_RE.match(line, pos)
        if not match:
            raise ValueError("invalid value")
        basic, literal, boolean, integer, array = match.groups()[:5]
        if basic is not None:
            # TOML basic string escapes are compatible with JSON ones
            return json.loads(basic), match.end()
        if literal is not None:
            return literal, match.end()
        if boolean is not None:
            return boolean == "true", match.end()
        if integer is not None:
            return int(integer), match.end()
        if array is None:
            raise ValueError("invalid value")

        # Array: values separated by commas, a trailing comma is allowed
        items, pos = [], match.end()
        while True:
            match = TOML_TOKEN_RE.match(line, pos)
            if match and match.group(6):
                return items, match.end()
            item, pos = parse_value(line, pos)
            items.append(item)
            match = TOML_TOKEN_RE.match(line, pos)
            if not match or not (match.group(6) or match.group(7)):
                raise ValueError("expected ',' or ']'")
            if match.group(7):
                pos = match.end()

    data, table = {}, None
    for lineno, line in enumerate(text.splitlines(), 1):
        try:
            if not line.strip() or line.strip().startswith("#"):
                continue
            match = TOML_TABLE_RE.match(line)
            if match:
                if match.group(1) in data:
                    raise ValueError(f"duplicate table '{match.group(1)}'")
                table = data[match.group(1)] = {}
                continue
            match = TOML_KEY_RE.match(line)
            if not match:
                raise ValueError("expected 'key = value' or '[table]'")
            value, pos = parse_value(line, match.end())
            rest = line[pos:].strip()
            if rest and not rest.startswith("#"):
                raise ValueError("unexpected trailing content")
            target = data if table is None else table
            if match.group(1) in target:
                raise ValueError(f"duplicate key '{match.group(1)}'")
            target[match.group(1)] = value
        except ValueError as exc:
            raise ValueError(f"{exc} (at line {lineno})")
    return data


def load_config(path, explicit):
    # Load and validate the TOML config, omitted keys keep their defaults
    if not os.path.isfile(path):
        if explicit:
            die(f"config file not found: {path}")
        return {}

    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = parse_toml_subset(fh.read())
    except ValueError as exc:
        # Also covers invalid UTF-8 (UnicodeDecodeError)
        die(f"invalid TOML in {path}: {exc}")

    cfg = {}
    for key, value in data.items():
        if key not in DEFAULTS:
            die(f"{path}: unknown setting '{key}'")
        default = DEFAULTS[key]
        if isinstance(default, dict) and not isinstance(value, dict):
            die(f"{path}: '{key}' must be a table")
        if isinstance(default, list) and not isinstance(value, list):
            die(f"{path}: '{key}' must be a list")
        if key == "release":
            unknown = set(value) - set(DEFAULTS["release"])
            if unknown:
                die(f"{path}: unknown release setting(s): {', '.join(sorted(unknown))}")
        cfg[key] = value
    return cfg


def resolve_settings(args):
    # Merge defaults, config file, environment and command line
    cfg = copy.deepcopy(DEFAULTS)
    cli = vars(args)

    # Config file
    config_path = cli.pop("config_file", None)
    explicit = config_path is not None
    config_path = os.path.abspath(config_path) if explicit else DEFAULT_CONFIG
    for key, value in load_config(config_path, explicit).items():
        if key == "release":
            cfg["release"].update(value)
        else:
            cfg[key] = value

    # Environment variables (relative paths are relative to the cwd)
    for env_name, key in ENV_SETTINGS.items():
        if os.environ.get(env_name):
            value = os.environ[env_name]
            cfg[key] = os.path.abspath(value) if key in PATH_KEYS else value

    # Command line, raw "-D" defines and args are appended to the config ones
    for define in cli.pop("defines", []):
        name, sep, value = define.partition("=")
        if not sep or not name:
            die(f"invalid define '{define}', expected NAME=VALUE")
        cfg["cmake"][name] = value
    cfg["cmake_args"] = cfg["cmake_args"] + cli.pop("extra_cmake_args", [])
    for key in ("sign", "remote"):
        if key in cli:
            cfg["release"][key] = cli.pop(key)
    for key, value in cli.items():
        if key in DEFAULTS:
            cfg[key] = (
                os.path.abspath(os.path.expanduser(value))
                if key in PATH_KEYS
                else value
            )

    # The RPI toolchain shortcut only provides a default toolchain file
    if cli.get("rpi_toolchain"):
        cfg["rpi"] = True
        cfg["toolchain_file"] = cfg["toolchain_file"] or RPI_TOOLCHAIN

    # Platform specific defaults
    home = os.path.expanduser("~")
    if cfg["game_homepath"] is None:
        if IS_WINDOWS:
            cfg["game_homepath"] = os.path.join(home, "Documents", "ETLegacy")
        elif IS_MACOS:
            cfg["game_homepath"] = os.path.join(
                home, "Library", "Application Support", "etlegacy"
            )
        else:
            cfg["game_homepath"] = os.path.join(home, ".etlegacy")
    if cfg["game_basepath"] is None:
        cfg["game_basepath"] = os.path.join(home, "Documents", "ETLegacy-Build")
    if cfg["arch"] is None:
        # 32 bit keeps 2.60b compatibility, but is only possible on x86 hosts
        x86 = platform.machine().lower() in ("x86_64", "amd64", "i386", "i686", "x86")
        cfg["arch"] = "32" if x86 and not IS_MACOS and not cfg["rpi"] else "64"
    if cfg["rpi"]:
        cfg["arch"] = "64"
    if IS_MACOS and not cfg["macos_architectures"]:
        cfg["macos_architectures"] = (
            "arm64" if platform.machine() == "arm64" else "x86_64"
        )
    if cfg["mod_url"] is None:
        cfg["mod_url"] = MOD_URLS.get(cfg["mod_name"], MOD_URLS["legacy"])
    if cfg["jobs"] is None:
        cfg["jobs"] = os.cpu_count() or 1

    # Resolve all paths to absolute ones
    for key in PATH_KEYS:
        if cfg[key]:
            cfg[key] = os.path.join(ROOT, os.path.expanduser(cfg[key]))

    if cfg["arch"] not in ("32", "64"):
        die(f"arch must be '32' or '64', got '{cfg['arch']}'")
    if cfg["color"] not in (True, False, "auto"):
        die('color must be true, false or "auto"')
    if cfg["target"] not in TARGET_PRESETS:
        die(f"target must be one of: {', '.join(TARGET_PRESETS)}")
    return cfg


def cmake_vars(cfg):
    # Compute the CMake cache variables for the configure step
    v = {}

    # Platform defaults
    if IS_WINDOWS:
        v.update(
            {
                "BUNDLED_LIBS": 1,
                "FEATURE_AUTOUPDATE": 1,
                "INSTALL_EXTRA": 1,
                "RENDERER_DYNAMIC": 0,
                "FEATURE_RENDERER2": 0,
                "BUNDLED_WOLFSSL": 0,
                "BUNDLED_OPENSSL": 0,
                "FEATURE_SSL": 1,
                "FEATURE_AUTH": 1,
            }
        )
    else:
        v.update(
            {
                "ZIP_ONLY": 0,
                "BUILD_SERVER": 1,
                "BUILD_CLIENT": 1,
                "BUILD_MOD": 1,
                "BUILD_MOD_PK3": 1,
            }
        )
        v.update(dict.fromkeys(ALL_BUNDLED, 1))
        v.update(
            {
                "BUNDLED_WOLFSSL": 0,
                "FEATURE_CURL": 1,
                "FEATURE_SSL": 1,
                "FEATURE_AUTH": 1,
                "FEATURE_OGG_VORBIS": 1,
                "FEATURE_THEORA": 1,
                "FEATURE_OPENAL": 1,
                "FEATURE_FREETYPE": 1,
                "FEATURE_PNG": 1,
                "FEATURE_TRACKER": 1,
                "FEATURE_LUA": 1,
                "FEATURE_MULTIVIEW": 1,
                "FEATURE_EDV": 1,
                "FEATURE_ANTICHEAT": 1,
                "FEATURE_GETTEXT": 1,
                "FEATURE_DBMS": 1,
                "FEATURE_XPSAVE": 1,
                "FEATURE_RATING": 1,
                "FEATURE_AUTOUPDATE": 1,
                "FEATURE_RENDERER1": 1,
                "FEATURE_RENDERER2": 0,
                "FEATURE_RENDERER_GLES": 0,
                "RENDERER_DYNAMIC": 1,
                "FEATURE_LUASQL": 1,
                "FEATURE_OMNIBOT": 1,
                "INSTALL_EXTRA": 1,
                "INSTALL_OMNIBOT": 1,
                "INSTALL_GEOIP": 1,
                "INSTALL_WOLFADMIN": 1,
            }
        )
        if IS_MACOS:
            # The system curl breaks the auth requests sometimes, so the
            # bundled one is used when auth is enabled (resolved below)
            v.update({"BUNDLED_CURL": None, "BUNDLED_OPENSSL": 0, "BUNDLED_OPENAL": 0})

    # Environment variables named like a CMake variable override the defaults
    for name in list(v):
        if os.environ.get(name):
            v[name] = os.environ[name]

    # Explicit settings
    v["CMAKE_BUILD_TYPE"] = cfg["build_type"]
    v["CROSS_COMPILE32"] = 1 if cfg["arch"] == "32" else 0
    v["MODNAME"] = cfg["mod_name"]
    v["MODURL"] = cfg["mod_url"]

    # Presets and toggles
    if cfg["rpi"]:
        v.update(RPI_PRESET)
    v.update(TARGET_PRESETS[cfg["target"]])
    for key, enabled, disabled in TOGGLES:
        if cfg[key] is not None:
            v.update(enabled if cfg[key] else disabled)

    # Consistency rules
    if str(v.get("FEATURE_SSL")) == "0" and str(v.get("FEATURE_AUTH")) == "1":
        warn(
            "SSL is disabled, but authentication is enabled. Disabling authentication."
        )
        v["FEATURE_AUTH"] = 0
    if IS_MACOS:
        if v["BUNDLED_CURL"] is None:
            v["BUNDLED_CURL"] = v["FEATURE_AUTH"]
        # No Omni-bot support on macOS
        v["FEATURE_OMNIBOT"] = 0
        v["INSTALL_OMNIBOT"] = 0

    # Toolchain, install layout and tooling
    if cfg["toolchain_file"]:
        v["CMAKE_TOOLCHAIN_FILE"] = cfg["toolchain_file"]
    if not IS_WINDOWS and cfg["portable_install"]:
        layout = "./" if IS_MACOS else "."
        v["CMAKE_INSTALL_PREFIX"] = cfg["install_prefix"]
        for name in ("MODDIR", "SHAREDIR", "BINDIR", "BASEDIR"):
            v["INSTALL_DEFAULT_" + name] = layout
    if IS_MACOS:
        v["CMAKE_OSX_DEPLOYMENT_TARGET"] = cfg["macos_deployment_target"]
        v["CMAKE_OSX_ARCHITECTURES"] = cfg["macos_architectures"]
        if cfg["macos_sysroot"]:
            v["XCODE_SDK_PATH"] = cfg["macos_sysroot"]
    if cfg["lsp"]:
        v["CMAKE_EXPORT_COMPILE_COMMANDS"] = 1

    # Raw user overrides always win
    v.update(cfg["cmake"])
    return v


def configure_cmd(cfg, generator):
    # Build the full CMake configure command line
    cmd = ["cmake"]
    if generator:
        cmd += ["-G", generator]
    if IS_WINDOWS:
        # Only the Visual Studio generators support platform and toolset
        if not generator or generator.startswith("Visual Studio"):
            cmd += ["-A", "x64" if cfg["arch"] == "64" else "Win32"]
            if cfg["toolset"]:
                cmd += ["-T", cfg["toolset"]]
    for name, value in cmake_vars(cfg).items():
        # Booleans from the raw overrides become CMake ON/OFF
        if isinstance(value, bool):
            value = "ON" if value else "OFF"
        cmd.append(f"-D{name}={value}")
    return cmd + cfg["cmake_args"] + [ROOT]


def compiler_env(cfg):
    # Setup CC/CXX, CMake cross compiling does not set -m32 reliably everywhere
    # (see https://cmake.org/pipermail/cmake/2016-April/063312.html)
    env = os.environ.copy()
    if IS_WINDOWS:
        return env
    compiler = cfg["compiler"]
    if compiler is None:
        if env.get("CC") or env.get("CXX"):
            return env
        if shutil.which("gcc") and shutil.which("g++"):
            compiler = "gcc"
        elif shutil.which("clang") and shutil.which("clang++"):
            compiler = "clang"
        else:
            die("Missing compiler (gcc/g++ or clang/clang++)")
    cc, cxx = {"gcc": ("gcc", "g++"), "clang": ("clang", "clang++")}[compiler]
    suffix = " -m32" if cfg["arch"] == "32" else ""
    env["CC"] = cc + suffix
    env["CXX"] = cxx + suffix
    return env


def version_info():
    # Full and short version strings, CI provides them via the environment
    describe = (
        os.environ.get("CI_ETL_DESCRIBE")
        or capture(["git", "describe", "--abbrev=7", "--tags"])
        or ""
    )
    tag = (
        os.environ.get("CI_ETL_TAG")
        or capture(["git", "describe", "--abbrev=0", "--tags"])
        or ""
    )
    return describe, tag


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def print_startup(cfg, env):
    # Print the environment overview and check for required tools
    if Out.silent:
        return
    head("ET: Legacy dev tool")
    head("===============================")
    print(f"  version   : {version_info()[0] or 'unknown'}")
    if IS_MACOS:
        distro = f"macOS {platform.mac_ver()[0]}"
    elif IS_WINDOWS:
        distro = f"Windows {platform.version()}"
    else:
        try:
            distro = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
        except (AttributeError, OSError):
            distro = "Linux"
    print(f"  running on: {platform.system()} {platform.machine()} - {distro}")
    print(f"  build dir : {cfg['build_dir']}")
    print()

    apps = (
        (("cmake", True),)
        if IS_WINDOWS
        else (
            ("autoconf", True),
            ("libtoolize", True),
            ("cmake", True),
            ("gcc", True),
            ("g++", True),
            ("clang", False),
            ("clang++", False),
            ("git", False),
            ("zip", True),
            ("nasm", True),
        )
    )
    for app, required in apps:
        path = shutil.which(app)
        if path:
            status = f"{Out.c(BOLD_GREEN)}found{Out.c(RESET)}: {path}"
        elif required:
            status = f"{Out.c(BOLD_RED)}not found{Out.c(RESET)}"
        else:
            status = f"{Out.c(BOLD_YELLOW)}not found but no problem{Out.c(RESET)}"
        print(f"  {app:10} {status}")
    if not IS_WINDOWS:
        print(f"\n  CC  = {env.get('CC', '')}\n  CXX = {env.get('CXX', '')}")


def prepare(cfg):
    # Common checks and setup for every command that runs CMake
    if not shutil.which("cmake"):
        die("Missing CMake, cannot proceed.")
    if IS_MACOS and cfg["arch"] == "32":
        major, minor = (platform.mac_ver()[0].split(".") + ["0"])[:2]
        if int(major) > 10 or int(minor) > 13:
            die(
                f"You can't compile 32bit binaries with macOS {major}.{minor}. Use '-64'."
            )

    env = compiler_env(cfg)
    print_startup(cfg, env)

    # Fetch the bundled libraries submodule if needed
    if not os.path.isfile(os.path.join(LIBS_DIR, "CMakeLists.txt")):
        info("Getting bundled libs...")
        run_git(["submodule", "init"])
        run_git(["submodule", "update"])
    return env


def cmd_clean(cfg):
    info("Clean...")
    rmtree(cfg["build_dir"])
    if IS_WINDOWS:
        rmtree(cfg["game_basepath"])

    if not os.path.isfile(os.path.join(LIBS_DIR, "CMakeLists.txt")):
        return

    # Remove in-source build leftovers of the bundled libraries
    if not IS_WINDOWS and shutil.which("make"):
        for subdir, target in LIB_CLEAN_DIRS:
            path = os.path.join(LIBS_DIR, subdir)
            if os.path.isfile(os.path.join(path, "Makefile")):
                info(f"Cleaning {subdir}...")
                run(["make", target], cwd=path, check=False)
    run_git(["clean", "-d", "-f"], cwd=LIBS_DIR)


def cmd_generate(cfg):
    env = prepare(cfg)
    generator = cfg["generator"] or (None if IS_WINDOWS else "Unix Makefiles")
    info(f"Generating build files{': ' + generator if generator else ''}...")
    os.makedirs(cfg["build_dir"], exist_ok=True)

    # Point clangd to the compilation database
    if cfg["lsp"]:
        info("Setting up clangd (for lsp)")
        with open(os.path.join(ROOT, ".clangd"), "w") as fh:
            fh.write(f"CompileFlags:\n  CompilationDatabase: \"{cfg['build_dir']}\"\n")

    run(configure_cmd(cfg, generator), cwd=cfg["build_dir"], env=env)
    return env


def build_cmd(cfg, target=None):
    # Assemble the CMake build command line
    cmd = ["cmake", "--build", cfg["build_dir"], "--config", cfg["build_type"]]
    if target:
        cmd += ["--target", target]
    if cfg["jobs"]:
        cmd += ["--parallel", str(cfg["jobs"])]
    if cfg["build_tool_args"]:
        cmd += ["--"] + cfg["build_tool_args"]
    return cmd


def cmd_build(cfg, quick=False):
    # Quick builds skip the configure step when the build dir is configured
    if quick and os.path.isfile(os.path.join(cfg["build_dir"], "CMakeCache.txt")):
        env = compiler_env(cfg)
    else:
        env = cmd_generate(cfg)
    info("Build...")
    run(build_cmd(cfg), env=env)


def create_macos_dmg(cfg):
    # Create the macOS DMG installer from the CPack output
    build_dir = cfg["build_dir"]
    for app in ("magick", "node", "npx"):
        if not shutil.which(app):
            print(f"Missing {app}, skipping macOS installer creation")
            return
    if not shutil.which("rsvg-convert"):
        die("Missing rsvg-convert, cannot create installer")

    info("Generating macOS installer")
    describe, tag = version_info()
    misc = os.path.join(ROOT, "misc")

    # Folder icon and DMG backgrounds
    with open(os.path.join(build_dir, "icon.png"), "wb") as fh:
        subprocess.run(
            ["rsvg-convert", "-h", "256", os.path.join(misc, "etl.svg")],
            stdout=fh,
            check=True,
        )
    for name, size, points, pos in (
        ("osx-dmg-background.jpg", "640x360", "20", "80,355"),
        ("osx-dmg-background@2x.jpg", "1280x720", "40", "165,710"),
    ):
        run(
            [
                "magick",
                "convert",
                os.path.join(misc, "osx-dmg-background.jpg"),
                "-resize",
                size,
                "-font",
                os.path.join(misc, "din1451alt.ttf"),
                "-pointsize",
                points,
                "-fill",
                "rgb(85,85,85)",
                "-draw",
                f"text {pos} '{tag}'",
                name,
            ],
            cwd=build_dir,
        )

    # Copy the CPack folder and give it the icon
    tgz_dir = os.path.join(build_dir, "_CPack_Packages", "Darwin", "TGZ")
    packs = (
        [p for p in os.listdir(tgz_dir) if p.startswith("etlegacy")]
        if os.path.isdir(tgz_dir)
        else []
    )
    if not packs:
        die(f"No CPack TGZ folder found in {tgz_dir}")
    target = os.path.join(build_dir, "ETLegacy")
    rmtree(target)
    shutil.copytree(os.path.join(tgz_dir, packs[0]), target, symlinks=True)
    run(["sips", "-i", "icon.png"], cwd=build_dir)
    with open(os.path.join(build_dir, "tmpicons.rscs"), "wb") as fh:
        subprocess.run(
            ["DeRez", "-only", "icns", "icon.png"], cwd=build_dir, stdout=fh, check=True
        )
    run(["Rez", "-append", "tmpicons.rscs", "-o", "ETLegacy/Icon\r"], cwd=build_dir)
    run(["SetFile", "-a", "C", "ETLegacy"], cwd=build_dir)
    run(["SetFile", "-a", "V", "ETLegacy/Icon\r"], cwd=build_dir)

    # Generate the DMG using appdmg (https://github.com/LinusU/node-appdmg)
    spec = {
        "title": f"ET Legacy {tag}",
        "icon": os.path.join(misc, "etl.icns"),
        "background": "osx-dmg-background.jpg",
        "window": {"size": {"width": 640, "height": 390}},
        "contents": [
            {"x": 456, "y": 250, "type": "link", "path": "/Applications"},
            {"x": 192, "y": 250, "type": "file", "path": "ETLegacy"},
        ],
    }
    with open(os.path.join(build_dir, "etlegacy-dmg.json"), "w") as fh:
        json.dump(spec, fh, indent=2)
    run(["npm", "i", "-D", "appdmg@0.6.6"], cwd=build_dir)
    run(
        ["npx", "appdmg", "etlegacy-dmg.json", f"etlegacy-{describe}.dmg"],
        cwd=build_dir,
    )


def cmd_package(cfg):
    info("Package...")
    run(["cpack"], cwd=cfg["build_dir"])
    if IS_MACOS:
        create_macos_dmg(cfg)


def copy_files(src_dir, patterns, dst_dir):
    # Copy files matching any of the glob patterns (non-recursive)
    if not os.path.isdir(src_dir):
        return
    os.makedirs(dst_dir, exist_ok=True)
    for name in os.listdir(src_dir):
        path = os.path.join(src_dir, name)
        if os.path.isfile(path) and any(fnmatch.fnmatch(name, p) for p in patterns):
            shutil.copy2(path, dst_dir)


def cmd_install(cfg):
    info("Install...")
    if not IS_WINDOWS:
        run(build_cmd(cfg, target="install"), env=compiler_env(cfg))
        return

    # Windows: copy the build into the game base path
    base, mod = cfg["game_basepath"], cfg["mod_name"]
    build_dir = cfg["build_dir"]
    if not os.path.isdir(base):
        print(f"Will create base directory: {base}")
        os.makedirs(base)

    # Remove stale mod binaries from both base and home path
    for path in (os.path.join(base, mod), os.path.join(cfg["game_homepath"], mod)):
        if os.path.isdir(path):
            for name in os.listdir(path):
                if any(fnmatch.fnmatch(name, p) for p in ("*.pk3", "*.dll", "*.dat")):
                    os.remove(os.path.join(path, name))

    copy_files(build_dir, ("et*.exe", "renderer_opengl*.dll"), base)
    copy_files(
        os.path.join(build_dir, mod),
        ("*.pk3", "qagame*.dll", "*.dat"),
        os.path.join(base, mod),
    )
    copy_files(
        os.path.join(ROOT, "misc", "etmain"), ("*",), os.path.join(base, "etmain")
    )
    copy_files(
        os.path.join(ROOT, "misc"), ("description.txt",), os.path.join(base, mod)
    )
    copy_files(os.path.join(ROOT, "docs"), ("INSTALL.txt",), base)
    copy_files(ROOT, ("COPYING.txt",), base)

    # Desktop shortcut
    def ps_quote(s):
        return "'" + s.replace("'", "''") + "'"

    link = os.path.join(
        os.environ.get("USERPROFILE", os.path.expanduser("~")),
        "Desktop",
        "ETLegacy.lnk",
    )
    script = f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({ps_quote(link)}); $s.TargetPath = {ps_quote(os.path.join(base, 'etl.exe'))}; $s.WorkingDirectory = {ps_quote(base)}; $s.Save()"
    run(["powershell", "-NoProfile", "-Command", script], check=False)
    info(
        f"The {cfg['build_type']} build has been installed in {base}, and a shortcut has been added to your desktop"
    )


def cmd_download(cfg):
    info("Downloading packages...")
    etmain = os.path.join(cfg["game_homepath"], "etmain")
    os.makedirs(etmain, exist_ok=True)
    for name in ("pak0.pk3", "pak1.pk3", "pak2.pk3"):
        target = os.path.join(etmain, name)
        if os.path.isfile(target):
            continue
        print(f"  {MIRROR + name} -> {target}")
        # Download into a temporary file so aborted downloads are not kept
        urllib.request.urlretrieve(MIRROR + name, target + ".part")
        os.replace(target + ".part", target)


def cmd_autoformat(cfg, args):
    # Run the repository formatter (misc/autoformat.py) with this Python
    if sys.version_info < (3, 10):
        die(
            "autoformat needs Python 3.10+ (misc/autoformat.py), running "
            f"{sys.version_info[0]}.{sys.version_info[1]}"
        )
    cmd = [sys.executable, os.path.join(ROOT, "misc", "autoformat.py")]
    if args.commit:
        cmd.append(args.commit)
    if args.format_all:
        cmd.append("--all")
    info("Autoformat...")
    run(cmd, cwd=ROOT)


def cmd_project(cfg):
    info("Project...")
    rmtree(cfg["project_dir"])
    os.makedirs(cfg["project_dir"])
    env = prepare(cfg)
    generator = "Xcode" if IS_MACOS else cfg["generator"]
    run(configure_cmd(cfg, generator), cwd=cfg["project_dir"], env=env)
    if IS_WINDOWS:
        os.startfile(os.path.join(cfg["project_dir"], "ETLEGACY.sln"))


def cmd_updatelicense(cfg):
    info("Updating license year")
    year = str(datetime.date.today().year).encode()
    replacement = b"2012-" + year

    def update(path, require_copyright):
        with open(path, "rb") as fh:
            data = fh.read()
        if require_copyright and not COPYRIGHT_RE.search(data):
            return
        new = YEAR_RANGE_RE.sub(replacement, data)
        if new != data:
            with open(path, "wb") as fh:
                fh.write(new)

    for top in ("src", "etmain"):
        for dirpath, _dirnames, filenames in os.walk(os.path.join(ROOT, top)):
            for name in filenames:
                update(os.path.join(dirpath, name), True)
    update(os.path.join(ROOT, "README.md"), False)
    update(os.path.join(ROOT, "src", "sys", "win_resource.rc"), False)


def cmd_watch(cfg):
    # Rebuild whenever a file below ./src changes (polling, no extra deps)
    def snapshot():
        state = {}
        for dirpath, _dirnames, filenames in os.walk(SOURCE_DIR):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    state[path] = os.stat(path).st_mtime_ns
                except OSError:
                    pass
        return state

    if not os.path.isfile(os.path.join(cfg["build_dir"], "CMakeCache.txt")):
        cmd_generate(cfg)
    env = compiler_env(cfg)
    previous = None
    try:
        while True:
            current = snapshot()
            if current != previous:
                previous = current
                # Clear the screen, needs escape sequence support
                if Out.color:
                    print("\033c", end="")
                run(build_cmd(cfg), env=env, check=False)
                info(f"Watching {SOURCE_DIR} for changes (Ctrl+C to stop)...")
            time.sleep(1)
    except KeyboardInterrupt:
        print()


def cmd_open(cfg):
    path = cfg["game_basepath"] if IS_WINDOWS else cfg["install_prefix"]
    if IS_WINDOWS:
        os.startfile(path)
    else:
        run(["open" if IS_MACOS else "xdg-open", path])


def cmd_config(cfg):
    # Show the effective settings and the resulting CMake command
    print(json.dumps(cfg, indent=2, sort_keys=True))
    generator = cfg["generator"] or (None if IS_WINDOWS else "Unix Makefiles")
    print("\n" + subprocess.list2cmdline(configure_cmd(cfg, generator)))
    print(subprocess.list2cmdline(build_cmd(cfg)))


def cmd_release(cfg, args):
    # Bump VERSION.txt, commit and tag a new release
    rel = cfg["release"]
    os.chdir(ROOT)
    branch = capture(["git", "branch", "--show-current"])
    if branch != rel["branch"]:
        die(f"Not in {rel['branch']}, exiting")
    if capture(["git", "status", "--porcelain"]) != "":
        die("Git repository is not clean, exiting")
    if not os.path.isfile("VERSION.txt"):
        die("VERSION.txt file not found, exiting")

    with open("VERSION.txt", "r", newline="") as fh:
        content = fh.read()
    version = {}
    for part in ("MAJOR", "MINOR", "PATCH"):
        match = re.search(rf"VERSION_{part}\s+([0-9]+)", content)
        if not match:
            die(f"VERSION_{part} missing in VERSION.txt")
        version[part] = int(match.group(1))

    # Bump the requested part
    bump = getattr(args, "bump", None)
    if bump is None:
        print("Nothing to do")
        return
    if bump == "major":
        version.update(MAJOR=version["MAJOR"] + 1, MINOR=0, PATCH=0)
    elif bump == "minor":
        version.update(MINOR=version["MINOR"] + 1, PATCH=0)
    else:
        version["PATCH"] += 1
    major, minor, patch = version["MAJOR"], version["MINOR"], version["PATCH"]
    number = f"{major}.{minor}.{patch}"
    tag = "v" + number

    # The tag must not be taken yet
    if capture(["git", "tag", "-l", tag]):
        die(f"Tag '{tag}' was taken")
    if patch == 0 and capture(["git", "tag", "-l", f"v{major}.{minor}"]):
        die(f"Tag 'v{major}.{minor}' was taken")

    message = getattr(args, "message", None) or f"Version {number}"
    print(f"Ready to commit and tag a new version: {number}")
    print(f"Version message will be: {message}")
    if input("Ready to commit? [Y/N]: ").strip().lower() not in ("y", "yes"):
        print("Chicken!")
        return

    # Update the version file
    for part in ("MAJOR", "MINOR", "PATCH"):
        content = re.sub(
            rf"(VERSION_{part})\s+[0-9]+", rf"\g<1> {version[part]}", content
        )
    with open("VERSION.txt", "w", newline="") as fh:
        fh.write(content)

    commit_msg = f"Incrementing version number to {number}"
    if rel["sign"]:
        print("Create a SIGNED release")
        run(["git", "commit", "-a", "-S", "-m", commit_msg])
        run(["git", "tag", "-s", tag, "-m", message])
    else:
        print("Create an UNSIGNED release")
        run(["git", "commit", "-a", "-m", commit_msg])
        run(["git", "tag", "-a", tag, "-m", message])
    print("Committed and tagged a new release")

    # Optionally push the result
    remote = (
        rel["remote"]
        or input("Enter the name of the upstream remote (default: origin): ").strip()
        or "origin"
    )
    if input(f"Push commit and tag to remote '{remote}'? [Y/N]: ").strip().lower() in (
        "y",
        "yes",
    ):
        run(["git", "push", remote, "HEAD"])
        run(["git", "push", remote, tag])
        print(f"Pushed data to remote '{remote}'. Congrats!")
    else:
        print(
            f"You need to 'git push {remote} HEAD' and 'git push {remote} {tag}' manually."
        )


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def toggle(group, name, dest, help_text):
    # Add a --NAME / --no-NAME pair which only sets a value when given
    group.add_argument(
        "--" + name,
        dest=dest,
        action="store_const",
        const=True,
        help="enable " + help_text,
    )
    group.add_argument(
        "--no-" + name,
        dest=dest,
        action="store_const",
        const=False,
        help="disable " + help_text,
    )


def build_parser():
    sup = argparse.SUPPRESS

    # Options shared by all commands
    common = argparse.ArgumentParser(add_help=False, argument_default=sup)
    group = common.add_argument_group("general")
    group.add_argument(
        "--config",
        dest="config_file",
        metavar="PATH",
        help="TOML config file (default: ./dev-cfg.toml if present)",
    )
    group.add_argument(
        "--silent", action="store_const", const=True, help="reduce output"
    )
    toggle(
        group, "color", "color", "colored output (default: if the terminal supports it)"
    )

    # Directory options
    paths = argparse.ArgumentParser(add_help=False, argument_default=sup)
    group = paths.add_argument_group("paths")
    group.add_argument(
        "--build-dir",
        "--build",
        dest="build_dir",
        metavar="DIR",
        help="build directory (default: ./build)",
    )
    group.add_argument(
        "--project-dir",
        dest="project_dir",
        metavar="DIR",
        help="IDE project directory (default: ./project)",
    )
    group.add_argument(
        "--prefix",
        dest="install_prefix",
        metavar="DIR",
        help="install prefix (default: ~/etlegacy)",
    )
    group.add_argument(
        "--game-homepath",
        dest="game_homepath",
        metavar="DIR",
        help="game home path, used by 'download' and the Windows install",
    )
    group.add_argument(
        "--game-basepath",
        dest="game_basepath",
        metavar="DIR",
        help="Windows install target (default: ~/Documents/ETLegacy-Build)",
    )

    # CMake configure options
    configure = argparse.ArgumentParser(add_help=False, argument_default=sup)
    group = configure.add_argument_group("configure")
    group.add_argument(
        "--build-type",
        dest="build_type",
        metavar="TYPE",
        help="CMake build type (default: Release)",
    )
    group.add_argument(
        "--debug",
        dest="build_type",
        action="store_const",
        const="Debug",
        help="shortcut for --build-type Debug",
    )
    group.add_argument(
        "--arch",
        choices=("32", "64"),
        help="target architecture (default: 32 on x86 Linux/Windows, else 64)",
    )
    group.add_argument(
        "-32",
        dest="arch",
        action="store_const",
        const="32",
        help="shortcut for --arch 32",
    )
    group.add_argument(
        "-64",
        dest="arch",
        action="store_const",
        const="64",
        help="shortcut for --arch 64",
    )
    group.add_argument(
        "--compiler",
        choices=("gcc", "clang"),
        help="compiler (default: $CC/$CXX, else gcc, else clang)",
    )
    group.add_argument(
        "--gcc",
        dest="compiler",
        action="store_const",
        const="gcc",
        help="shortcut for --compiler gcc",
    )
    group.add_argument(
        "--clang",
        dest="compiler",
        action="store_const",
        const="clang",
        help="shortcut for --compiler clang",
    )
    group.add_argument("--generator", "-G", metavar="NAME", help="CMake generator")
    group.add_argument(
        "--ninja",
        dest="generator",
        action="store_const",
        const="Ninja",
        help="shortcut for --generator Ninja",
    )
    group.add_argument(
        "--toolset", metavar="NAME", help="Visual Studio platform toolset (-T)"
    )
    group.add_argument(
        "--toolchain",
        dest="toolchain_file",
        metavar="FILE",
        help="CMake toolchain file",
    )
    group.add_argument(
        "--target", choices=tuple(TARGET_PRESETS), help="what to build (default: all)"
    )
    group.add_argument(
        "--mod",
        dest="target",
        action="store_const",
        const="mod",
        help="shortcut for --target mod",
    )
    group.add_argument(
        "--server",
        dest="target",
        action="store_const",
        const="server",
        help="shortcut for --target server",
    )
    group.add_argument(
        "--rpi", action="store_const", const=True, help="Raspberry PI / aarch64 build"
    )
    group.add_argument(
        "--rpi-toolchain",
        dest="rpi_toolchain",
        action="store_const",
        const=True,
        help="--rpi using the aarch64 cross toolchain unless --toolchain is set",
    )
    group.add_argument(
        "--modname",
        dest="mod_name",
        metavar="NAME",
        help=f"mod name (default: legacy, known: {', '.join(MOD_URLS)})",
    )
    group.add_argument(
        "--modurl",
        dest="mod_url",
        metavar="URL",
        help="mod URL (default: derived from the mod name)",
    )
    group.add_argument(
        "--osx",
        "--macos-target",
        dest="macos_deployment_target",
        metavar="VER",
        help="macOS deployment target (default: 11.0)",
    )
    group.add_argument(
        "--osx-arc",
        "--macos-arch",
        dest="macos_architectures",
        metavar="ARCHS",
        help="macOS architectures, e.g. 'x86_64;arm64' (default: host)",
    )
    group.add_argument(
        "--sysroot", dest="macos_sysroot", metavar="DIR", help="macOS SDK path"
    )
    group.add_argument(
        "--lsp",
        action="store_const",
        const=True,
        help="export compile_commands.json and point .clangd to it",
    )
    toggle(
        group,
        "portable-install",
        "portable_install",
        "relative install layout below --prefix",
    )
    toggle(group, "ssl", "ssl", "SSL (and auth when disabling)")
    toggle(group, "auth", "auth", "authentication")
    toggle(group, "db", "db", "database support")
    toggle(group, "r2", "renderer2", "renderer2")
    toggle(group, "dynamic", "renderer_dynamic", "dynamic renderer build")
    toggle(group, "bundled-libs", "bundled_libs", "all bundled libraries")
    toggle(group, "bundled-jpeg", "bundled_jpeg", "bundled libjpeg-turbo")
    toggle(group, "extra", "extra", "installing Omni-bot, GeoIP and WolfAdmin")
    toggle(group, "update", "autoupdate", "autoupdate")
    toggle(group, "pk3", "mod_pk3", "building the mod pk3")
    toggle(group, "zip-only", "zip_only", "zip only mod packaging")
    group.add_argument(
        "-D",
        dest="defines",
        action="append",
        metavar="NAME=VALUE",
        help="set a raw CMake variable (overrides everything)",
    )
    group.add_argument(
        "--cmake-arg",
        dest="extra_cmake_args",
        action="append",
        metavar="ARG",
        help="extra raw argument for the CMake configure call",
    )

    # Build options
    build = argparse.ArgumentParser(add_help=False, argument_default=sup)
    group = build.add_argument_group("build")
    group.add_argument(
        "-j",
        "--jobs",
        dest="jobs",
        type=int,
        nargs="?",
        const=None,
        metavar="N",
        help="parallel jobs (default: CPU count, 0: tool default)",
    )

    parser = argparse.ArgumentParser(
        prog="dev.py",
        parents=[common],
        description="ET: Legacy developer tool. Arguments after '--' are passed to the "
        "native build tool.",
        epilog="Settings can be persisted in ./dev-cfg.toml (see ./dev-cfg-example.toml).",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # name -> (help, parents)
    commands = (
        (
            "clean",
            "remove the build directory and bundled lib leftovers",
            [common, paths],
        ),
        ("generate", "generate the build files", [common, paths, configure]),
        ("build", "generate and build", [common, paths, configure, build]),
        (
            "quickbuild",
            "build, only generating when not configured yet",
            [common, paths, configure, build],
        ),
        (
            "package",
            "create packages via CPack (plus DMG on macOS)",
            [common, paths, configure],
        ),
        (
            "install",
            "install the build (Windows: into the game base path)",
            [common, paths, configure, build],
        ),
        (
            "all",
            "clean, build, package (not on Windows) and install",
            [common, paths, configure, build],
        ),
        ("dist", "clean, build and package", [common, paths, configure, build]),
        (
            "project",
            "generate IDE project files (Xcode / Visual Studio)",
            [common, paths, configure],
        ),
        (
            "download",
            "download the etmain assets into the game home path",
            [common, paths],
        ),
        (
            "autoformat",
            "format changed files (or all with --all) via misc/autoformat.py",
            [common],
        ),
        ("updatelicense", "update the license years to the current year", [common]),
        ("watch", "rebuild whenever ./src changes", [common, paths, configure, build]),
        ("open", "open the install directory", [common, paths]),
        (
            "config",
            "print the effective settings and CMake command",
            [common, paths, configure, build],
        ),
        ("release", "bump VERSION.txt, commit and tag a new version", [common]),
    )
    subs = {}
    for name, help_text, parents in commands:
        subs[name] = sub.add_parser(
            name,
            help=help_text,
            description=help_text,
            parents=parents,
            argument_default=sup,
        )

    # Version bump options
    group = subs["release"].add_argument_group("release").add_mutually_exclusive_group()
    group.add_argument(
        "--major",
        dest="bump",
        action="store_const",
        const="major",
        help="bump the major version",
    )
    group.add_argument(
        "--minor",
        dest="bump",
        action="store_const",
        const="minor",
        help="bump the minor version",
    )
    group.add_argument(
        "--patch",
        dest="bump",
        action="store_const",
        const="patch",
        help="bump the patch version",
    )
    subs["release"].add_argument(
        "--sign",
        dest="sign",
        action="store_const",
        const=True,
        help="GPG sign the commit and tag",
    )
    subs["release"].add_argument("-m", "--message", dest="message", help="tag message")
    subs["release"].add_argument("--remote", dest="remote", help="remote to push to")

    # Autoformat options, passed on to misc/autoformat.py
    subs["autoformat"].add_argument(
        "commit",
        nargs="?",
        help="format the files changed compared to this commit "
        "(default: .upstream-remote-branch, else origin/master)",
    )
    subs["autoformat"].add_argument(
        "--all",
        dest="format_all",
        action="store_const",
        const=True,
        help="format all git-tracked files",
    )
    return parser


def main(argv):
    # Everything after "--" is passed to the native build tool
    tool_args = None
    if "--" in argv:
        index = argv.index("--")
        argv, tool_args = argv[:index], argv[index + 1 :]

    parser = build_parser()
    args = parser.parse_args(argv)
    command = getattr(args, "command", None)
    if command is None:
        parser.print_help()
        return 0
    del args.command

    # Command specific options (release, autoformat) are not settings
    cmd_args = argparse.Namespace()
    for key in ("bump", "message", "commit", "format_all"):
        setattr(cmd_args, key, getattr(args, key, None))
        if hasattr(args, key):
            delattr(args, key)

    # Colors for errors while loading the settings, then the configured ones
    cli_color = getattr(args, "color", None)
    Out.setup("auto", cli_color)
    cfg = resolve_settings(args)
    if tool_args is not None:
        cfg["build_tool_args"] = tool_args
    Out.silent = cfg["silent"]
    Out.setup(cfg["color"], cli_color)

    if command == "clean":
        cmd_clean(cfg)
    elif command == "generate":
        cmd_generate(cfg)
    elif command == "build":
        cmd_build(cfg)
    elif command == "quickbuild":
        cmd_build(cfg, quick=True)
    elif command == "package":
        cmd_package(cfg)
    elif command == "install":
        cmd_install(cfg)
    elif command in ("all", "dist"):
        cmd_clean(cfg)
        cmd_build(cfg)
        if command == "dist" or not IS_WINDOWS:
            cmd_package(cfg)
        if command == "all":
            cmd_install(cfg)
    elif command == "project":
        cmd_project(cfg)
    elif command == "download":
        cmd_download(cfg)
    elif command == "autoformat":
        cmd_autoformat(cfg, cmd_args)
    elif command == "updatelicense":
        cmd_updatelicense(cfg)
    elif command == "watch":
        cmd_watch(cfg)
    elif command == "open":
        cmd_open(cfg)
    elif command == "config":
        cmd_config(cfg)
    elif command == "release":
        cmd_release(cfg, cmd_args)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
