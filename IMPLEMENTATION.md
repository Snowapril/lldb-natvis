# Implementation notes

How lldb-natvis is built and why. Read this before changing the internals;
`README.md` covers installation and usage.

## Problem

Visual Studio renders C++ types in the watch window from `.natvis` XML files.
LLDB has an equivalent *mechanism* (type summaries and synthetic children
providers) but no natvis front end, so on macOS the same codebase debugs with
raw struct members. This project bridges the two: parse natvis, and drive
LLDB's formatter machinery from the parsed model at runtime.

Constraints that shaped everything:

- Runs inside LLDB's **embedded Python 3.9** (Apple lldb-1703). Standard
  library only — no pip installs, no `match`, no PEP 604 unions.
- Code runs **inside debugger callbacks**. An unhandled exception or an
  unbounded loop doesn't fail a test, it freezes the user's debug session.
- Natvis expressions are C++ evaluated in the object's context. Fidelity
  matters more than speed, but a JIT compile per child is unusable at scale.

## Module map

| File | Responsibility |
|---|---|
| `lldb_natvis.py` | Entry point. `sys.path` fixup, `__lldb_init_module`, and the four names LLDB resolves by string: `natvis_summary`, `NatvisSyntheticProvider`, `natvis_match_summary`, `natvis_match_synthetic` |
| `natvis/model.py` | Dataclasses mirroring the natvis schema, document order preserved |
| `natvis/parser.py` | XML → model. Namespace-stripping, lenient (unknown elements warn, never raise) |
| `natvis/typename.py` | Wildcard patterns → loose regexes; structural matcher binding `$T1..$Tn` |
| `natvis/expr.py` | Preprocessing (`$Tn`, `$i`, intrinsics) and the two-tier evaluator |
| `natvis/formatspec.py` | Format specifier parsing and rendering (`,x ,s ,su ,en ,[n] ,view(x)` …) |
| `natvis/display.py` | DisplayString brace interpolation and the summary entry point |
| `natvis/providers.py` | Generic synthetic-children provider; one builder per `<Expand>` node kind |
| `natvis/interp.py` | `<CustomListItems>` mini-interpreter |
| `natvis/registry.py` | Loaded files, type dispatch, LLDB category registration, recursive discovery |
| `natvis/commands.py` | The `natvis` command and startup autoscan |

~2,900 lines of implementation, ~160 lines of test harness.

## Key design decisions

### 1. Formatter match callbacks, not name/regex specifiers

Every natvis Type could be registered as its own `SBTypeNameSpecifier`, but
LLDB name and regex specifiers match on the **type's own name** only. Visual
Studio applies a base class's visualizer to derived types (`Inheritable`,
default true), which no regex can express.

So the whole package registers exactly **one** summary and **one** synthetic
formatter, both using `lldb.eFormatterMatchCallback`. LLDB asks Python "does a
formatter apply to this `SBType`?" and `NatvisRegistry.match_sbtype` answers by
resolving the name, then walking direct base classes up to depth 8. Wildcards,
typedefs, whitespace variants, `Priority`, and inheritance are all resolved in
one place, in Python, where they can be expressed properly.

This is the single most load-bearing decision in the codebase. The first
implementation used per-type regex specifiers and failed the inheritance test
(`Square` deriving from a visualized `Shape`); switching to callbacks fixed it
and simplified registration to a fixed two entries.

### 2. Two-tier expression evaluation

`natvis/expr.py` evaluates each expression by the cheapest route that works:

- **Tier 1** — a recursive-descent parser for the restricted grammar that
  dominates real natvis files (`a.b->c[3]`, `*p`, `&x`, plus `chain OP intlit`
  for conditions). It walks `SBValue` children directly via
  `GetChildMemberWithName` / `Dereference` / `GetChildAtIndex`. No compiler.
- **Tier 0'** — `int_eval`, a Python AST evaluator over an allow-list of nodes,
  for pure integer/bool expressions. This is what keeps `<CustomListItems>`
  loops (which run O(N) times) from JIT-compiling on every iteration.
- **Tier 2** — `SBValue.EvaluateExpression`, which evaluates C++ with the
  object's members in scope. That is exactly the natvis semantic, so this is
  the correctness backstop for everything the fast paths decline.

The sample suite runs ~80% of evaluations on the fast paths. `natvis status`
reports the live split (`fast` / `int` / `slow` / `slow_fail`) — a useful first
stop when a visualizer feels slow.

Everything starts from `GetNonSyntheticValue()` so evaluation never recurses
into our own synthetic children.

### 3. Nested formatting delegates to LLDB

A `DisplayString` child value is rendered by asking for its **summary**, not by
manually recursing into its natvis entry. That way nested formatting flows
through LLDB's own machinery and picks up built-in formatters too. Our own
guard (`display.py`: thread-local depth cap of 8 plus a visited
`(addr, name, type)` set) covers self-referential structures.

### 4. `<Synthetic>` via an override map

