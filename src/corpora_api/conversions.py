"""Bounded, source-preserving conversion through corpora-core's packaged CLIs."""

import hashlib
import io
import json
import os
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

MAX_UPLOAD_BYTES = 3_000_000
MAX_ARCHIVE_BYTES = 4_000_000
MAX_BUNDLE_BYTES = 64_000_000
MAX_BUNDLE_FILES = 3_000

ConversionKind = Literal["tei", "epub", "pdf"]


class ConversionRejected(Exception):
    """The converter rejected an input or could not produce a usable bundle."""


class ConversionUnavailable(Exception):
    """A required converter executable is missing."""


class ConversionTooLarge(Exception):
    """The upload or completed bundle exceeds this synchronous API's limits."""


class ConversionTimedOut(Exception):
    """The converter exceeded its bounded execution time."""


@dataclass(frozen=True)
class ConversionResult:
    archive: bytes
    status: str
    source_sha256: str
    archive_sha256: str


class ConversionBridge:
    def __init__(
        self,
        *,
        tei_bin: str | None = None,
        epub_bin: str | None = None,
        pdf_bin: str | None = None,
        epub_checker: str | None = None,
        pdf_extractor: str | None = None,
    ) -> None:
        self.tei_bin = tei_bin or os.getenv("CORPORA_TEI_TO_TF_BIN", "tei-to-tf")
        self.epub_bin = epub_bin or os.getenv("CORPORA_EPUB_TO_TF_BIN", "epub-to-tf")
        self.pdf_bin = pdf_bin or os.getenv("CORPORA_PDF_TO_TF_BIN", "pdf-to-tf")
        self.epub_checker = epub_checker or os.getenv(
            "CORPORA_EPUB_CHECK_BIN", "corpora-epub-check"
        )
        self.pdf_extractor = pdf_extractor or os.getenv(
            "CORPORA_PDF_EXTRACT_BIN", "corpora-pdf-extract"
        )

    def convert(
        self,
        kind: ConversionKind,
        filename: str,
        source_bytes: bytes,
        *,
        choice: Literal["reject", "original", "edited"] = "reject",
        apparatus: Literal["reject", "lemma", "witness"] = "reject",
        witness: str | None = None,
        notes: Literal["separate", "include"] = "separate",
        allow_no_namespace: bool = False,
        repair: bool = False,
        languages: Literal["eng", "fra", "heb", "eng+fra", "eng+heb"] = "eng",
    ) -> ConversionResult:
        if not source_bytes:
            raise ConversionRejected("The uploaded source is empty")
        if len(source_bytes) > MAX_UPLOAD_BYTES:
            raise ConversionTooLarge("Upload exceeds the 3 MB conversion limit")
        uploaded_name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        expected_suffixes = {
            "tei": {".xml", ".tei"},
            "epub": {".epub"},
            "pdf": {".pdf"},
        }
        if (
            not uploaded_name
            or len(uploaded_name) > 128
            or Path(uploaded_name).suffix.lower() not in expected_suffixes[kind]
        ):
            raise ConversionRejected(f"Upload a {kind.upper()} file with a supported extension")
        if apparatus == "witness" and not witness:
            raise ConversionRejected("A witness token is required for witness apparatus")
        if witness and len(witness) > 128:
            raise ConversionRejected("The witness token is too long")

        with tempfile.TemporaryDirectory(prefix="corpora-conversion-") as work:
            root = Path(work)
            source = root / f"source{Path(uploaded_name).suffix.lower()}"
            output = root / "bundle"
            source.write_bytes(source_bytes)
            command = self._command(
                kind,
                source,
                output,
                choice=choice,
                apparatus=apparatus,
                witness=witness,
                notes=notes,
                allow_no_namespace=allow_no_namespace,
                repair=repair,
                languages=languages,
            )
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=100 if kind == "pdf" else 75,
                    check=False,
                    cwd=root,
                )
            except FileNotFoundError as exc:
                raise ConversionUnavailable(
                    f"The {kind.upper()} converter is not installed"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise ConversionTimedOut(f"The {kind.upper()} conversion timed out") from exc
            if completed.returncode != 0 and not (kind == "pdf" and completed.returncode == 3):
                raise ConversionRejected(f"The {kind.upper()} converter rejected this input")
            status = self._status(kind, output)
            archive = self._archive(output)
            return ConversionResult(
                archive=archive,
                status=status,
                source_sha256=hashlib.sha256(source_bytes).hexdigest(),
                archive_sha256=hashlib.sha256(archive).hexdigest(),
            )

    def _command(
        self,
        kind: ConversionKind,
        source: Path,
        output: Path,
        *,
        choice: str,
        apparatus: str,
        witness: str | None,
        notes: str,
        allow_no_namespace: bool,
        repair: bool,
        languages: str,
    ) -> list[str]:
        if kind == "tei":
            command = [
                self.tei_bin,
                source.name,
                "--output",
                output.name,
                "--choice",
                choice,
                "--apparatus",
                apparatus,
                "--notes",
                notes,
                "--max-slots",
                "200000",
                "--max-memberships",
                "2000000",
                "--max-file-bytes",
                str(MAX_UPLOAD_BYTES),
                "--max-total-bytes",
                str(MAX_UPLOAD_BYTES),
            ]
            if witness:
                command.extend(("--witness", witness))
            if allow_no_namespace:
                command.append("--allow-no-namespace")
            return command
        if kind == "epub":
            command = [
                self.epub_bin,
                source.name,
                output.name,
                "--checker",
                self.epub_checker,
                "--max-slots",
                "200000",
            ]
            if repair:
                command.append("--repair")
            return command
        return [
            self.pdf_bin,
            source.name,
            "--output",
            output.name,
            "--extractor",
            self.pdf_extractor,
            "--languages",
            languages,
            "--max-pages",
            "10",
        ]

    @staticmethod
    def _status(kind: ConversionKind, output: Path) -> str:
        if not output.is_dir():
            raise ConversionRejected("The converter produced no bundle")
        if kind == "pdf":
            report_path = output / "conversion-report.json"
            if not report_path.is_file():
                raise ConversionRejected("The PDF converter produced no report")
            try:
                report = json.loads(report_path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise ConversionRejected("The PDF converter produced an invalid report") from exc
            status = report.get("status")
            if status not in {"converted", "converted_with_issues", "gated"}:
                raise ConversionRejected("The PDF converter did not verify its output")
            return status
        if not (output / "manifest.json").is_file():
            raise ConversionRejected("The converter produced no manifest")
        return "converted"

    @staticmethod
    def _archive(output: Path) -> bytes:
        paths = sorted(path for path in output.rglob("*") if path.is_file())
        if any(path.is_symlink() for path in paths):
            raise ConversionRejected("The converter produced an unsupported link")
        total_bytes = sum(path.stat().st_size for path in paths)
        if len(paths) > MAX_BUNDLE_FILES or total_bytes > MAX_BUNDLE_BYTES:
            raise ConversionTooLarge("The converted bundle exceeds the synchronous API limit")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                archive.write(path, arcname=path.relative_to(output))
        result = stream.getvalue()
        if len(result) > MAX_ARCHIVE_BYTES:
            raise ConversionTooLarge("The ZIP bundle exceeds Vercel's 4 MB response limit")
        return result
