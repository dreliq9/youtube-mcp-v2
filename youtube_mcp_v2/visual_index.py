"""Immutable timestamped frame index for cross-modal corpus retrieval.

Ordinary frame cache files are acquisition artifacts and may be rebuilt. Visual
indexes therefore snapshot every indexed frame into a content-addressed artifact
store before embedding it. The immutable index records corpus/frame/model identity
and never depends on a mutable frame-cache pathname for evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import corpus_index, skeleton
from .embeddings import EmbeddingError, normalize_vector
from .paths import FRAMES_DIR as _DEFAULT_FRAMES_DIR, VECTOR_DIR as _DEFAULT_VECTOR_DIR
from .visual_embeddings import VisualEmbeddingBackend

FRAMES_DIR = _DEFAULT_FRAMES_DIR
VECTOR_DIR = _DEFAULT_VECTOR_DIR
VISUAL_INDEX_ROOT = VECTOR_DIR / "visual"
VISUAL_ARTIFACT_ROOT = VECTOR_DIR / "visual-artifacts"
VISUAL_SCHEMA_VERSION = 1
MAX_TOP_K = 50
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SINGLE_RE = re.compile(r"^single_(\d+)ms_.*\.(png|jpg|jpeg)$", re.IGNORECASE)
_VISUAL_INDEX_RE = re.compile(r"^vidx1-[0-9a-f]{24}$")


class VisualIndexError(RuntimeError):
    pass


class VisualIndexNotFound(VisualIndexError):
    pass


@dataclass(frozen=True)
class VisualPreparedIndex:
    corpus_revision: str
    visual_index_revision: str
    path: Path
    corpus_sha256: str
    text_model_id: str
    image_model_id: str
    total_videos: int
    indexed_videos: int
    missing_videos: list[str]
    frame_count: int
    cached: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "corpus_revision": self.corpus_revision,
            "visual_index_revision": self.visual_index_revision,
            "text_model_id": self.text_model_id,
            "image_model_id": self.image_model_id,
            "total_videos": self.total_videos,
            "indexed_videos": self.indexed_videos,
            "missing_videos": self.missing_videos,
            "coverage": (
                self.indexed_videos / self.total_videos if self.total_videos else 0.0
            ),
            "frame_count": self.frame_count,
            "cached": self.cached,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _pack_vector(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes | None, dim: int | None) -> list[float]:
    if blob is None or dim is None or int(dim) <= 0:
        raise VisualIndexError("visual frame is missing its embedding")
    dim_i = int(dim)
    if len(blob) != dim_i * 4:
        raise VisualIndexError("visual embedding byte length does not match dimension")
    return list(struct.unpack(f"<{dim_i}f", blob))


def _artifact_path(sha256: str, ext: str) -> Path:
    return VISUAL_ARTIFACT_ROOT / sha256[:2] / f"{sha256}.{ext}"


def _snapshot_frame(source: Path) -> tuple[str, str, Path]:
    """Freeze one mutable frame-cache file into the content-addressed store."""
    ext = source.suffix.lower().lstrip(".")
    if ext == "jpeg":
        ext = "jpg"
    if ext not in {"png", "jpg"}:
        raise VisualIndexError(f"unsupported cached frame format: {source.suffix!r}")

    sha256 = _sha256_file(source)
    target = _artifact_path(sha256, ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_file() or _sha256_file(target) != sha256:
            raise VisualIndexError(
                f"content-addressed visual artifact is corrupt: {sha256}"
            )
        return sha256, ext, target

    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if not target.is_file() or _sha256_file(target) != sha256:
            raise VisualIndexError(
                f"content-addressed visual artifact is corrupt: {sha256}"
            )
        return sha256, ext, target

    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            shutil.copyfileobj(src, out, length=1024 * 1024)
        if _sha256_file(target) != sha256:
            raise VisualIndexError("visual artifact snapshot hash verification failed")
        try:
            target.chmod(0o444)
        except OSError:
            # Read-only is defense in depth; content hash verification remains the
            # actual evidence-integrity boundary on filesystems without chmod.
            pass
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return sha256, ext, target


def _publish_immutable(tmp: Path, path: Path) -> None:
    """Publish a completed visual index without replacing an existing revision."""
    try:
        os.link(tmp, path)
        return
    except FileExistsError:
        return
    except OSError:
        # Hard links are not guaranteed on every supported filesystem. Claim the
        # path with O_EXCL, then copy only after we own that pathname.
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return
        try:
            with os.fdopen(fd, "wb") as out, tmp.open("rb") as src:
                shutil.copyfileobj(src, out, length=1024 * 1024)
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise


def _safe_cached_frame(video_dir: Path, raw_path: str) -> Path | None:
    path = Path(raw_path).expanduser()
    try:
        resolved = path.resolve()
        expected = video_dir.resolve()
    except OSError:
        return None
    if resolved.parent != expected or not resolved.is_file():
        return None
    if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
        return None
    return resolved


def _manifest_frames(video_dir: Path) -> list[tuple[float, Path, str]]:
    found: list[tuple[float, Path, str]] = []
    for manifest in sorted(video_dir.glob("sheet_*.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        timestamps = payload.get("frame_timestamps")
        paths = payload.get("frame_paths")
        if not isinstance(timestamps, list) or not isinstance(paths, list):
            continue
        if len(timestamps) != len(paths):
            continue
        for timestamp, raw_path in zip(timestamps, paths):
            try:
                timestamp_s = float(timestamp)
            except (TypeError, ValueError):
                continue
            if timestamp_s < 0 or not isinstance(raw_path, str):
                continue
            path = _safe_cached_frame(video_dir, raw_path)
            if path is not None:
                found.append((timestamp_s, path, "contact_sheet_frame"))
    return found


def _single_frames(video_dir: Path) -> list[tuple[float, Path, str]]:
    found: list[tuple[float, Path, str]] = []
    for path in sorted(video_dir.glob("single_*")):
        if not path.is_file():
            continue
        match = _SINGLE_RE.match(path.name)
        if match is None:
            continue
        found.append((int(match.group(1)) / 1000.0, path.resolve(), "single_frame"))
    return found


def discover_frames(
    handle: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Snapshot all trustworthy already-cached timestamped frames for a corpus."""
    corpus = skeleton.load_skeleton(handle)
    videos = corpus.get("videos")
    if not isinstance(videos, list):
        raise VisualIndexError("corpus revision has no valid videos list")

    records: list[dict[str, Any]] = []
    missing: list[str] = []
    seen_ids: set[str] = set()
    for video in videos:
        if not isinstance(video, dict):
            raise VisualIndexError("corpus video entries must be objects")
        video_id = str(video.get("id") or "")
        if not _VIDEO_ID_RE.fullmatch(video_id):
            raise VisualIndexError(f"invalid video id in corpus: {video_id!r}")
        if video_id in seen_ids:
            raise VisualIndexError(f"duplicate video id in corpus: {video_id}")
        seen_ids.add(video_id)

        video_dir = FRAMES_DIR / video_id
        if not video_dir.is_dir():
            missing.append(video_id)
            continue

        candidates = [*_manifest_frames(video_dir), *_single_frames(video_dir)]
        dedup: dict[tuple[str, float], dict[str, Any]] = {}
        for timestamp_s, source_path, source_kind in candidates:
            sha256, ext, artifact = _snapshot_frame(source_path)
            dedup[(sha256, timestamp_s)] = {
                "video_id": video_id,
                "title": video.get("title"),
                "channel": video.get("channel"),
                "timestamp_s": timestamp_s,
                "frame_sha256": sha256,
                "artifact_ext": ext,
                "artifact_path": artifact,
                "source_kind": source_kind,
            }
        if not dedup:
            missing.append(video_id)
            continue
        records.extend(
            sorted(
                dedup.values(),
                key=lambda record: (record["timestamp_s"], record["frame_sha256"]),
            )
        )

    return corpus, records, missing


