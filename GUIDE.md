# Using resultsgen

This guide walks through making a set of fake test results, looking at what came
out, and using it to check whether your own tool reads results correctly. It
assumes resultsgen is [installed](README.md#installing) and that you're
comfortable at a shell.

The reference material lives in [README.md](README.md): the output layout, the
truth columns, the event types, and the flags. This guide is the part where you
actually run something.

- [Your first set](#your-first-set)
- [The same thing in a browser](#the-same-thing-in-a-browser)
- [Looking at what came out](#looking-at-what-came-out)
- [Reading the archive from your own tool](#reading-the-archive-from-your-own-tool)
- [Checking your tool against the truth](#checking-your-tool-against-the-truth)
- [Changing the scenario](#changing-the-scenario)
- [Making the set bigger](#making-the-set-bigger)
- [Reproducing a set later](#reproducing-a-set-later)
- [When something goes wrong](#when-something-goes-wrong)

## Your first set

Start with the `dev` preset. It's two applications over twelve calendar nights,
one of which is an outage, so eleven nights actually produce results. It packs a
regression, some flaky tests, a few permanent removals, and every kind of broken
file into a set you can regenerate in seconds.

Make a configuration from it:

```sh
$ python -m resultsgen new-config --preset dev configs/demo.json
```

That's an ordinary JSON file and you can open it now if you're curious. It sets
`archive_name` to `results-dev` and `timezone` to `UTC`.

Check that it's coherent before running anything:

```sh
$ python -m resultsgen validate configs/demo.json
Configuration is valid.
```

Now simulate it without writing anything:

```sh
$ python -m resultsgen preview configs/demo.json
11 runs; 1 outage nights
7,912 test-runs; 1,880 missing
12 failures; rate 0.1989%; target 0.2500%
  atlas (legacy): 1 failures, 0.0212%
  beacon (typical): 11 failures, 0.8333%
...
```

Eleven runs and one outage night is what an unedited dev preset should say. If
you want the whole report, including which specific tests each event landed on,
ask for JSON:

```sh
$ python -m resultsgen preview configs/demo.json --json > demo-preview.json
```

Preview runs the same simulation `generate` does, using the same seed. It just
skips writing the XML and Parquet. The statistics it prints will match what you
get when you generate for real.

So generate for real:

```sh
$ python -m resultsgen generate configs/demo.json --out ./out
{
  "results_root": "/home/you/work/out/results-dev",
  "sidecar": "/home/you/work/out/results-dev.truth",
  "runs": 11,
  ...
}
```

Relative paths like `./out` resolve from wherever you started the command, not
from wherever the configuration file happens to live. That trips people up. The
report echoes them back absolute.

If you'd rather only write the first few nights:

```sh
$ python -m resultsgen generate configs/demo.json --out ./out-trial --max-runs 3
```

`--max-runs` truncates the output, not the configuration. Events scheduled for
later nights never fire, but the manifest still lists them with their original
run windows and records nothing to say they were cut off. Only its `runs` count
tells you how far the archive actually goes.

## The same thing in a browser

If you'd rather click:

```sh
$ python -m resultsgen serve
```

Open <http://127.0.0.1:8080> and leave the terminal running.

Click **Load dev preset (2 apps)**. You'll get the same twelve nights and two
applications as above. The preset's dates are fixed, so loading it does not move
the calendar to today.

In the **Archive** section, fill in:

| Field | Try | What it controls |
| --- | --- | --- |
| Config name | `demo` | Saves to `configs/demo.json`. Nothing else. |
| Output directory | `./out` | Where both output folders go. |
| Archive name | `results-dev` | Names `out/results-dev` and `out/results-dev.truth`. |
| Seed | `20260907` | Every random choice in the simulation. |
| Timezone (local or IANA) | `UTC` | Keeps dates independent of the host clock. |

Config names take letters, digits, underscores, and hyphens, have to start with
a letter or digit, and cap out at 64 characters. Saving over an existing name
replaces it. Note that changing the config name does not change where results
get written; that's the archive name's job.

Leave **Replace an existing archive** unchecked the first time.

Then **Validate**, **Save config**, **Preview**, and finally **Generate
archive**. Wait for **Complete**, and note the two paths in the report.

The files land on whatever machine is running the server. The page shows you
where they are; it doesn't hand you a zip.

## Looking at what came out

```text
out/
  results-dev/
    1/
      junit/
        atlas.xml
        beacon.xml
    2/junit/...
    ...
    11/junit/...
  results-dev.truth/
    manifest.json
    truth.parquet
    config.json
    generate.log
```

Four `atlas.xml` files, in runs 3 through 6, are missing or cut off. That's the
preset exercising all four broken-file modes, not a bug.

There's a checker in the repo that reads the archive back and compares it
against truth:

```sh
$ python scripts/check_archive.py out/results-dev --sample 20
{
  "sampled_runs": 11,
  "test_rows": 7912,
  "parsed_testcases": 6032,
  "malformed_files": 1
}
```

For the dev set it checks all eleven runs, since there are fewer than twenty,
and the one malformed file is expected. On a larger set the same command checks
a deterministic sample of twenty runs. It asserts as it goes, so a disagreement
with truth raises rather than printing a diff.

It's also worth reading `scripts/check_archive.py` itself. It's short, and it's
a working example of the incremental-parsing behaviour your own tool will need.

## Reading the archive from your own tool

Point it at `out/results-dev` and expect this shape:

```text
<results-root>/<run-number>/junit/<app-name>.xml
```

Five things to get right:

**Sort run directories numerically.** `10` and `11` come after `9`, not after
`1`.

**Take the night's date from the run directory's mtime**, interpreted in the
configured timezone. Not from the files inside, which can finish writing after
midnight. Outages leave gaps in the dates but not in the numbering.

**A testcase failed if it contains a `<failure>` element.** Anything else you
can see, passed. There are no summary counts and no timestamps in the XML to
lean on.

**Identify a test by `(app, suite name, test name)`.** Add the run number when
you mean one specific nightly result.

**Keep "missing" separate from "passed."** An absent file, a file with no suites
in it, and the tail of a truncated file are all absences of information, not
passes. The `empty` mode writes a well-formed document containing no suites
rather than a zero-length file, so don't detect it by size. With malformed XML,
only fully closed suites count as observed; the cut suite and everything after
it are missing.

When you copy the archive somewhere else, preserve directory times as well as
file times, or you'll lose the dates:

```sh
$ rsync -a out/results-dev out/results-dev.truth user@analysis-host:/path/to/datasets/
```

Keep the two folders together. `manifest.json` also records each run's expected
date and timestamps, so you can verify a transferred set against it.

## Checking your tool against the truth

The point of the truth folder is that you run your analysis on the XML first,
then find out how you did.

A quick look at what's in there:

```sh
$ python - <<'PY'
import polars as pl

truth = pl.scan_parquet("out/results-dev.truth/truth.parquet")
print(truth.group_by("outcome").len().sort("outcome").collect())
print(
    truth.filter(pl.col("outcome") == "fail")
    .group_by(["app", "cause"])
    .len()
    .sort(["app", "cause"])
    .collect()
)
PY
```

The comparison itself:

1. Parse the XML with your importer and record what it detected.
2. Join to truth on `(run, app, suite, test)`.
3. Check pass/fail agreement. Handle missing rows as their own category rather
   than folding them into failures.
4. Compute failure rate as `fail / (pass + fail)`, leaving missing rows out.
5. If your tool claims to find regressions or flakes, compare its claims against
   the target identities and run windows recorded in `manifest.json`.

That last one is the interesting test. The manifest knows exactly which tests
were meant to look flaky and on which nights, so you can score detection rather
than just parsing.

## Changing the scenario

Edit `configs/demo.json`, or load it in the browser, then validate, preview, and
generate again.

| You want | Change |
| --- | --- |
| A different random scenario, same shape | `seed`, or `generate --seed 12345` |
| A different period | `calendar.start_date` and `calendar.end_date`, and move your event dates to match |
| Weekdays only | `calendar.days_of_week` to `[0, 1, 2, 3, 4]` |
| Bigger or smaller suites | Each app's `test_count` and `suite_count` |
| More or fewer applications | **Add application** and **Remove** in the browser, or edit the array directly |
| More or fewer failures | Failure-model rates, personalities, or the events themselves. Preview to see the effect |
| To keep the old set around | A different `archive_name` or `output_dir` |

Removing an application in the browser also removes any events that referenced
it. At least one application has to remain.

### Filling the applications table quickly

Each numeric column heading in the browser's Applications table has a **▾** menu
holding a minimum and a maximum, a switch for whether **Randomize** touches that
column, and a **Randomize column** button that redraws the whole column. A row's
**Randomize** button draws that one row; **Randomize all applications** does the
lot.

Duration and partial rate start switched off so they keep inheriting from the
global settings until you deliberately include them. Drawn values get corrected
to stay legal: suite count never exceeds test count, and the duration pair comes
out ordered.

Those ranges live in your browser's local storage. They never become part of a
configuration, and they need JavaScript. Without it the table behaves the way it
always did.

### Planting a regression you can hunt for

Append this to the `events` array, or paste it into **Injected events (JSON)** in
the browser:

```json
{
  "type": "regression",
  "date": "2026-09-05",
  "app": "beacon",
  "targets": {"test_ids": [10, 11, 12]},
  "parameters": {"duration": 3}
}
```

Three more `beacon` tests start failing on September 5, which is run 9, and keep
failing for three executed runs. The preset already plants one regression on
`beacon` for September 3, so the manifest will show both. Test IDs are
zero-based within the application; preview JSON and the manifest will tell you
which suite and test names they resolved to.

A higher-priority cause can steal a night from an event like this: if the
application is having an infrastructure night, that wins, and truth records it
accordingly. See [the precedence order](README.md#how-an-outcome-is-decided).
The dev preset only breaks `atlas` files, so this particular event keeps all
three of its nights. To see the opposite, target `atlas` test IDs 260, 261 and
262 on September 1: runs 5 and 6 truncate those suites away, so truth records
them as `missing` with cause `truncated`, and the regression only surfaces on
run 7.

Keep event dates inside the calendar, and keep targets valid if you rename
applications or shrink test counts. Validation will catch you if you don't.

### Turning off the broken files

To get complete, well-formed XML everywhere:

1. Set `partial.rate` to `0`.
2. Set any per-app `partial_rate` overrides to `0` or `null`.
3. Delete every `{"type": "partial"}` entry from `events`.
4. Validate, preview, generate to a fresh destination.

Step 3 is the one people forget. Zeroing the rates has no effect on partial
events you asked for by hand.

If you only want to stop the *malformed* files and are happy keeping absent and
truncated-but-parseable ones, set `partial.mode_weights.truncated_malformed` to
`0`, redistribute the other three weights so they still sum to `1`, and change
or remove any injected `truncated_malformed` events.

### Editing the raw JSON in the browser

Expand **Advanced: full JSON configuration** and tick **Use full JSON**. While
that's on, the JSON is the configuration and your edits in the grouped form
above are ignored. Untick it to go back.

## Making the set bigger

When you want the real thing, roughly ten million results across ten
applications and 411 nights:

```sh
$ python -m resultsgen new-config --preset default configs/large.json
```

Edit it, set an explicit timezone, then:

```sh
$ python -m resultsgen validate configs/large.json
$ python -m resultsgen preview configs/large.json
$ python -m resultsgen generate configs/large.json --out ./out-large
```

Preview runs the full simulation, so a large preview is not a cheap operation
either. It skips the writing, not the thinking.

## Reproducing a set later

Every generated set carries the configuration that made it. Point `generate`
straight at it:

```sh
$ python -m resultsgen generate out/results-dev.truth/config.json --out ./out-reproduced
```

To get the same bytes and the same timestamps you need the same configuration,
the same seed, the same dependency versions, and the same timezone rules. If the
original used `--max-runs`, pass it again, because that flag isn't recorded in
`config.json`. Manifest generation times and the diagnostic log will differ, and
that's fine.

## When something goes wrong

| What you see | What to do |
| --- | --- |
| `No module named resultsgen` | Activate `.venv`. If that doesn't do it, reinstall with the command in the README. |
| Port 8080 already in use | `python -m resultsgen serve --port 8081` |
| Validation errors | Fix all of them, then validate again. Usual suspects: event dates outside the calendar, app names that no longer exist, test IDs past the end, probabilities outside 0–1. |
| `Destination already exists` | Pick another archive name or output directory, or pass `--force` if you really mean to replace both folders. |
| XML missing, empty, or truncated | Check the manifest for planned partial events. In the dev preset this is expected. |
| Failure rate isn't what you asked for | The budget target is a number to compare against, not a knob. Look at the preview's cause breakdown and change the actual rates or events. |
| `truth.parquet` isn't there | `truth.write_parquet` is off. Turn it on and regenerate if you want row-level comparison or the checker. |
| A job just failed | Read the browser's **Job log**, or the diagnostic log path the CLI printed. Exit code 2 is a bad configuration, 3 is a filesystem problem, 1 is a bug. |