import unittest

from browser_actions import _normalize_field_names
from browser_evidence import BrowserEvidenceLedger, extract_list_from_snapshot


class BrowserEvidenceExtractionTests(unittest.TestCase):
    def test_repeated_semantic_cards_are_not_swallowed_by_generic_wrapper(self):
        content = [{"type": "text", "text": """### Snapshot
- generic [ref=page-root]:
  - generic [ref=results-wrapper]:
    - article [ref=card-1]:
      - heading \"First project\" [ref=title-1]
      - link [ref=repo-1]: \"repository\"
        - /url: https://github.com/example/first
    - article [ref=card-2]:
      - heading \"Second project\" [ref=title-2]
      - link [ref=repo-2]: \"repository\"
        - /url: https://github.com/example/second
"""}]

        result = extract_list_from_snapshot(
            content, ["title", "url"], limit=20,
            page_url="https://github.com/trending", observation_id="obs-1",
        )

        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [item["fields"]["title"] for item in result["items"]],
            ["First project", "Second project"],
        )

    def test_list_extraction_skips_github_auth_and_sponsor_controls(self):
        content = [{"type": "text", "text": """### Page
- Page URL: https://github.com/trending/python
### Snapshot
```yaml
- generic [ref=root]:
  - article [ref=auth]:
    - link [ref=auth-link]: "You must be signed in to star a repository"
      - /url: https://github.com/login
  - article [ref=project]:
    - link [ref=project-link]: "Example Project"
      - /url: https://github.com/example/project
  - article [ref=sponsor]:
    - link [ref=sponsor-link]: "Sponsor @example"
      - /url: https://github.com/sponsors/example
```"""}]

        result = extract_list_from_snapshot(
            content, ["title", "url"], limit=5, unique_by=["url"],
            page_url="https://github.com/trending/python",
        )

        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["fields"]["title"], "Example Project")

    def test_issue_title_projection_keeps_requested_semantic_field(self):
        content = [{"type": "text", "text": """### Snapshot
- article [ref=issue]:
  - link [ref=issue-link]: "external"
    - /url: https://github.com/example/project/issues/1
"""}]

        result = extract_list_from_snapshot(
            content, ["issue_title", "url"], limit=3,
            page_url="https://github.com/example/project/issues",
        )

        self.assertEqual(result["items"][0]["fields"]["issue_title"], "external")

    def test_issue_source_context_aliases_generic_title_to_issue_title(self):
        ledger = BrowserEvidenceLedger()
        ledger.add(
            kind="list_item", tab_id="tab-1", observation_id="obs-1",
            source_url="https://github.com/example/project/issues",
            fields={"title": "external", "url": "https://github.com/example/project/issues/1"},
        )

        self.assertEqual(ledger.records[0].fields["issue_title"], "external")

    def test_issue_fields_merge_into_matching_repository_record_without_removing_rows(self):
        ledger = BrowserEvidenceLedger()
        ledger.add(
            kind="github_page_extract", tab_id="tab-1", observation_id="obs-1",
            source_url="https://github.com/example/project",
            fields={"title": "project", "installation": "pip install project"},
        )
        ledger.add(
            kind="list_item", tab_id="tab-2", observation_id="obs-2",
            source_url="https://github.com/example/project/issues",
            fields={"issue_title": "external", "url": "https://github.com/example/project/issues/1"},
        )

        merged = ledger.merge_repository_fields(
            source_url="https://github.com/example/project/issues",
            fields={"issue_title": "external"},
        )

        self.assertTrue(merged)
        self.assertEqual(len(ledger.records), 2)
        self.assertEqual(ledger.records[0].fields["issue_title"], "external")
        self.assertEqual(ledger.records[1].fields["issue_title"], "external")

    def test_github_issue_list_prefers_issue_number_rows_over_sidebar_listitems(self):
        content = [{"type": "text", "text": """### Page
- Page URL: https://github.com/example/project/issues
### Snapshot
- list [ref=sidebar]:
  - listitem [ref=repo-nav]: \"project\"
    - link [ref=repo-link]: \"project\"
      - /url: https://github.com/example/project
  - listitem [ref=issues-nav]: \"Issues\"
    - link [ref=issues-link]: \"Issues\"
      - /url: https://github.com/example/project/issues
- list [ref=issue-list]:
  - listitem [ref=issue-1]:
    - link [ref=issue-link-1]: \"First real issue\"
      - /url: https://github.com/example/project/issues/101
  - listitem [ref=issue-2]:
    - link [ref=issue-link-2]: \"Second real issue\"
      - /url: https://github.com/example/project/issues/100
"""}]

        result = extract_list_from_snapshot(
            content, ["issue_title", "url"], limit=3, unique_by=["url"],
            page_url="https://github.com/example/project/issues", observation_id="obs-1",
        )

        self.assertEqual(result["count"], 2)
        self.assertEqual(
            [item["fields"]["issue_title"] for item in result["items"]],
            ["First real issue", "Second real issue"],
        )
        self.assertTrue(all("/issues/" in item["fields"]["url"] for item in result["items"]))

    def test_action_field_normalization_preserves_issue_title_semantics(self):
        self.assertEqual(_normalize_field_names(["issue_title", "installation_command"]),
                         ["issue_title", "installation"])


if __name__ == "__main__":
    unittest.main()
