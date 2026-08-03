import tempfile
import unittest
from pathlib import Path

from web_recipes import WebRecipeStore


class WebRecipeStoreTests(unittest.TestCase):
    def test_only_verified_recipe_is_saved_and_reuse_requires_revalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WebRecipeStore(Path(directory) / "recipes.sqlite3")
            saved = store.save_verified("search-v1", origin="https://example.test", kind="search",
                                        locators=[{"role": "textbox", "name": "Search"}],
                                        preconditions={"signed_in": False}, verifier={"result_count": 1},
                                        validators_passed=True)
            self.assertTrue(saved)
            self.assertTrue(store.candidate("search-v1")["requires_revalidation"])

    def test_two_failed_revalidations_disable_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WebRecipeStore(Path(directory) / "recipes.sqlite3")
            store.save_verified("search-v1", origin="https://example.test", kind="search",
                                locators=[{"ref": "e1"}], preconditions={}, verifier={}, validators_passed=True)
            store.record_revalidation("search-v1", False)
            self.assertIsNotNone(store.candidate("search-v1"))
            store.record_revalidation("search-v1", False)
            self.assertIsNone(store.candidate("search-v1"))

    def test_rejects_unverified_or_unsafe_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            store = WebRecipeStore(Path(directory) / "recipes.sqlite3")
            self.assertFalse(store.save_verified("bad", origin="file:///secret", kind="search", locators=[],
                                                 preconditions={}, verifier={}, validators_passed=False))


if __name__ == "__main__":
    unittest.main()
