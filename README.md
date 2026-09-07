AI written tool to generate results. Junit xml files. Used for building and testing tools.


# resultsgen

resultsgen makes up nightly test results. It writes JUnit XML the way a CI
system would, one directory per night and one file per application, and next to
it a second directory holding the answers: which tests were supposed to pass,
which were supposed to fail, and why.

Point your importer, dashboard, or analysis tool at the XML. Then check what it
found against the answers.

resultsgen does not run any tests. It simulates their outcomes.

The default settings produce ten applications over 411 nights, roughly ten
million test results. The `dev` preset produces eleven nights of two
applications, small enough to regenerate while you wait. It plants an outage,
all four broken-file modes, a regression, flakes, and a removal. It does not
plant persistent failures, correlated failures, or additions.

For a walkthrough, see [GUIDE.md](GUIDE.md). For every configuration field and
its default, see [CONFIGURATION.md](CONFIGURATION.md).

## What it can simulate

- Regressions that start on a given night and last a given number of runs.
- Flaky tests, driven by a two-state Markov chain that steps once per run.
- Nights when the CI infrastructure fell over and produced nothing.
- Result files that are missing, empty, truncated between suites, or truncated
  in the middle of a tag.
- Tests that get added, and tests that get deleted and never come back.
- Tests that have never passed since the day they were written.

## Installing

Python 3.12 or newer.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Runtime dependencies are NumPy, Polars, and Flask. Development adds pytest.

## Two ways to drive it

From the shell:

```sh
$ python -m resultsgen new-config --preset dev configs/demo.json
$ python -m resultsgen generate configs/demo.json --out ./out
{
  "results_root": "/home/you/work/out/results-dev",
  "sidecar": "/home/you/work/out/results-dev.truth",
  "runs": 11,
  ...
}
```

Both paths come back absolute, whatever you passed to `--out`.

Or in a browser:

```sh
$ python -m resultsgen serve
```

Then open <http://127.0.0.1:8080>, load a preset, edit the settings, and click
Generate. The browser interface runs the same CLI commands as subprocesses and
saves named configurations to `configs/`.

The server has no authentication and runs one generate at a time, though
previews are not limited. Run it on localhost or a LAN you trust. Saved
configurations and generated archives live on disk and survive a restart. The
job list does not: it lives in memory, while each job's `jobs/<id>/` directory
of config, progress, and logs stays on disk and is never cleaned up.

## Commands

| Command | What it does |
| --- | --- |
| `new-config --preset {dev,default} PATH` | Write a starting configuration. |
| `validate PATH` | Check a configuration without running anything. |
| `preview PATH` | Run the full simulation and report statistics. Writes no files. |
| `generate PATH` | Run the simulation and write both directories. |
| `serve` | Start the browser interface. |

`generate` takes `--out PATH`, `--seed N`, `--max-runs N`, `--force`, and
`--progress-file PATH`. `preview` takes `--json` for the full report including
every event's resolved targets, and `--progress-file`.

Exit codes are 2 for an invalid configuration, 3 for a filesystem or output
error, and 1 for anything unexpected. A failed run prints the path to its
diagnostic log.

## What gets written

```text
out/
  results/                  # named by archive_name
    1/junit/atlas.xml
    1/junit/beacon.xml
    2/junit/...
  results.truth/
    manifest.json
    truth.parquet
    config.json
    generate.log
```

Run directories are numbered from 1 with no gaps. A night lost to an outage
does not get a number, so two consecutive numbers can be several days apart.

The date of a night is the **modification time of its run directory**, not of
the files inside it. Application files can finish writing after midnight.

