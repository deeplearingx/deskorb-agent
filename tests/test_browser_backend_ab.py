from browser_backend_ab import evaluate_ab, evidence_accuracy


def _run(backend, case_id, *, status="completed_verified", actions=5, ledger=None):
    return {
        "browser_backend": backend,
        "case_id": case_id,
        "status": status,
        "action_steps": actions,
        "state_violations": 0,
        "forbidden_actions": 0,
        "real_site": True,
        "evidence_ledger": ledger or {"records": []},
    }


def test_case03_evidence_accuracy_requires_target_and_non_search_source():
    run = _run("browser_use", "case-03-conditional-4399", ledger={"records": [{
        "source_url": "https://www.example.com/game",
        "fields": {"title": "造梦西游", "url": "https://www.example.com/game"},
    }]})
    result = evidence_accuracy("case-03-conditional-4399", run)
    assert result["exact"] is True

    search_run = _run("browser_use", "case-03-conditional-4399", ledger={"records": [{
        "source_url": "https://www.baidu.com/s?wd=x",
        "fields": {"title": "造梦西游", "url": "https://www.baidu.com/ck/a"},
    }]})
    assert evidence_accuracy("case-03-conditional-4399", search_run)["exact"] is False


def test_case06_evidence_accuracy_counts_four_distinct_wikipedia_pages():
    records = []
    for slug in ("Artificial_intelligence", "Machine_learning", "Deep_learning", "Transformer"):
        records.append({
            "source_url": f"https://en.wikipedia.org/wiki/{slug}",
            "fields": {"title": slug, "url": f"https://en.wikipedia.org/wiki/{slug}", "excerpt": "first paragraph"},
        })
    result = evidence_accuracy("case-06-wikipedia-back", _run("browser_use", "case-06-wikipedia-back",
                                                                  ledger={"records": records}))
    assert result["matched"] == 4
    assert result["exact"] is True


def test_ab_decision_requires_browser_use_to_be_no_worse_and_strictly_better():
    runs = []
    for backend in ("playwright", "browser_use"):
        for case_id in ("case-03-conditional-4399", "case-06-wikipedia-back"):
            for _ in range(3):
                runs.append(_run(backend, case_id, actions=3 if backend == "browser_use" else 6))
    report = evaluate_ab(runs, repetitions=3)
    assert report["decision"] == "browser_use_candidate"
    assert report["backend_summary"]["browser_use"]["action_count_average"] == 3
