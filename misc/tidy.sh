#!/usr/bin/env bash
# calls clang-tidy in parallel of whole source tree and writes to 'log-tidy.txt'
#
# REQUIRES: clang-tidy parallel
set -eo pipefail

# cd to parent dir of current script
cd "$(dirname "${BASH_SOURCE[0]}")"
cd ..


tidy_log="log-tidy.txt"

# defer: clean colors from log output
cleanup_colors_from_log() {
	tmpfile=$(mktemp)
	if [[ -e "$tidy_log" ]]; then
		sed -e 's/\x1b\[[0-9;]*m//g' < "$tidy_log" > "$tmpfile"
		mv "$tmpfile" "$tidy_log"
	fi
}
trap 'cleanup_colors_from_log' EXIT INT

processing_count=$(nproc --all 2>/dev/null || true )
processing_count=${processing_count:-4}
find ./src \( -iname '*.h' -o -iname '*.c' \) \
	-a -type f -not -path "./src/Omnibot/*" \
	-print0 | parallel -0 "-j${processing_count}" clang-tidy --system-headers --quiet --use-color | tee "$tidy_log"

