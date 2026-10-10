"""The committed report is what the committed cache produces."""

from evals import report


def test_the_committed_results_are_regenerated_exactly_from_the_cache(tmp_path, monkeypatch):
    # The figures go to a scratch folder so that running the tests never touches docs/.
    monkeypatch.setattr(report, "IMAGES", tmp_path)

    text = report.build()

    assert text == report.RESULTS.read_text(encoding="utf-8")
    assert {path.name for path in tmp_path.iterdir()} == {
        "tradeoff.png",
        "reliability.png",
        "risk_coverage.png",
    }
