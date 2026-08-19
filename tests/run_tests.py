#!/usr/bin/env python3
"""End-to-end tests: drive `lldb -b --no-lldbinit` against test_bin with
sample.natvis loaded, then regex-assert the formatted output."""

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.path.join(HERE, "test_bin")

VARIABLES = [
    "vec", "aliasRef", "fixed", "list", "map", "str", "wstr", "colors",
    "temp", "circle", "square", "self", "optZero", "optOther", "v3", "dq",
    "table", "both", "alias", "views", "badsize", "recintr", "et",
]

# (test name, regex expected to match the transcript; DOTALL applied)
EXPECTATIONS = [
    ("wildcard summary + $T1",
     r"\(MyVector<int>\) vec = \{ size=5 \}"),
    ("ArrayItems children",
     r"vec = \{ size=5 \}.*?\[size\] = 5.*?\[capacity\] = 8.*?"
     r"\[0\] = 10.*?\[4\] = 50"),
    ("typedef + reference cascade",
     r"aliasRef = .*?\{ size=5 \}"),
    ("{arr,[n]} array preview",
     r"arr\(6\) = \{0, 1, 4, 9, 16, 25\}"),
    ("Synthetic child with own DisplayString",
     r"\[grid\] = 3x4 grid"),
    ("multi-dim ArrayItems (spot check)",
     r"\[1,2\] = 12"),
    ("LinkedListItems",
     r"list = \{ count=3 \}.*?\[0\] = 1.*?\[1\] = 2.*?\[2\] = 3"),
    ("TreeItems in-order",
     r"map = \{ size=5 \}.*?\[0\] = 100.*?\[1\] = 200.*?\[2\] = 300"
     r".*?\[3\] = 400.*?\[4\] = 500"),
    ("string ,s",
     r'str = "hello natvis" \(len=12\)'),
    ("string ,su (utf-16)",
     r'wstr = "wide text"'),
    ("enum ,en + flags + ,x",
     r"colors = c=Green f=F_A \| F_C raw=0x0*5"),
    ("Intrinsic with Parameter",
     r"temp = 20C = 68F"),
    ("derived type summary",
     r"circle = circle r=2\.5"),
    ("ExpandedItem base-class splice",
     r"circle = .*?\[radius\] = 2\.5.*?id = 7"),
    ("Inheritable base-walk summary",
     r"square = Shape #8"),
    ("ExpandedItem this-splice",
     r"self = sum=33.*?\[sum\] = 33.*?x = 11.*?y = 22"),
    ("DisplayString Condition first-match",
     r"optZero = zero:42"),
    ("DisplayString Optional skip + fallthrough",
     r"optOther = other:43"),
    ("Synthetic computed DisplayString",
     r"\[len2\] = 25"),
    ("IndexListItems with $i",
     r"dq = \{ len=4 \}.*?\[0\] = 100.*?\[1\] = 200.*?\[2\] = 300"
     r".*?\[3\] = 400"),
    ("CustomListItems hash table",
     r"table = \{ count=3 \}.*?\[9\] = 999.*?\[1\] = 111.*?\[4\] = 444"),
    ("Priority High wins",
     r"both = high:77"),
    ("AlternativeType",
     r"alias = alias v=55"),
    ("views: view(simple) vs default",
     r"views = \[brief:5\] \[full:5 6\]"),
    ("stringview command",
     r"^hello natvis$"),
    ("regression: negative Size yields zero children",
     r"badsize = bad n=-3\s*\n\(lldb\)"),
    ("regression: recursive Intrinsic bounded, Optional falls through",
     r"recintr = rec-safe:9"),
    ("unload restores raw children",
     r"mSize = 5"),
    # Tier-1 expression engine (C semantics: int division truncates toward 0)
    ("expr: arithmetic + C division",
     r"et = 7/2=3 rem=1 negdiv=-3 f=17\.5"),
    ("expr: ternary",
     r"\[ternary\] = 700"),
    ("expr: shifts and bit-or",
     r"\[shifts\] = 33"),
    ("expr: logical operators",
     r"\[logic\] = 1"),
    ("expr: operator precedence",
     r"\[precedence\] = 12"),
    ("expr: computed index",
     r"\[deref-index\] = 40"),
    ("expr: pointer arithmetic",
     r"\[ptr-math\] = 30"),
    ("expr: unary minus / not",
     r"\[negate\] = -7.*?\[not\] = 0"),
    ("expr: computed Size and ValuePointer offset",
     r"\[not\] = 0.*?\[0\] = 20.*?\[1\] = 30.*?\[2\] = 40"),
    # perf guard: the sample natvis must never need LLDB's expression compiler
    # except for the one deliberately-missing member (see OptHolder)
    ("perf: no unnecessary Tier-2 compiles",
     r"eval paths\s+: fast=\d+ int=\d+ slow=0 slow_fail=1"),
]

