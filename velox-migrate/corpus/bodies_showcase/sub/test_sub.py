"""The subtree's own tests, which resolve the copies rather than the fixtures at the root."""


def test_report_reads_the_subtrees_own_list(report):
    assert report["seen"] == ["sub"]
    assert report["dsn"] == "sqlite:///bodies"