def _index_path(handle: str, revision: str) -> Path:
    if not skeleton.is_valid_handle(handle):
        raise VisualIndexError(f"invalid corpus revision handle: {handle!r}")
    if not _VISUAL_INDEX_RE.fullmatch(revision):
        raise VisualIndexError(f"invalid visual index revision: {revision!r}")
    return VISUAL_INDEX_ROOT / handle / f"{revision}.sqlite"


_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE frames (
    id INTEGER PRIMARY KEY,
    video_id TEXT NOT NULL,
    title TEXT,
    channel TEXT,
    timestamp_s REAL NOT NULL,
    frame_sha256 TEXT NOT NULL,
    artifact_ext TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    embedding BLOB NOT NULL,
    embedding_dim INTEGER NOT NULL
);
CREATE INDEX idx_visual_video_time ON frames(video_id, timestamp_s);
"""


def _read_meta(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise VisualIndexNotFound(f"visual index does not exist: {path}")
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error as exc:
        raise VisualIndexError(f"invalid visual index: {exc}") from exc
    finally:
        conn.close()
    return {str(key): str(value) for key, value in rows}


def _prepared(path: Path, *, cached: bool) -> VisualPreparedIndex:
    meta = _read_meta(path)
    try:
        return VisualPreparedIndex(
            corpus_revision=meta["corpus_revision"],
            visual_index_revision=meta["visual_index_revision"],
            path=path,
            corpus_sha256=meta["corpus_sha256"],
            text_model_id=meta["text_model_id"],
            image_model_id=meta["image_model_id"],
            total_videos=int(meta["total_videos"]),
            indexed_videos=int(meta["indexed_videos"]),
            missing_videos=list(json.loads(meta.get("missing_videos", "[]"))),
            frame_count=int(meta["frame_count"]),
            cached=cached,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VisualIndexError("incomplete visual index metadata") from exc


def prepare_visual_index(
    handle: str,
    *,
    backend: VisualEmbeddingBackend,
) -> VisualPreparedIndex:
    corpus, records, missing = discover_frames(handle)
    corpus_sha = corpus_index._corpus_hash(corpus)
    text_model = str(getattr(backend, "text_model_id", "")).strip()
    image_model = str(getattr(backend, "image_model_id", "")).strip()
    if not text_model or not image_model:
        raise VisualIndexError("visual backend must expose paired model IDs")

    frame_identity = [
        {
            "video_id": record["video_id"],
            "timestamp_s": record["timestamp_s"],
            "frame_sha256": record["frame_sha256"],
            "artifact_ext": record["artifact_ext"],
        }
        for record in records
    ]
    identity = {
        "schema_version": VISUAL_SCHEMA_VERSION,
        "corpus_revision": handle,
        "corpus_sha256": corpus_sha,
        "text_model_id": text_model,
        "image_model_id": image_model,
        "frames": frame_identity,
        "normalization": "l2-f32le",
    }
    revision = f"vidx1-{corpus_index._sha256_json(identity)[:24]}"
    path = _index_path(handle, revision)
    if path.exists():
        return _prepared(path, cached=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if tmp.exists():
        tmp.unlink()

    try:
        vectors = backend.embed_images([record["artifact_path"] for record in records])
    except EmbeddingError:
        raise
    except Exception as exc:
        raise VisualIndexError("visual backend failed while embedding frames") from exc
    if len(vectors) != len(records):
        raise VisualIndexError(
            f"visual backend returned {len(vectors)} vectors for {len(records)} frames"
        )

    normalized: list[list[float]] = []
    expected_dim: int | None = None
    for vector in vectors:
        norm = normalize_vector(vector)
        if expected_dim is None:
            expected_dim = len(norm)
        elif len(norm) != expected_dim:
            raise VisualIndexError("visual embedding dimension changed within one index")
        normalized.append(norm)

    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(_SCHEMA)
        for frame_id, (record, vector) in enumerate(zip(records, normalized), start=1):
            conn.execute(
                "INSERT INTO frames (id, video_id, title, channel, timestamp_s, "
                "frame_sha256, artifact_ext, source_kind, embedding, embedding_dim) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    frame_id,
                    record["video_id"],
                    record.get("title"),
                    record.get("channel"),
                    float(record["timestamp_s"]),
                    record["frame_sha256"],
                    record["artifact_ext"],
                    record["source_kind"],
                    _pack_vector(vector),
                    len(vector),
                ),
            )

        indexed_videos = len({record["video_id"] for record in records})
        meta: dict[str, Any] = {
            "schema_version": VISUAL_SCHEMA_VERSION,
            "visual_index_revision": revision,
            "corpus_revision": handle,
            "corpus_sha256": corpus_sha,
            "created_at": _now_iso(),
            "text_model_id": text_model,
            "image_model_id": image_model,
            "total_videos": len(corpus.get("videos") or []),
            "indexed_videos": indexed_videos,
            "missing_videos": missing,
            "frame_count": len(records),
            "embedding_dim": expected_dim or 0,
            "identity_sha256": corpus_index._sha256_json(identity),
        }
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                (
                    key,
                    corpus_index._canonical_json(value)
                    if isinstance(value, (list, dict))
                    else str(value),
                )
                for key, value in meta.items()
            ],
        )
        conn.commit()
    except Exception:
        conn.close()
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    else:
        conn.close()

    try:
        _publish_immutable(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return _prepared(path, cached=False)


def list_visual_indexes(handle: str) -> list[VisualPreparedIndex]:
    if not skeleton.is_valid_handle(handle):
        raise VisualIndexError(f"invalid corpus revision handle: {handle!r}")
    directory = VISUAL_INDEX_ROOT / handle
    if not directory.exists():
        return []
    found: list[tuple[str, VisualPreparedIndex]] = []
    for path in directory.glob("vidx1-*.sqlite"):
        try:
            prepared = _prepared(path, cached=True)
            created = _read_meta(path).get("created_at", "")
        except VisualIndexError:
            continue
        found.append((created, prepared))
    found.sort(key=lambda item: (item[0], item[1].visual_index_revision), reverse=True)
    return [prepared for _created, prepared in found]


def latest_visual_index(
    handle: str,
    *,
    text_model_id: str | None = None,
    image_model_id: str | None = None,
) -> VisualPreparedIndex | None:
    for prepared in list_visual_indexes(handle):
        if text_model_id is not None and prepared.text_model_id != text_model_id:
            continue
        if image_model_id is not None and prepared.image_model_id != image_model_id:
            continue
        return prepared
    return None


def search_visual_index(
    handle: str,
    query: str,
    *,
    backend: VisualEmbeddingBackend,
    top_k: int = 10,
    visual_index_revision: str | None = None,
) -> dict[str, Any]:
    query = query.strip()
    if not query:
        raise VisualIndexError("visual query must not be empty")
    if top_k < 1 or top_k > MAX_TOP_K:
        raise VisualIndexError(f"top_k must be between 1 and {MAX_TOP_K}")

    text_model = str(getattr(backend, "text_model_id", "")).strip()
    image_model = str(getattr(backend, "image_model_id", "")).strip()
    if visual_index_revision is None:
        prepared = latest_visual_index(
            handle, text_model_id=text_model, image_model_id=image_model
        )
        if prepared is None:
            raise VisualIndexNotFound("no compatible prepared visual index exists")
    else:
        prepared = _prepared(_index_path(handle, visual_index_revision), cached=True)
        if prepared.text_model_id != text_model or prepared.image_model_id != image_model:
            raise VisualIndexError(
                "pinned visual index model identity does not match active visual backend"
            )

    try:
        query_vector = normalize_vector(backend.embed_query(query))
    except EmbeddingError:
        raise
    except Exception as exc:
        raise VisualIndexError("visual backend failed while embedding query") from exc

    meta = _read_meta(prepared.path)
    try:
        stored_dim = int(meta.get("embedding_dim", "0"))
    except ValueError as exc:
        raise VisualIndexError("visual index has invalid embedding dimension") from exc
    if stored_dim and len(query_vector) != stored_dim:
        raise VisualIndexError(
            f"visual query dim {len(query_vector)} does not match index dim {stored_dim}"
        )

    conn = sqlite3.connect(prepared.path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM frames").fetchall()
    finally:
        conn.close()

    ranked: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        vector = _unpack_vector(row["embedding"], row["embedding_dim"])
        if len(vector) != len(query_vector):
            raise VisualIndexError("visual frame embedding dimension is inconsistent")
        score = sum(a * b for a, b in zip(query_vector, vector))
        ranked.append((score, row))
    ranked.sort(
        key=lambda item: (
            -item[0],
            str(item[1]["video_id"]),
            float(item[1]["timestamp_s"]),
            str(item[1]["frame_sha256"]),
        )
    )
    ranked = ranked[:top_k]

    hits: list[dict[str, Any]] = []
    for rank, (score, row) in enumerate(ranked, start=1):
        timestamp_s = float(row["timestamp_s"])
        artifact_ext = str(row["artifact_ext"])
        artifact = _artifact_path(row["frame_sha256"], artifact_ext)
        if not artifact.is_file() or _sha256_file(artifact) != row["frame_sha256"]:
            raise VisualIndexError(
                f"frozen visual artifact failed integrity check: {row['frame_sha256']}"
            )
        hits.append(
            {
                "rank": rank,
                "video_id": row["video_id"],
                "title": row["title"],
                "channel": row["channel"],
                "timestamp_s": timestamp_s,
                "url": (
                    f"https://www.youtube.com/watch?v={row['video_id']}"
                    f"&t={max(0, int(timestamp_s))}s"
                ),
                "artifact_path": str(artifact),
                "artifact_ext": artifact_ext,
                "frame_sha256": row["frame_sha256"],
                "source_kind": row["source_kind"],
                "score": score,
            }
        )

    return {
        "corpus_revision": handle,
        "visual_index_revision": prepared.visual_index_revision,
        "backend": "clip",
        "text_model_id": prepared.text_model_id,
        "image_model_id": prepared.image_model_id,
        "query": query,
        "coverage": prepared.as_dict(),
        "hits": hits,
    }