# (test name, regex, minimum occurrence count across the transcript)
COUNT_EXPECTATIONS = [
    ("reload restores natvis formatting",
     r"vec = \{ size=5 \}", 2),
]

NEGATIVE = [
    ("no lldb evaluation errors leaked", r"<error"),
    ("no python tracebacks", r"Traceback \(most recent call last\)"),
    ("no unresolved summary markers", r"\?\?"),
]


def find_break_line():
    with open(os.path.join(HERE, "test.cpp")) as f:
        for i, line in enumerate(f, 1):
            if "BREAK HERE" in line:
                return i
    raise SystemExit("no BREAK HERE marker in test.cpp")


def run_lldb():
    cmds = [
        "command script import %s" % os.path.join(ROOT, "lldb_natvis.py"),
        "natvis load %s" % os.path.join(HERE, "sample.natvis"),
        "b test.cpp:%d" % find_break_line(),
        "run",
    ]
    for var in VARIABLES:
        cmds.append("frame variable %s" % var)
    cmds.append("natvis stringview str")
    cmds.append("natvis status")
    # unload/reload lifecycle: raw children come back, then formatting returns
    cmds.append("natvis unload --all")
    cmds.append("frame variable vec")
    cmds.append("natvis load %s" % os.path.join(HERE, "sample.natvis"))
    cmds.append("frame variable vec")
    cmds.append("quit")
    argv = ["xcrun", "lldb", "-b", "--no-lldbinit", BIN]
    for c in cmds:
        argv += ["-o", c]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    return proc.stdout + proc.stderr


def main():
    if not os.path.exists(BIN):
        raise SystemExit("test_bin missing; run ./build.sh first")
    transcript = run_lldb()
    if "--verbose" in sys.argv:
        print(transcript)
    failures = []
    for name, pattern in EXPECTATIONS:
        if not re.search(pattern, transcript, re.DOTALL | re.MULTILINE):
            failures.append("FAIL (missing): %s\n  pattern: %s"
                            % (name, pattern))
    for name, pattern, min_count in COUNT_EXPECTATIONS:
        count = len(re.findall(pattern, transcript))
        if count < min_count:
            failures.append("FAIL (count %d < %d): %s\n  pattern: %s"
                            % (count, min_count, name, pattern))
    for name, pattern in NEGATIVE:
        m = re.search(pattern, transcript)
        if m:
            start = max(0, m.start() - 120)
            failures.append("FAIL (present): %s\n  context: ...%s..."
                            % (name, transcript[start:m.end() + 120]))
    total = len(EXPECTATIONS) + len(COUNT_EXPECTATIONS) + len(NEGATIVE)
    passed = total - len(failures)
    print("%d/%d checks passed" % (passed, total))
    if failures:
        print()
        for f in failures:
            print(f)
        if "--verbose" not in sys.argv:
            print("\n(re-run with --verbose for the full lldb transcript)")
        sys.exit(1)


if __name__ == "__main__":
    main()