LLDB has no concept of "a child with its own visualizer attached". A
`<Synthetic Name="X">` child is materialized at the *parent's* address with the
parent's type, and its nested `DisplayString`/`Expand` is stashed in
`registry.synthetic_overrides`, keyed `(load_addr, name, type_name)`.

That key alone can collide with a real variable of the same name and type at
the same address, so the lookup is gated on
`GetValueType() == eValueTypeConstResult` — values we materialize report
`ConstResult`, while frame variables and members report their own value types.
(Verified empirically against lldb-1703; see the session's probe.)

### 5. Bounded everything

Every loop that walks debuggee memory has a budget, because the data may be
corrupt and the code runs in a UI callback:

| Guard | Where | Bound |
|---|---|---|
| Children per node | `providers.py` | `REGISTRY.max_children` (1000), `MaxItemsPerView` |
| List/tree walk length | `_build_linked_list`, `_build_tree` | `cap * 64` steps, plus visited-address cycle guards |
| Interpreter loops | `interp.py` | 100k per loop **and** 500k global steps shared across nested loops |
| Intrinsic expansion | `expr.py` | 64 substitutions (recursive `<Intrinsic>` raises instead of growing the string forever) |
| Summary recursion | `display.py` | depth 8 + visited set |
| Directory scan | `registry.py` | depth 8, 5000 dirs, prunes `.git`/`node_modules`/`DerivedData`/build dirs |
| Negative `<Size>` | `providers.py` | read signed, clamped to ≥ 0 |

Exceptions are caught at every LLDB boundary (`natvis_summary`,
`update`, `get_child_at_index`, `_Entry.realize`, match callbacks) and degrade
to "no formatting" rather than propagating into the debugger.

### 6. Lenient parsing

Real natvis files in the wild are not schema-clean. The parser strips the XML
namespace by rewriting tags, tolerates a license comment before the `<?xml?>`
declaration (SPIRV-Cross ships one; strict XML rejects it), and logs-and-skips
unknown elements. A broken `<Type>` is dropped with a warning instead of
failing the file. Warnings surface in `natvis list -v`.

## Verification

`tests/run_tests.py` drives one `xcrun lldb -b --no-lldbinit` batch against
`tests/test_bin` and regex-asserts the transcript: **32 checks**, covering every
supported feature plus negatives (no `<error`, no tracebacks, no unresolved
markers). `tests/test.cpp` + `tests/sample.natvis` are written as one type per
feature, so they double as worked examples.

Real-world parse checks: `godot.natvis` (39 types), `Jolt.natvis` (20),
`D3D12MemAlloc.natvis` (4), `spirv_cross.natvis` (2) — all load with zero
warnings.

`tests/xcode-sample/` wraps the same `test.cpp` in a SwiftPM executable so the
sample can be debugged in Xcode's GUI (`xed tests/xcode-sample`).

### Defects found by review, after the suite was first green

An independent review pass over untested paths found eight real defects, all
fixed and now covered:

1. **Recursive `<Intrinsic>` hung the debugger.** The depth guard sat outside
   the substitution loop, which re-found the just-inserted call site forever,
   doubling the string each pass. Replaced with an in-loop budget.
2. **`int_eval` rejected every expression starting with `!`.** `!` → `" not "`
   left a leading space, and `ast.parse(" not x")` raises `unexpected indent`.
   So `Condition="!done"` — a very common loop condition — silently fell to the
   500 ms JIT path on every iteration. One-character fix (`src.strip()`).
3. **Negative `<Size>` fabricated ~1000 children** (read unsigned, so `-1`
   became huge). Now signed and clamped.
4. **Sequential env substitution corrupted casts** when a `<Variable>` name
   matched a type name. Now a single-pass alternation regex.
5. **`<Synthetic>` override key could capture the parent object** — fixed by
   the `ConstResult` gate above.
6. **List/tree walks were unbounded** when a `ValueNode` condition filtered
   everything (emission count never advanced). Added step budgets.
7. **Unparseable `<Exec>` silently no-oped**, spinning its loop to the cap. Now
   aborts the program.
8. **Exceptions could escape `get_child_at_index`** into LLDB. Now caught.

Two of these (1 and 2) were hang-class bugs in a debugger UI callback — the
kind that a green test suite says nothing about. The lesson worth keeping: for
this codebase, *tests prove features work; only review proves failures are
survivable.*

### Environment gotcha

If `run` hangs in a batch lldb session with no output, check
`DevToolsSecurity -status`. With developer mode disabled, `debugserver` waits
on a GUI authorization prompt that never appears in a headless run — this looks
exactly like a hang in your own code. `sudo DevToolsSecurity -enable` fixes it
(needs a real terminal for the password).

## Deliberate omissions

`<UIVisualizer>`, `<CustomVisualizer>`, `ModuleName`/`ModuleVersionMin` filters
and `<Version>` are parsed and ignored: they are Visual Studio IDE concepts
with no LLDB equivalent. See `TODO.md` for gaps that are worth closing.
