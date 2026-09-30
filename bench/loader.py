"""Load and validate benchmark manifests.

Validation is two-layered:

1. JSON Schema (`manifest.schema.json`, draft 2020-12) when `jsonschema` is
   importable — it ships with the `mcp` SDK this project already depends on.
2. Standard-library semantic checks that the schema cannot express (unique IDs
   across manifests, known scorers/fixture boundaries, well-formed windows).
   These always run, so a missing `jsonschema` never silently weakens CI.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BENCH_DIR / "manifest.schema.json"
CASES_DIR = BENCH_DIR / "cases"

ALLOWED_MODES = {"deterministic", "live"}
REQUIRED_CASE_FIELDS = {"id", "suite", "task", "mode", "input", "expected"}
SCORING_METHODS = {"subset", "metadata_fields", "provenance", "temporal_window", "fault"}
FIXTURE_BOUNDARIES = {"native", "native_malformed", "ytdlp", "watch_page", "data_api", "cache"}


class ManifestError(ValueError):
    """A manifest failed schema or semantic validation."""


@dataclass(frozen=True)
class LoadedManifest:
    path: Path | None
    data: dict[str, Any]
    sha256: str

    @property
    def benchmark_version(self) -> str:
        return self.data["benchmark_version"]

    def describe(self) -> dict[str, Any]:
        return {
            "path": _display_path(self.path),
            "benchmark_version": self.benchmark_version,
            "sha256": self.sha256,
            "cases": len(self.data.get("cases", [])),
        }


def _display_path(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(BENCH_DIR.parent).as_posix()
    except ValueError:
        return path.as_posix()


def _schema_validate(manifest: dict[str, Any]) -> None:
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema ships with mcp
        return
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(manifest), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.absolute_path) or "<root>"
        raise ManifestError(f"schema violation at {where}: {first.message}")


def validate_manifest(manifest: dict[str, Any], *, use_schema: bool = True) -> None:
    """Validate one manifest (schema + semantics). Raises ManifestError."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be an object")
    if not isinstance(manifest.get("benchmark_version"), str):
        raise ManifestError("manifest benchmark_version must be a string")
    adapter = manifest.get("adapter")
    if not isinstance(adapter, str) or not adapter:
        raise ManifestError("manifest adapter must be a non-empty string")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ManifestError("manifest cases must be a non-empty list")

    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise ManifestError(f"case {index} must be an object")
        missing = REQUIRED_CASE_FIELDS - set(case)
        if missing:
            raise ManifestError(f"case {index} missing fields: {sorted(missing)}")
        case_id = case["id"]
        if not isinstance(case_id, str) or not case_id:
            raise ManifestError(f"case {index} id must be a non-empty string")
        if case_id in seen:
            raise ManifestError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        if case["mode"] not in ALLOWED_MODES:
            raise ManifestError(f"case {case_id} has unsupported mode {case['mode']!r}")
        if not isinstance(case["input"], dict) or not isinstance(case["expected"], dict):
            raise ManifestError(f"case {case_id} input/expected must be objects")
        _validate_semantics(case)

    if use_schema:
        _schema_validate(manifest)


def _validate_semantics(case: dict[str, Any]) -> None:
    case_id = case["id"]
    scoring = case.get("scoring") or {}
    method = scoring.get("method", "subset")
    if method not in SCORING_METHODS:
        raise ManifestError(f"case {case_id} uses unknown scoring method {method!r}")
    if method == "temporal_window":
        windows = (scoring.get("params") or {}).get("windows")
        if not windows:
            raise ManifestError(f"case {case_id}: temporal_window needs params.windows")
        for window in windows:
            if (
                not isinstance(window, list)
                or len(window) != 2
                or not all(isinstance(v, (int, float)) for v in window)
                or window[0] > window[1]
            ):
                raise ManifestError(f"case {case_id}: malformed evidence window {window!r}")
    fixture = case.get("fixture")
    if fixture is not None:
        if case["mode"] != "deterministic":
            raise ManifestError(f"case {case_id}: fixtures are only valid for deterministic cases")
        unknown = set(fixture) - FIXTURE_BOUNDARIES
        if unknown:
            raise ManifestError(f"case {case_id}: unknown fixture boundaries {sorted(unknown)}")


def load_manifest(path: Path) -> LoadedManifest:
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"{path}: not valid UTF-8 JSON: {exc}") from exc
    try:
        validate_manifest(data)
    except ManifestError as exc:
        raise ManifestError(f"{_display_path(path)}: {exc}") from exc
    return LoadedManifest(path=path, data=data, sha256=hashlib.sha256(raw).hexdigest())


def from_dict(data: dict[str, Any]) -> LoadedManifest:
    validate_manifest(data)
    raw = json.dumps(data, sort_keys=True).encode("utf-8")
    return LoadedManifest(path=None, data=data, sha256=hashlib.sha256(raw).hexdigest())


def load_manifests(paths: list[Path] | None = None) -> list[LoadedManifest]:
    """Load explicit manifest paths, or every `bench/cases/*.json` (sorted)."""
    targets = paths or sorted(CASES_DIR.glob("*.json"))
    manifests = [load_manifest(p) for p in targets]
    seen: dict[str, str] = {}
    for manifest in manifests:
        for case in manifest.data["cases"]:
            origin = _display_path(manifest.path) or "<inline>"
            if case["id"] in seen:
                raise ManifestError(
                    f"duplicate case id {case['id']!r} in {origin} and {seen[case['id']]}"
                )
            seen[case["id"]] = origin
    return manifests


def revision_id(manifests: list[LoadedManifest]) -> str:
    """Stable identity for the exact set of manifest bytes that ran."""
    digest = hashlib.sha256()
    for manifest in sorted(manifests, key=lambda m: m.sha256):
        digest.update(manifest.sha256.encode("ascii"))
    return digest.hexdigest()[:16]
