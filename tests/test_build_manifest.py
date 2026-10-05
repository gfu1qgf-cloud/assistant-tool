import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


path = Path(__file__).resolve().parents[1] / "packaging" / "build_manifest.py"
spec = importlib.util.spec_from_file_location("release_build_manifest", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class BuildManifestTests(unittest.TestCase):
    def test_rejects_invalid_identity(self):
        for version, commit in (("latest", "a" * 40), ("v2.0.0", "bad")):
            with self.subTest(version=version, commit=commit):
                with self.assertRaises(ValueError):
                    module.make_manifest(version, commit)

    def test_records_identity_without_host_or_config(self):
        from types import SimpleNamespace
        fake_cv = SimpleNamespace(__version__="4.14.0", getBuildInformation=lambda: "  PNG: 1.6.57\n  avcodec: YES (61.19.100)\n")
        with patch.dict(sys.modules, {"cv2": fake_cv, "numpy": SimpleNamespace(__version__="2.2.6")}):
            result = module.make_manifest("v2.0.0", "a" * 40)
        self.assertEqual(result["source_commit"], "a" * 40)
        self.assertEqual(result["native_media"]["png"], "1.6.57")
        self.assertNotIn("hostname", result)
        self.assertNotIn("config", result)

    def test_old_native_stack_fails(self):
        from types import SimpleNamespace
        with patch.dict(sys.modules, {"cv2": SimpleNamespace(__version__="4.11.0"), "numpy": SimpleNamespace(__version__="1.26.4")}):
            with self.assertRaisesRegex(ValueError, "native stack"):
                module.make_manifest("v2.0.0", "a" * 40)


if __name__ == "__main__":
    unittest.main()
