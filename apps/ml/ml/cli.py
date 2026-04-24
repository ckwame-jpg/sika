from __future__ import annotations

import argparse
import json
from pathlib import Path

from ml.training import train_and_package


def _train(args: argparse.Namespace) -> int:
    result = train_and_package(
        database_url=args.database_url,
        artifact_root=args.artifact_root,
        manifest_out=None if args.dry_run else args.manifest_out,
        serve_family_key=args.serve_family_key,
        feature_set_version=args.feature_set_version,
        model_version=args.model_version,
        dry_run=args.dry_run,
    )
    print(
        json.dumps(
            {
                "model_name": result.model_name,
                "artifact_dir": str(result.artifact_dir),
                "manifest_path": str(result.manifest_path) if result.manifest_path else None,
                "training_rows": result.metrics["training_rows"],
                "winner": result.metrics["winner"],
                "player_group_brier": result.metrics["metrics"][result.metrics["winner"]]["player_group"]["brier"],
                "dry_run": args.dry_run,
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ml.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)
    train = subparsers.add_parser("train", help="Train and package the global v1 model.")
    train.add_argument("--database-url", default=None)
    train.add_argument("--artifact-root", default="artifacts")
    train.add_argument("--manifest-out", default=str(Path("manifests") / "current.json"))
    train.add_argument("--serve-family-key", default="mlb_props")
    train.add_argument("--feature-set-version", default="public-feature-set-v1")
    train.add_argument("--model-version", default=None)
    train.add_argument("--dry-run", action="store_true")
    train.set_defaults(func=_train)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
