import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from corpora_api.project_discovery_api import DiscoveryEvidence


def test_maximum_evidence_remains_readable_after_review():
    corpus = str(uuid4())
    revision = "a" * 40
    value = {
        "id": uuid4(),
        "source": {
            "work_id": "work",
            "edition_id": "edition",
            "package_id": corpus,
            "revision": revision,
            "document_id": ".corpora/source.corpus",
            "locators": [{"kind": "structural", "anchor_id": f"{corpus}@{revision}/Mini!word1"}],
        },
        "quote": "😀" * 7000,
        "cited_name": "Example",
    }
    initial = DiscoveryEvidence.model_validate(value)
    size = len(
        json.dumps(initial.model_dump(mode="json", exclude={"status"}), ensure_ascii=False).encode()
    )
    value["quote"] += "x" * (32768 - size)
    for status in ("pending", "accepted", "rejected"):
        evidence = DiscoveryEvidence.model_validate({**value, "status": status})
        serialized = evidence.model_dump(mode="json", exclude={"status"})
        assert len(json.dumps(serialized, ensure_ascii=False).encode()) == 32768
    with pytest.raises(ValidationError, match="exceeds size limit"):
        DiscoveryEvidence.model_validate({**value, "quote": value["quote"] + "x"})
