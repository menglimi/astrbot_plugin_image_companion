from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

from astrbot_plugin_image_companion.main import ImageCompanionExtensionAPI


def test_reference_import_ids_match_private_companion_contract(tmp_path: Path) -> None:
    api = ImageCompanionExtensionAPI(SimpleNamespace(data_dir=str(tmp_path)))
    receipt = asyncio.run(
        api.import_references(
            {
                "assets": [{"content": b"reference-bytes"}],
            }
        )
    )

    assert receipt["status"] == "succeeded"
    assert re.fullmatch(r"reflease_[0-9a-f]{48}", receipt["lease_id"])
    assert re.fullmatch(r"ref_[0-9a-f]{48}", receipt["asset_ids"][0])
    assert api.release_reference_import(receipt["lease_id"]) is True
