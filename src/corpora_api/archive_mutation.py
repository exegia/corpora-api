"""Stage typed feature edits in private archive copies; never overwrite HEAD."""

from __future__ import annotations

import gc
import hashlib
import json
import re
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from cfabric import Fabric
from corpus_validate.ai_scope import scope_content_hash
from corpus_validate.ai_scope import scope_nodes as _scope_nodes
from corpus_validate.ai_types import NodeScope, ScopeLevel, UnitRange
from corpus_validate.reader import _load_api
from corpus_validate.validator import _extract as extract_members
from corpus_validate.validator import _members, _plain, validate_archive
from tf.fabric import Fabric as TextFabric

from corpora_api.drafts import DraftHead
from corpora_api.models import AIChatSuggestion as Suggestion
from corpora_api.mutation_journal import (
    CurationError,
    Evidence,
    Plan,
    _operation_id,
    _validate_plan,
)


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()


def _load(root: Path) -> tuple[Any, dict]:
    manifest = yaml.safe_load((root / "manifest.yml").read_text())
    if not isinstance(manifest, dict) or not manifest.get("version"):
        raise CurationError(422, "Archive manifest has no version")
    # Source files are required so cache regeneration cannot silently discard
    # a CFM-only feature. Unrecognized formats are read-only for this adapter.
    corpus = root / "corpora"
    if not (corpus / "otype.tf").is_file() or not (corpus / "oslots.tf").is_file():
        raise CurationError(422, "Feature editing requires retained Text-Fabric sources")
    api = _load_api(root)
    if api is None or isinstance(api, bool):
        raise CurationError(422, "Corpus features could not be loaded")
    for feature in (*api.Fall(), *api.Eall(), "otext"):
        if not (corpus / (feature + ".tf")).is_file():
            raise CurationError(422, "Feature editing requires complete Text-Fabric sources")
    # Verify all data against a cold source load, not only the validator's sample.
    # Otherwise a stale shipped cache can hide displaced source values.
    with tempfile.TemporaryDirectory(prefix="ai-cold-") as temporary:
        cold_root = Path(temporary) / "corpora"
        shutil.copytree(corpus, cold_root)
        for name in (".cfr", ".cfm", ".tf"):
            cache = cold_root / name
            if cache.exists():
                shutil.rmtree(cache)
        cold = TextFabric(locations=str(cold_root), silent="deep").loadAll(silent="deep")
        if (
            not cold
            or set(cold.Fall()) != set(api.Fall())
            or set(cold.Eall()) != set(api.Eall())
            or cold.TF.features["otext"].metaData != api.TF.features["otext"].metaData
            or any(
                cold.TF.features[name].metaData.get("valueType")
                != api.TF.features[name].metaData.get("valueType")
                for name in (*api.Fall(), *api.Eall())
            )
            or _render_hash(cold) != _render_hash(api)
            or _untouched_hash(cold, set(), 0) != _untouched_hash(api, set(), 0)
        ):
            raise CurationError(422, "Shipped cache does not match Text-Fabric sources")
    return api, manifest


def scope_nodes(api: Any, scope: Any) -> tuple[int, ...]:
    data = scope.model_dump()
    data["level"] = ScopeLevel(data["level"])
    if data.get("unit_range") is not None:
        data["unit_range"] = UnitRange(**data["unit_range"])
    return _scope_nodes(api, NodeScope(**data))


def _build_toc(
    *, uid: str, version: str, corpus_id: str, corpora_dir: Path, publisher_id: str, author_ids: Any
) -> dict[str, Any]:
    api = _load_api(corpora_dir.parent)
    paths = [p for p in sorted(corpora_dir.rglob("*")) if p.is_file()]
    return {
        "uid": uid,
        "version": version,
        "corpusId": corpus_id,
        "has_sections": bool(api.T.sectionTypes),
        "has_features": len(api.Fall()) > 2,
        "has_nodes": len(api.F.otype.all) > 0,
        "files": [
            {"id": str(uuid4()), "path": p.name, "type": "text", "name": p.stem, "visible": True}
            for p in paths
        ],
        "total_file_count": len(paths),
        "total_file_size": sum(p.stat().st_size for p in paths),
        "publisherId": publisher_id,
        "authorId": author_ids,
    }


def _values(api: Any, suggestion: Suggestion) -> dict[str, str | None]:
    if suggestion.target_node not in scope_nodes(api, suggestion.scope):
        raise CurationError(422, "Suggestion target is outside its pinned scope")
    result = {}
    for name in api.Fall():
        value = api.Fs(name).v(suggestion.target_node)
        result[name] = None if value is None else str(value)
    return result


