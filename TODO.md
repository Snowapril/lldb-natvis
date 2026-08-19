# TODO

Known gaps and follow-ups, roughly in priority order. Nothing here blocks
day-to-day use; the 32-check suite is green and real-world natvis files load
clean. See `IMPLEMENTATION.md` for how the pieces fit.

## Verification gaps

- [ ] **Confirm the Xcode Variables view end-to-end.** Everything so far is
      verified through `frame variable` in batch lldb. The GUI path (Variables
      pane, hover popovers, `po`) is believed to work — it uses the same
      formatter machinery — but has not been visually confirmed. Repro:
      `xed tests/xcode-sample`, ⌘R, inspect `vec` / `map` / `table`.
      Watch for eager `num_children` cost on large containers.
- [ ] **Cover `<ArrayItems>` `Direction="Backward"` and `<LowerBound>`.**
      Implemented (`_unflatten` treats Forward/Backward as row-/column-major
      ordering, matching the natvis spec) but no test exercises either.
- [ ] **Cover `,s32` / `,s8b` string specifiers.** Implemented, untested.
- [ ] Test against a natvis file that uses MSVC-STL-style `<TreeItems>` with a
      sentinel `ValueNode Condition` (e.g. `std::map`), which is the shape the
      traversal was designed for but only synthetically tested.

## Feature gaps

- [ ] **Fast-path gaps that still reach the compiler** (each costs one ~5 s
      stall per process, once): string literals in expressions,
      `reinterpret_cast`/functional casts, and `sizeof`. 5 of 561 real-world
      expressions. Use `natvis verbose on` to see which expressions in your own
      natvis file fall through and why.

- [ ] **Top-level `<Intrinsic>`.** Only intrinsics declared inside a `<Type>`
      are collected (`parser.py:_parse_type`); ones directly under
      `<AutoVisualizer>` are logged and skipped. Fix: collect them in
      `parse_file` and merge into every Type's intrinsic list at resolve time.
- [ ] **Type-level `IncludeView` / `ExcludeView`.** `ViewConstraint` is honored
      on `DisplayString` / `StringView` / Expand nodes, but `NatvisType` itself
      does not carry the attributes, so a whole `<Type>` cannot be view-scoped.
- [ ] **`<Exec>` left-hand sides are restricted to bare variable names.**
      Member/index targets (`node->count = 0`, `arr[i] = x`) abort the
      CustomListItems program instead of assigning. Real natvis rarely does
      this, but it is a real divergence.
- [ ] **`,view(x)` does not propagate through LLDB.** A view name applies to
      our own nested lookups only; once rendering hands off to LLDB's summary
      machinery the view is lost. Would need our own recursive renderer to fix
      properly.
- [ ] **`$Tn` binding for a trailing `*`.** `Foo<*>` against `Foo<int,float>`
      binds `$T1=int, $T2=float` (each remaining arg positionally). This
      matches observed VS behavior but is not spec-verified; revisit if a real
      file disagrees.

## Robustness / performance

- [ ] **Per-value resolve caching.** `resolve_cache` is keyed by type name,
      which is right, but `resolve_for_value` still does reference-stripping
      and base-class walking on every call. Profile before optimizing —
      `natvis status` shows whether evaluation or dispatch dominates.
- [ ] **`natvis reload --dev`** to `importlib.reload` the package modules, so
      editing the Python source doesn't require restarting lldb. Currently
      `reload` only re-parses `.natvis` files.
- [ ] Consider a wall-clock budget (not just step counts) for
      `<CustomListItems>`, since a single Tier-2 evaluation can take 500 ms and
      500k steps of *those* would be far too slow. In practice the int fast
      path prevents this; a timer would make it structural.

## Packaging

- [ ] **Add a LICENSE** (MIT is the obvious fit for a debugger utility).
- [ ] Consider CI (GitHub Actions, macOS runner) running `tests/build.sh` +
      `tests/run_tests.py`. Note the runner needs developer mode enabled for
      `debugserver` to attach — see the environment gotcha in
      `IMPLEMENTATION.md`.
- [ ] README could use a screenshot of the Xcode Variables view before/after,
      which is the fastest way to communicate what this does.
