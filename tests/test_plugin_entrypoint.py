from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

import yaml

ENTRYPOINT = Path(__file__).parents[1] / "__init__.py"
MANIFEST = ENTRYPOINT.with_name("plugin.yaml")


class PluginEntrypointTests(unittest.TestCase):
    def test_register_is_a_safe_noop(self):
        spec = importlib.util.spec_from_file_location("codex_limits_entrypoint", ENTRYPOINT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        self.assertIsNone(module.register(object()))

    def test_manifest_is_accepted_by_current_plugin_installer(self):
        from hermes_cli.plugins_cmd import _SUPPORTED_MANIFEST_VERSION

        manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        self.assertLessEqual(
            manifest["manifest_version"],
            _SUPPORTED_MANIFEST_VERSION,
        )


if __name__ == "__main__":
    unittest.main()