def _render_hash(api: Any, *, include_sections: bool = True) -> str:
    """Bind every slot and advertised rendering format to the cold source."""
    checksum = hashlib.sha256()
    formats = sorted(api.T.formats)
    config = [formats, list(api.T.sectionTypes), list(api.T.sectionFeats), api.T.defaultFormat]
    checksum.update(json.dumps(config, ensure_ascii=False).encode())
    if include_sections:
        for node in range(1, int(api.F.otype.maxNode) + 1):
            if api.F.otype.v(node) in api.T.sectionTypes:
                section = _plain(api.T.sectionFromNode(node))
                checksum.update(json.dumps(["section", node, section], ensure_ascii=False).encode())
                checksum.update(b"\n")
    for name in formats:
        for node in range(1, int(api.F.otype.maxSlot) + 1):
            checksum.update(
                json.dumps([name, node, api.T.text(node, fmt=name)], ensure_ascii=False).encode()
            )
            checksum.update(b"\n")
    return checksum.hexdigest()


def _untouched_hash(api: Any, fields: set[str], target: int) -> str:
    """Detect stale caches or rebuild drift outside the exact edited values."""
    checksum = hashlib.sha256()
    for name in sorted(api.Fall()):
        for node, value in sorted(api.Fs(name).items()):
            if name in fields and node == target:
                continue
            checksum.update(
                json.dumps(["node", name, int(node), _plain(value)], ensure_ascii=False).encode()
            )
            checksum.update(b"\n")
    for name in sorted(api.Eall()):
        for node, linked in sorted(api.Es(name).items()):
            destinations = (
                sorted((int(n), _plain(value)) for n, value in linked.items())
                if isinstance(linked, dict)
                else sorted(int(n) for n in linked)
            )
            checksum.update(
                json.dumps(["edge", name, int(node), destinations], ensure_ascii=False).encode()
            )
            checksum.update(b"\n")
    return checksum.hexdigest()


def _evidence(api: Any, manifest: dict, head: DraftHead, suggestion: Suggestion) -> Evidence:
    if str(manifest["version"]).removeprefix("v") != head.version.removeprefix("v"):
        raise CurationError(409, "Draft manifest and registered version differ")
    return Evidence(
        revision=str(head.revision),
        digest=head.digest,
        version=head.version,
        content_hash=scope_content_hash(api, scope_nodes(api, suggestion.scope)),
        values=_values(api, suggestion),
    )


