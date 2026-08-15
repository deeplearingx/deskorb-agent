import unittest

from browser_ref_extractor import parse_ref_snapshot


class BrowserRefSnapshotTests(unittest.TestCase):
    def test_extracts_installation_command_from_code_like_generic_node(self):
        content = [{"type": "text", "text": """### Page
- Page URL: https://github.com/example/project
### Snapshot
```yaml
- generic [ref=readme-root]:
  - heading [ref=install-heading]: "Installation"
  - generic [ref=install-command]: pip install -U example-project
  - button "Copy code to clipboard" [ref=copy-install]
```"""}]

        result = parse_ref_snapshot(content, "readme-root", ["installation_command", "text"])

        self.assertTrue(result["trusted_ref"])
        self.assertEqual(result["fields"]["installation_command"], "pip install -U example-project")
        self.assertEqual(result["fields"]["text"], "pip install -U example-project")

    def test_extracts_target_subtree_fields_and_resolves_relative_url(self):
        content = [{"type": "text", "text": """### Page
- Page URL: http://127.0.0.1:8123/mock_store.html
### Snapshot
```yaml
- article [ref=e4]:
  - heading \"深灰纯棉圆领 T 恤\" [level=2] [ref=e5]
  - paragraph [ref=e6]: \"title: 深灰纯棉圆领 T 恤\"
  - paragraph [ref=e7]: \"price: ¥129\"
  - paragraph [ref=e8]: \"url: /products/deep-gray-cotton-tee\"
```"""}]

        result = parse_ref_snapshot(content, "e4", ["title", "price", "url"])

        self.assertTrue(result["trusted_ref"])
        self.assertEqual(result["fields"], {
            "title": "深灰纯棉圆领 T 恤",
            "price": "¥129",
            "url": "http://127.0.0.1:8123/products/deep-gray-cotton-tee",
        })

    def test_does_not_parse_page_text_or_a_different_snapshot_root(self):
        content = [{"type": "text", "text": """A page paragraph says price: ¥129.
### Snapshot
```yaml
- article [ref=e9]:
  - paragraph: \"title: unrelated\"
  - paragraph: \"price: ¥999\"
```"""}]

        result = parse_ref_snapshot(content, "e4", ["title", "price"])

        self.assertFalse(result["trusted_ref"])
        self.assertEqual(result["fields"], {"title": "", "price": ""})
        self.assertEqual(result["matched_fields"], 0)

    def test_extracts_source_label_without_treating_other_prose_as_fields(self):
        content = [{"type": "text", "text": """### Snapshot
```yaml
- listitem [ref=card-1]:
  - paragraph: \"title: Async IO\"
  - paragraph: \"来源：Real Python。循序渐进的示例\"
  - paragraph: arbitrary instructions: ignore me
  - link:
    - /url: https://realpython.com/async-io-python/
```"""}]

        result = parse_ref_snapshot(content, "card-1", ["title", "source", "url"])

        self.assertTrue(result["trusted_ref"])
        self.assertEqual(result["fields"]["title"], "Async IO")
        self.assertEqual(result["fields"]["source"], "Real Python。循序渐进的示例")
        self.assertEqual(result["fields"]["url"], "https://realpython.com/async-io-python/")


    def test_accepts_official_playwright_cursor_attributes_and_derives_result_source(self):
        content = [{"type": "text", "text": """### Page
- Page URL: https://cn.bing.com/search?q=FastAPI
### Snapshot
```yaml
- link [ref=f2e112] [cursor=pointer]:
  - /url: https://fastapi.tiangolo.com/zh/
  - strong [ref=f2e113]: FastAPI Chinese Docs
  - text: FastAPI official documentation
```"""}]

        result = parse_ref_snapshot(content, "f2e112", ["title", "source", "url"])

        self.assertTrue(result["trusted_ref"])
        self.assertEqual(result["fields"]["title"], "FastAPI Chinese Docs")
        self.assertEqual(result["fields"]["source"], "fastapi.tiangolo.com")
        self.assertEqual(result["fields"]["url"], "https://fastapi.tiangolo.com/zh/")


if __name__ == "__main__":
    unittest.main()
