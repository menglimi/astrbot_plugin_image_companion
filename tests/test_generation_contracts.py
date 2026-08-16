from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generation_contracts import (  # noqa: E402
    BackendCapabilitiesV1,
    ContractValidationError,
    GenerationResultV1,
    OutfitPieceV2,
    OutfitSpecV2,
    PromptPackageV1,
    ReferenceBindingV1,
    WorkflowManifestV1,
    WorkflowSlotV1,
)
from reference_asset_gate import ReferenceAssetGate  # noqa: E402


class VersionedContractTests(unittest.TestCase):
    def test_structured_outfit_validates_topology_and_serializes(self):
        outfit = OutfitSpecV2(
            mode="free_outfit",
            category="homewear",
            top=OutfitPieceV2(kind="oversized t-shirt", color="mint green"),
            bottom=OutfitPieceV2(kind="lounge shorts", color="light gray"),
        )
        outfit.validate()
        self.assertIn("oversized t-shirt", " ".join(outfit.positive_tags()))
        with self.assertRaises(ContractValidationError):
            OutfitSpecV2(topology="one_piece").validate()

    def test_prompt_capability_reference_and_result_validators_fail_closed(self):
        with self.assertRaises(ContractValidationError):
            PromptPackageV1(positive_prompt="").validate()
        with self.assertRaises(ContractValidationError):
            BackendCapabilitiesV1(max_reference_images=-1).validate()
        with self.assertRaises(ContractValidationError):
            ReferenceBindingV1("ref", "/tmp/a.png", roles=("unknown",)).validate()
        with self.assertRaises(ContractValidationError):
            GenerationResultV1(image_path="/tmp/a.png", error_code="submission_failed").validate()

    def test_workflow_manifest_rejects_duplicate_node_inputs(self):
        slot = WorkflowSlotV1("positive_prompt", "1", "text", "string")
        manifest = WorkflowManifestV1(1, "wf", "abc", ("anima",), ("selfie",), (slot, slot))
        with self.assertRaises(ContractValidationError):
            manifest.validate()

    def test_managed_reference_ticket_is_one_shot_and_scope_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            asset_root = data / "photo_reference_assets"
            asset_root.mkdir()
            image = asset_root / "identity.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nidentity")
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            gate = ReferenceAssetGate(data)
            plan, status = gate.plan([{
                "id": "identity", "role": "identity", "file": "identity.png", "sha256": digest,
            }], generation_id="req-1", mode="new_topic")
            self.assertEqual("ok", status)
            self.assertIsNotNone(plan)
            ticket = gate.issue(plan, backend="comfyui")
            self.assertEqual(([], "scope_mismatch"), gate.consume(ticket, generation_id="req-2", backend="comfyui", capacity=1))
            paths, status = gate.consume(ticket, generation_id="req-1", backend="comfyui", capacity=1)
            self.assertEqual("ok", status)
            self.assertEqual([str(image)], paths)
            self.assertEqual(([], "expired_or_consumed_ticket"), gate.consume(ticket, generation_id="req-1", backend="comfyui", capacity=1))


if __name__ == "__main__":
    unittest.main()
