from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import retrieval_benchmark


class RetrievalBenchmarkTests(unittest.TestCase):
    def test_dataset_has_train_and_independent_holdout_queries(self) -> None:
        cases = retrieval_benchmark.benchmark_cases()
        self.assertGreaterEqual(len(cases), 100)
        self.assertLessEqual(len(cases), 200)
        self.assertTrue(any(case["split"] == "train" for case in cases))
        self.assertTrue(any(case["split"] == "holdout" for case in cases))
        self.assertTrue(any(case["expected"] is None for case in cases))
        self.assertTrue(any(case["kind"] == "scope" for case in cases))
        self.assertTrue(any(case["kind"] == "fixture-opt-in" for case in cases))

    def test_synthetic_corpus_reports_relevance_abstention_and_isolation(self) -> None:
        result = retrieval_benchmark.run_benchmark("all")
        self.assertEqual(result["benchmark"], "akb-synthetic-retrieval-v1")
        self.assertEqual(result["query_count"], len(retrieval_benchmark.benchmark_cases()))
        self.assertGreaterEqual(result["recall_at_3"], 0.75)
        self.assertGreaterEqual(result["no_answer_empty_rate"], 0.9)
        self.assertTrue(all(item["pass_rate"] == 1 for item in result["special_cases"].values()))
        self.assertTrue(any("Synthetic" in item for item in result["limitations"]))

    def test_ordered_cjk_matching_improves_train_and_holdout_recall(self) -> None:
        for split in ("train", "holdout"):
            with self.subTest(split=split):
                lexical = retrieval_benchmark.run_benchmark(split, cjk_fragments=False)
                improved = retrieval_benchmark.run_benchmark(split, cjk_fragments=True)
                self.assertGreater(improved["recall_at_3"], lexical["recall_at_3"])
                self.assertGreaterEqual(improved["no_answer_empty_rate"], lexical["no_answer_empty_rate"])


if __name__ == "__main__":
    unittest.main()
