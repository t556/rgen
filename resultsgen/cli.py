"""Thin command line boundary, also used by the web job runner."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import tempfile

from .config import ConfigError, OutputError, config_dict, load_config, preset, save_config


def positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def nonnegative_integer(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return number


def port_number(value: str) -> int:
    number = positive_integer(value)
    if number > 65535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="resultsgen", description="Generate reproducible nightly JUnit archives.")
    commands = result.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="open the local web UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=port_number, default=8080)
    serve.add_argument("--configs-dir", type=Path, default=Path("configs"))
    generate = commands.add_parser("generate", help="write an archive and truth sidecar")
    generate.add_argument("config")
    generate.add_argument("--out", type=Path)
    generate.add_argument("--seed", type=nonnegative_integer)
    generate.add_argument("--max-runs", type=positive_integer)
    generate.add_argument("--force", action="store_true")
    generate.add_argument("--progress-file", type=Path)
    preview = commands.add_parser("preview", help="simulate the failure budget without writing XML")
    preview.add_argument("config")
    preview.add_argument("--json", action="store_true")
    preview.add_argument("--progress-file", type=Path)
    validate = commands.add_parser("validate", help="validate all configuration fields")
    validate.add_argument("config")
    new = commands.add_parser("new-config", help="save a preset")
    new.add_argument("--preset", choices=("default", "dev"), default="default")
    new.add_argument("out", type=Path)
    return result


def write_progress(path: Path | None, value: dict) -> None:
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")
        temporary.replace(path)


def preview_text(report: dict) -> str:
    stats = report["statistics"]
    budget = report["budget"]
    lines = [f"{report['runs']} runs; {len(report['outage_nights'])} outage nights",
             f"{stats['total_test_runs']:,} test-runs; {stats['total_missing']:,} missing",
             f"{stats['total_failures']:,} failures; rate {stats['failure_rate']:.4%}; target {budget['target_failure_rate']:.4%}"]
    for app in report["apps"]:
        row = stats["per_app"][app["name"]]
        lines.append(f"  {app['name']} ({app['personality']}): {row['total_failures']:,} failures, {row['failure_rate']:.4%}")
    lines.append("Failures by cause: " + ", ".join(f"{key}={value:,}" for key, value in stats["failures_by_cause"].items()))
    lines.append(f"Planned events: {len(report['events'])} (full identities available with --json)")
    for event in report["events"]:
        lines.append(f"  {event['type']} {event['app']} {event['start_date']} runs {event['start_run']}–{event['end_run']}: {len(event['targets'])} tests")
    if budget["warning"]:
        lines.append(budget["warning"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    for console in logging.getLogger().handlers:
        console.setLevel(logging.INFO)
    logger = logging.getLogger(__name__)
    progress_path = getattr(args, "progress_file", None)
    log_path = None
    handler = None
    try:
        if args.command == "serve":
            from .web.app import create_app
            create_app(configs_dir=args.configs_dir).run(host=args.host, port=args.port)
            return 0
        if args.command == "new-config":
            save_config(preset(args.preset), args.out)
            print(f"Saved {args.preset} configuration to {args.out}")
            return 0
        cfg = load_config(args.config)
        if args.command == "validate":
            print("Configuration is valid.")
            return 0
        if args.command == "generate":
            data = config_dict(cfg)
            if args.out is not None:
                data["output_dir"] = str(args.out)
            if args.seed is not None:
                data["seed"] = args.seed
            if args.force:
                data["overwrite"] = True
            cfg = load_config(data)
        if progress_path:
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            log_path = progress_path.parent / "engine.log"
        else:
            with tempfile.NamedTemporaryFile(prefix="resultsgen-", suffix=".log", delete=False) as handle:
                log_path = Path(handle.name)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logging.getLogger().addHandler(handler)

        def progress(done, total, current_run, elapsed):
            write_progress(progress_path, {"status": "running", "runs_done": done,
                           "runs_total": total, "current_run": current_run, "elapsed": elapsed})

        if args.command == "generate":
            from .generate import generate
            report = generate(cfg, max_runs=args.max_runs, progress=progress, log_path=log_path)
        else:
            from .preview import preview
            report = preview(cfg, progress=progress)
        write_progress(progress_path, {"status": "complete", "report": report})
        print(preview_text(report) if args.command == "preview" and not args.json
              else json.dumps(report, indent=2, default=str))
        return 0
    except ConfigError as exc:
        code = 2
        logger.error("%s", exc)
    except (OutputError, OSError) as exc:
        code = 3
        logger.error("%s", exc)
    except Exception:
        code = 1
        logger.exception("Unexpected error")
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()
    if log_path:
        logger.error("Log: %s", log_path)
    try:
        write_progress(progress_path, {"status": "failed", "exit_code": code,
                                      "log_path": str(log_path) if log_path else None})
    except OSError:
        logger.exception("Could not write job status")
    return code
