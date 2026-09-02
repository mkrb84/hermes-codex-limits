from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ENTRYPOINT = Path(__file__).parents[1] / "__init__.py"


class PluginEntrypointTests(unittest.TestCase):
    def test_register_is_a_safe_noop(self):
        spec = importlib.util.spec_from_file_location("codex_limits_entrypoint", ENTRYPOINT)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        self.assertIsNone(module.register(object()))


if __name__ == "__main__":
    unittest.main()
