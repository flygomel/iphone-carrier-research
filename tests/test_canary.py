import json
from pathlib import Path
import tempfile
import unittest
import transport_canary as canary


class CanaryTests(unittest.TestCase):
    def test_every_postcondition_required(self):
        good = {k: True for k in ("stageSucceeded", "airTrafficSucceeded", "exactBytesRecovered",
                                 "cleanupComplete", "targetAbsent", "booksPreimageRestored")}
        self.assertTrue(canary.complete(good))
        for k in good:
            self.assertFalse(canary.complete({**good, k: False}), k)
            self.assertFalse(canary.complete({**good, k: 1}), k)

    def test_journal_preserves_intent_without_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "journal.jsonl"
            canary.Journal(path).append("native_intent", command="stage")
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(rows[-1]["event"], "native_intent")
            self.assertNotIn("completed", path.read_text())

    def test_missing_output_does_not_mean_success(self):
        self.assertFalse(canary.complete({}))


if __name__ == "__main__":
    unittest.main()
