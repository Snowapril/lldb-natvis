# lldb-natvis

Visual Studio `.natvis` visualizers for LLDB on macOS. Load your project's
natvis file once and `frame variable` / Xcode's Variables view render types
almost exactly like the Visual Studio watch window — DisplayString summaries,
expanded synthetic children, the works.

Pure Python, stdlib only, driven entirely by LLDB's script bridge (verified on
Apple lldb-1703 / Xcode 17 toolchain, embedded Python 3.9).

## Install

Clone this repo, then add one line to `~/.lldbinit` (used by both CLI lldb and
Xcode):

```
command script import /path/to/lldb-natvis/lldb_natvis.py
```

> Note: if a `~/.lldbinit-Xcode` exists, Xcode reads **it instead of**
> `~/.lldbinit` — put the line in both, or delete the Xcode-specific one.
> Likewise, a scheme with an "LLDB Init File" set (Edit Scheme → Run →
> Options) replaces `~/.lldbinit` entirely, so include the import line there
> too.

On import, every `*.natvis` **under** the current working directory (recursive,
skipping `.git`/build dirs, bounded to 5000 dirs / depth 8), under
`$NATVIS_PATH` entries (colon-separated), and under any existing target
executable's directory is loaded automatically. Xcode usually launches with `/`
as cwd (which is never scanned), so for Xcode either set `NATVIS_PATH` or add
an explicit line:

```
command script import /path/to/lldb-natvis/lldb_natvis.py
natvis load /path/to/YourProject.natvis
```

### Per-project setup in Xcode (scheme "LLDB Init File")

To scope the setup to one project — and share it with teammates through the
scheme — use a project-local lldbinit instead of the global one:

1. Create `debug.lldbinit` at your project root:

   ```
   command script import /path/to/lldb-natvis/lldb_natvis.py
   natvis load /path/to/YourProject
   ```

   `natvis load` on a directory scans it recursively, so pointing it at the
   project root picks up every `.natvis` in the tree.

2. In Xcode: **Product → Scheme → Edit Scheme → Run → Options → LLDB Init
   File**, and set it to the file — build variables work, e.g.
   `$(SRCROOT)/debug.lldbinit`.

3. Debug as usual: the Variables view and console are natvis-formatted from
   the first breakpoint. Mark the scheme as *Shared* (Manage Schemes) to check
   it into the repo for the whole team.

> When a scheme sets an LLDB Init File, lldb reads **that file instead of**
> `~/.lldbinit`, which is why step 1 includes the `command script import` line
> (and any other global lldbinit settings you rely on).

## The `natvis` command

| Command | Effect |
|---|---|
| `natvis load <file-or-dir>` | load a `.natvis` file, or every `*.natvis` under a directory (recursive) |
| `natvis reload` | re-parse and re-register everything (picks up file edits) |
| `natvis list [-v]` | loaded files, type counts, parse warnings |
| `natvis unload [<file>\|--all]` | remove visualizers |
| `natvis stringview <var>` | print a type's `<StringView>` content in full |
| `natvis verbose on\|off` | debug logging for visualizer authors |
| `natvis status` | cache and fast/slow evaluation-path statistics |

## Supported natvis features

- `<DisplayString>` with `{expr}` interpolation, `{{`/`}}` escapes, `Condition`
  / `Optional` attributes, first-match-wins alternatives
- Format specifiers: `,d ,x ,X ,o ,b ,c ,s ,sb ,s8 ,su ,sub ,s32 ,en ,e ,f ,g
  ,na ,nd ,[size-expr] ,view(name)` (unknown ones are ignored gracefully)
- `<StringView>`, `<Intrinsic>` (with `<Parameter>`), `$T1..$Tn` template
  wildcards (`Name="MyVec&lt;*&gt;"`), `$i`, `<AlternativeType>`, `Priority`,
  `Inheritable` (applies to derived classes via base-class matching),
  `IncludeView`/`ExcludeView`
- `<Expand>` children: `<Item>`, `<ArrayItems>` (incl. multi-dimensional
  `Rank`/`LowerBound`/`Direction`), `<IndexListItems>`, `<LinkedListItems>`,
  `<TreeItems>` (in-order traversal with cycle guard and sentinel conditions),
  `<ExpandedItem>` (incl. base-class splicing), `<Synthetic>`,
  `<CustomListItems>` (full `Variable`/`Size`/`Loop`/`Break`/`If`/`Elseif`/
  `Else`/`Exec`/`Item` interpreter with `MaxItemsPerView`)

Implementation notes:

- Expressions are evaluated with a two-tier engine: simple member/index/deref
  chains walk `SBValue` children directly (no compiler); everything else goes
  through `SBValue::EvaluateExpression`, which evaluates C++ in the object's
  context — the exact natvis semantic. `natvis status` shows the hit rates.
- Matching is done by a formatter *callback*, so wildcards, typedefs,
  whitespace variants and inheritance all resolve in Python — one natvis entry
  for a base class formats every derived type, as in Visual Studio.
- Children are capped at 1000 per node (and `MaxItemsPerView`); traversals
  carry visited-set cycle guards, so corrupt lists/trees can't hang the
  debugger.

Known gaps: `<UIVisualizer>`, `<CustomVisualizer>`, `ModuleName` filters and
`<Version>` are parsed but ignored (they're VS-IDE concepts).

## Testing

```bash
cd tests
./build.sh              # clang++ -g -O0 test.cpp -o test_bin
python3 run_tests.py    # 28 end-to-end checks through real lldb
```

`tests/sample.natvis` + `tests/test.cpp` exercise every supported feature and
serve as working examples. Real-world sanity: `godot.natvis` (39 types),
`spirv_cross.natvis`, `D3D12MemAlloc.natvis` all load with zero warnings.

### Xcode verification checklist

1. Add the `command script import` line to `~/.lldbinit` (and remove stale
   imports that error out — see the note above).
2. Debug any target; in the console run `natvis load <your.natvis>`
   (or set `NATVIS_PATH`).
3. The Variables view should now show natvis summaries and expanded children;
   `frame variable <var>` in the console shows the same.
4. Re-run `natvis reload` after editing the `.natvis` file — no restart needed.