def _extract(archive: Path, root: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        extract_members(source, _members(source), root)


def inspect_archive(archive: Path, head: DraftHead, suggestion: Suggestion) -> Evidence:
    try:
        if digest(archive) != head.digest:
            raise CurationError(409, "Downloaded draft digest mismatch")
        with tempfile.TemporaryDirectory(prefix="ai-inspect-") as temporary:
            root = Path(temporary)
            _extract(archive, root)
            api, manifest = _load(root)
            return _evidence(api, manifest, head, suggestion)
    except (OSError, ValueError, zipfile.BadZipFile, yaml.YAMLError) as exc:
        raise CurationError(422, "Corpus archive cannot be inspected") from exc


def edit_archive(archive: Path, output: Path, head: DraftHead, suggestion: Suggestion) -> Plan:
    """Preserve all unrelated archive members; return exact before/after proof.

    Only existing typed node features may change. Slot text and graph topology
    remain source/walker responsibilities. A fresh cache and TOC are mandatory.
    """
    try:
        if archive.resolve() == output.resolve():
            raise CurationError(422, "Staging output must differ from the source archive")
        if digest(archive) != head.digest:
            raise CurationError(409, "Downloaded draft digest mismatch")
        with tempfile.TemporaryDirectory(prefix="ai-edit-") as temporary:
            root = Path(temporary)
            _extract(archive, root)
            api, manifest = _load(root)
            before = _evidence(api, manifest, head, suggestion)
            if suggestion.kind in ("boundary", "text"):
                raise CurationError(
                    422, "Boundary and source-text repairs require the source/walker workflow"
                )
            fields = [row.field for row in suggestion.diff]
            if len(set(fields)) != len(fields):
                raise CurationError(422, "Duplicate feature in change")
            # All declared render-format dependencies are protected, not only a
            # literal feature named text. This also prevents forged kind labels.
            otext = (root / "corpora" / "otext.tf").read_text()
            rendered = set()
            for line in otext.splitlines():
                if line.startswith("@fmt:"):
                    for token in re.findall(r"\{([^}]+)\}", line):
                        rendered.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", token))
            untouched = _untouched_hash(api, set(fields), suggestion.target_node)
            original_render = _render_hash(api, include_sections=False)
            changes: dict[str, dict[int, str | int]] = {}
            metadata = {}
            for row in suggestion.diff:
                name = row.field
                if (
                    not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                    or name in {"otype", "oslots", "otext"}
                    or name in rendered
                    or name not in api.Fall()
                ):
                    raise CurationError(422, "Feature is not editable")
                if before.values.get(name) != row.old or row.old == row.new:
                    raise CurationError(409, "Displaced feature value does not match")
                source = root / "corpora" / (name + ".tf")
                header = source.read_text().split("\n\n", 1)[0]
                kind = re.search(r"^@valueType=(str|int)$", header, re.M)
                if kind is None:
                    raise CurationError(422, "Feature has no supported value type")
                values = dict(api.Fs(name).items())
                if row.new is None:
                    required = dict(zip(api.T.sectionTypes, api.T.sectionFeats, strict=True))
                    if name == required.get(api.F.otype.v(suggestion.target_node)):
                        raise CurationError(422, "A required section label cannot be removed")
                    values.pop(suggestion.target_node, None)
                elif kind[1] == "int":
                    if not re.fullmatch(r"-?(0|[1-9][0-9]*)", row.new):
                        raise CurationError(422, "Integer feature requires a canonical integer")
                    number = int(row.new)
                    if not -(2**63) <= number < 2**63:
                        raise CurationError(422, "Integer feature exceeds the supported range")
                    values[suggestion.target_node] = number
                else:
                    if "\x00" in row.new:
                        raise CurationError(422, "Feature value contains a null byte")
                    values[suggestion.target_node] = row.new
                changes[name] = values
                metadata[name] = dict(api.TF.features[name].metaData)
                metadata[name]["valueType"] = kind[1]
            match = re.fullmatch(r"v?(\d+)\.(\d+)", head.version)
            if not match:
                raise CurationError(422, "Draft version must use major.minor notation")
            version = f"v{match[1]}.{int(match[2]) + 1}"
            revision = str(uuid4())
            expected = dict(before.values)
            expected.update({r.field: r.new for r in suggestion.diff})
            # Validate stale version/hash before writing even the private copy.
            _validate_plan(
                suggestion,
                Plan(
                    before=before,
                    after=Evidence(
                        revision=revision,
                        digest="staged",
                        version=version,
                        content_hash=before.content_hash,
                        values=expected,
                    ),
                ),
            )
            if not Fabric(locations=str(root / "corpora"), silent="deep").save(
                nodeFeatures=changes,
                metaData=metadata,
                location=str(root / "corpora"),
                silent="deep",
            ):
                raise CurationError(422, "Feature serialization failed")
            # Release mapped handles before removing caches, including on Windows.
            del api
            gc.collect()
            # Never ship stale compiled data after modifying the source features.
            for cache_name in (".cfr", ".cfm", ".tf"):
                cache = root / "corpora" / cache_name
                if cache.exists():
                    shutil.rmtree(cache)
            # Compile a fresh cache from retained TF sources, never reuse the shipped cache.
            if not Fabric(locations=str(root / "corpora"), silent="deep").loadAll(silent="deep"):
                raise CurationError(422, "Context-Fabric cache compilation failed")
            manifest["version"] = version
            (root / "manifest.yml").write_text(yaml.safe_dump(manifest, sort_keys=False))
            old_toc = yaml.safe_load((root / "toc.yml").read_text())
            if not isinstance(old_toc, dict):
                raise CurationError(422, "Invalid archive table of contents")
            toc = _build_toc(
                uid=str(old_toc.get("uid", "")),
                version=version,
                corpus_id=str(manifest.get("uid", "")),
                corpora_dir=root / "corpora",
                publisher_id=str(old_toc.get("publisherId", "")),
                author_ids=old_toc.get("authorId", []),
            )
            (root / "toc.yml").write_text(
                yaml.safe_dump(
                    {
                        **old_toc,
                        **{
                            key: toc[key]
                            for key in (
                                "version",
                                "has_sections",
                                "has_features",
                                "has_nodes",
                                "files",
                                "total_file_count",
                                "total_file_size",
                            )
                        },
                    },
                    sort_keys=False,
                )
            )
            history_path = root / "history.yml"
            history = yaml.safe_load(history_path.read_text())
            if not isinstance(history, dict) or not isinstance(history.get("versions"), list):
                raise CurationError(422, "Archive version history is missing or invalid")
            if any(not isinstance(entry, dict) for entry in history["versions"]):
                raise CurationError(422, "Archive version history contains invalid entries")
            if any(
                "current" in entry and not isinstance(entry["current"], bool)
                for entry in history["versions"]
            ):
                raise CurationError(
                    422, "Archive current version history is missing or inconsistent"
                )
            current = [entry for entry in history["versions"] if entry.get("current") is True]
            if len(current) != 1 or str(
                current[0].get("label") or current[0].get("id") or ""
            ).removeprefix("v") != head.version.removeprefix("v"):
                raise CurationError(
                    422, "Archive current version history is missing or inconsistent"
                )
            if any(
                any(
                    str(entry.get(key, "")).removeprefix("v") == version.removeprefix("v")
                    for key in ("id", "label")
                )
                for entry in history["versions"]
            ):
                raise CurationError(409, "Archive history already contains the next version")
            if current[0].get("snapshot_revision") not in (None, str(head.revision)):
                raise CurationError(409, "Archive history revision does not match draft HEAD")
            for entry in history["versions"]:
                if entry.get("current"):
                    entry["snapshot_key"] = head.object_key
                    entry["snapshot_revision"] = str(head.revision)
                entry["current"] = False
            reverts = suggestion.id[5:] if suggestion.id.startswith("undo:") else None
            provenance = {
                "operation_id": _operation_id(str(head.owner), suggestion.id, reverts),
                "reverts_operation_id": reverts,
                "suggestion_id": suggestion.id,
                "scope": suggestion.scope.model_dump(mode="json"),
                "node_id": suggestion.target_node,
                "diff": [r.model_dump() for r in suggestion.diff],
                "resp": ["#corpora-ai", "#" + str(head.owner)],
                "applied_by": str(head.owner),
                "base_revision": str(head.revision),
            }
            history["versions"].append(
                {
                    "id": version,
                    "label": version,
                    "title": "AI feature correction",
                    "at": datetime.now(UTC).isoformat(),
                    "current": True,
                    "snapshot_key": f"{head.owner}/ai/{head.document_id}/{revision}.corpus",
                    "snapshot_revision": revision,
                    "author": {"id": str(head.owner)},
                    "approved_by": None,
                    "notes": [],
                    "files": [{"path": f"corpora/{f}.tf", "kind": "modified"} for f in fields],
                    "ai_change": provenance,
                }
            )
            history_path.write_text(yaml.safe_dump(history, sort_keys=False))
            log_path = root / "ai-changes.json"
            log = json.loads(log_path.read_text()) if log_path.exists() else []
            if not isinstance(log, list):
                raise CurationError(422, "Archive change provenance is invalid")
            log.append({**provenance, "version": version, "revision": revision})
            log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2) + "\n")
            rebuilt, updated = _load(root)
            after_values = _values(rebuilt, suggestion)
            after_hash = scope_content_hash(rebuilt, scope_nodes(rebuilt, suggestion.scope))
            if (
                after_values != expected
                or after_hash != before.content_hash
                or _untouched_hash(rebuilt, set(fields), suggestion.target_node) != untouched
                or _render_hash(rebuilt, include_sections=False) != original_render
            ):
                raise CurationError(
                    422,
                    "Rebuilt archive does not preserve the requested feature diff and source text",
                )
            output.parent.mkdir(parents=True, exist_ok=True)
            with (
                zipfile.ZipFile(archive) as source,
                zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target,
            ):
                # Retain safe directory entries, including user-provided empty directories.
                # Removed cache directories stay absent unless the rebuild recreated them.
                for info in _members(source):
                    if info.is_dir() and (root / info.filename).is_dir():
                        target.writestr(info, b"")
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        target.write(path, path.relative_to(root))
            verdict = validate_archive(output.read_bytes(), "staged.corpus")
            if not verdict.get("valid"):
                raise CurationError(422, "Rebuilt corpus archive failed validation")
            return Plan(
                before=before,
                after=Evidence(
                    revision=revision,
                    digest=digest(output),
                    version=version,
                    content_hash=after_hash,
                    values=after_values,
                ),
            )
    except (
        OSError,
        ValueError,
        TypeError,
        zipfile.BadZipFile,
        yaml.YAMLError,
    ) as exc:
        raise CurationError(422, "Corpus archive cannot be edited") from exc


def run_archive_mutation(data: bytes, *, head: dict, suggestion: dict, edit: bool) -> dict:
    """Operate in the short-lived corpus worker and return only bounded output."""
    import base64

    parsed_head = DraftHead.model_validate(head)
    parsed_suggestion = Suggestion.model_validate(suggestion)
    with tempfile.TemporaryDirectory(prefix="ai-worker-") as temporary:
        source = Path(temporary) / "before.corpus"
        source.write_bytes(data)
        if not edit:
            return {
                "evidence": inspect_archive(source, parsed_head, parsed_suggestion).model_dump(
                    mode="json"
                )
            }
        output = Path(temporary) / "after.corpus"
        plan = edit_archive(source, output, parsed_head, parsed_suggestion)
        return {
            "plan": plan.model_dump(mode="json"),
            "data": base64.b64encode(output.read_bytes()).decode("ascii"),
        }
