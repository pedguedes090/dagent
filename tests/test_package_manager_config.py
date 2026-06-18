from __future__ import annotations

import json
import unittest
from pathlib import Path


class PackageManagerConfigTests(unittest.TestCase):
    def test_repo_uses_pnpm_global_virtual_store(self) -> None:
        root = Path(__file__).resolve().parents[1]
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        workspace_config = (root / "pnpm-workspace.yaml").read_text(encoding="utf-8")

        self.assertTrue(str(package.get("packageManager", "")).startswith("pnpm@"))
        self.assertIn("enableGlobalVirtualStore: true", workspace_config)
        self.assertTrue((root / "pnpm-lock.yaml").exists())
        self.assertFalse((root / "package-lock.json").exists())


if __name__ == "__main__":
    unittest.main()