Suites are flat and test names look like C++ identifiers. A `<testcase>` failed
if it contains a `<failure>` element. There are no counts, no timestamps, and no
`skipped` or `error` elements, which is deliberate: your tool has to derive
those rather than read them off. A test's identity across nights is
`(app, suite name, test name)`. The formatting follows the
[Testmo example](https://github.com/testmoapp/junitxml/blob/main/examples/junit-basic.xml),
vendored at `refs/junit-basic.xml`.

## The truth directory

| File | Holds |
| --- | --- |
| `manifest.json` | Resolved configuration and its SHA-256, the run schedule, personality assignments, every planted event with the exact suites and tests it hit, and aggregate statistics. |
| `truth.parquet` | One row per test per run. |
| `config.json` | The settings actually used, including CLI overrides. Feed it back to `generate` to reproduce the set. |
| `generate.log` | Diagnostics from the run. |

Parquet columns are `run`, `run_date`, `app`, `suite`, `test`, `outcome`,
`cause`, and `event_id`. Outcome is `pass`, `fail`, or `missing`. Passing rows
have a null cause, and `event_id` is null unless the row was caused by an
injected event.

Failure rate is `fail / (pass + fail)`. Missing rows are counted separately and
never enter that denominator.

Set `truth.include_missing_rows` to `false` to drop missing rows from both the
Parquet file and the reported statistics. Set `truth.write_parquet` to `false`
to keep the manifest and statistics without a Parquet file, though
`scripts/check_archive.py` then has nothing to compare against.

## How an outcome is decided

Causes are checked in this order, and the first one that applies wins:

```text
infrastructure → persistent → born-failing → regression → correlated → flaky → noise → pass
```

Whichever cause won is recorded in the `cause` column, which spells two of them
differently: infrastructure appears as `partial` or `truncated`, and
born-failing as `born_failing`. A flaky test steps its Markov state on every
executed run, including runs where a higher-priority cause hid the result, so
its behaviour doesn't depend on what else went wrong that night.

Born-failing tests come only from `addition` events, and there are none by
default. Removals are permanent.

## Injected events

| Type | Effect |
| --- | --- |
| `outage` | The night produces nothing and consumes no run number. |
| `partial` | One application's file is missing, empty, or truncated. |
| `persistent` | Targeted tests fail from this night on, for `duration` runs, or forever if you omit it. |
| `regression` | Targeted tests fail for a bounded window. |
| `correlated` | A group of tests fails together. |
| `flaky` | Targeted tests get Markov flake behaviour. |
| `removal` | Targeted tests disappear permanently. |
| `addition` | New tests appear, optionally already failing. |

```json
[
  {"type": "outage", "date": "2026-08-29"},

  {"type": "partial", "date": "2026-08-30", "app": "atlas",
   "parameters": {"mode": "truncated_wellformed", "keep_suites": 3}},

  {"type": "regression", "date": "2026-09-01", "app": "atlas",
   "targets": {"test_ids": [0, 1, 2]}, "parameters": {"duration": 3}},

  {"type": "addition", "date": "2026-09-02", "app": "beacon",
   "parameters": {"count": 5, "born_failing": true, "duration": 2}}
]
```

Rules that apply to all of them:

- Every type except `outage` needs an `app`.
- Use `date`, or `start_date` and `end_date` together. Dates have to fall inside
  the configured calendar.
- Events are scheduled by date, never by run number. A date that lands on an
  outage night takes effect on the next night that ran.
- An event dated after the last executed run stays in the manifest with
  `parameters.unfired` set to `true`.
- `targets.test_ids` and `targets.suite_ids` are zero-based and scoped to the
  application. A `count` takes a seeded random subset: of the whole application
  when you name no ids, or of the ids you did name. Omit targets and the event
  uses its own default selection. Manifest entries spell out the resulting suite
  and test names.
- `parameters.duration` counts executed runs, not calendar days.

Flaky events take `p_calm_to_flare`, `p_flare_to_calm`, `p_calm`, and `p_flare`.
Anything you leave out is drawn from the configured ranges. Personalities and
their assignment weights are editable in the configuration.

## Partial and malformed files

`partial.mode_weights` maps `absent`, `empty`, `truncated_wellformed`, and
`truncated_malformed` to probabilities that sum to 1. To stop generating
malformed XML, set the last one to 0 and redistribute, for example
`0.8, 0.1, 0.1, 0.0`.

`absent` writes no file. `empty` writes a well-formed document containing no
suites, not a zero-length file, so don't detect it by size.

Malformed files are cut mid-tag, so reading one requires an incremental parser.
Only suites that were fully closed count as observed. The cut suite and every
suite after it are `missing` in truth. `scripts/check_archive.py` shows how to
handle this.

Zeroing the weights does not disable partial events you injected explicitly.
Remove those from `events`.

## Reproducibility

The same configuration, seed, and dependency versions produce byte-identical XML
and identical modification times.

Timestamps are applied after every file is written, in order: files, then
`junit/`, then the run directory, all set to integer epoch seconds. Only mtime
and atime are set.

Four things will break reproducibility:

- `timezone: "local"` reads the host's timezone rules. Use an explicit IANA name
  such as `UTC` if you compare archives across machines.
- `--max-runs` is not recorded in `config.json`. Pass it again by hand.
- Dependency upgrades.
- Manifest generation times and diagnostic logs differ on every run and are
  expected to.

Linux birth time cannot be spoofed portably, so `btime` on generated files is
whenever you ran the generator. If your consumer reads it, running under
`libfaketime` is the usual escape hatch, but verify it actually takes effect on
your filesystem.

## Writing over an existing archive

Both directory trees are built inside one temporary directory next to the
destination, then renamed into place at the end. A failed run deletes that
directory. If the second rename fails, both are rolled back.

Two sibling directories cannot be made visible by a single rename. The truth
directory goes first, so there is a brief window where it exists and the results
directory it describes does not. Anything watching the output path needs to
tolerate that.

`generate` refuses to write to a destination that already exists. `--force`, or
`overwrite: true` in the configuration, replaces the results directory **and**
its truth directory.

## Where it will disappoint you

- Previewing does the whole simulation, so previewing a default-scale
  configuration costs about as much thinking as generating one. Only the writing
  is skipped.
- The failure-rate budget warning is a comparison against a number you supplied.
  It does not tune anything.
- Application files are written serially per run, so wall-clock generation time
  scales with total test count rather than with cores.

## Tests

```sh
$ python -m pytest -q
```

They use small configurations and the dev preset, and never generate a
default-scale archive. They cover parse-back against truth, byte and timestamp
determinism, cause precedence, Markov behaviour, XML escaping, all four partial
modes, rollback, and one end-to-end browser job.

`make test`, `make serve`, `make preview-dev`, and `make generate-dev` wrap the
common invocations and use `.venv/bin/python` by default.

To measure generation on your own hardware:

```sh
$ python -m resultsgen preview resultsgen/presets/default.json
$ /usr/bin/time -v python -m resultsgen generate resultsgen/presets/default.json
$ python scripts/check_archive.py out/results --sample 20
```
