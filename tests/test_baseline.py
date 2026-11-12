import json
import subprocess
import sys
import unittest
from pathlib import Path

import test_service
from demo import run


class OfflineDemoTests(unittest.TestCase):
    def test_demo_is_repeatable_and_exercises_existing_behavior(self):
        first = run()
        self.assertTrue(first)
        self.assertEqual(first, run())

    def test_documented_cli_outputs_the_same_data(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run([sys.executable, 'demo.py'], cwd=root, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), run())


def load_tests(loader, suite, pattern):
    suite.addTests(loader.loadTestsFromModule(test_service))
    return suite
