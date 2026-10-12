"""`tollgate-init`: the starting files someone gets when they have no copy of the repository."""

import json
from importlib.resources import files

from fakes import EXAMPLE_POLICY

from tollgate.init import main, starting_policy
from tollgate.policy.schema import load_policy
from tollgate.proxy.upstreams import load_upstreams


def test_the_packaged_example_policy_is_the_one_in_the_repository():
    packaged = (files("tollgate") / "examples" / "policy.example.yaml").read_text(encoding="utf-8")

    assert packaged == EXAMPLE_POLICY.read_text(encoding="utf-8")


def test_init_writes_a_policy_and_an_upstreams_file_that_tollgate_accepts(tmp_path, capsys):
    assert main(["--dir", str(tmp_path)]) == 0

    policy = load_policy(tmp_path / "policy.yaml")
    upstreams = load_upstreams(tmp_path / "upstreams.json", env={})
    assert policy.thresholds == load_policy(EXAMPLE_POLICY).thresholds
    assert list(upstreams) == ["filesystem"]
    assert "tollgate --upstreams upstreams.json --policy policy.yaml" in capsys.readouterr().out


def test_the_written_files_confine_the_agent_to_the_folder_tollgate_runs_from(tmp_path):
    main(["--dir", str(tmp_path)])

    policy = load_policy(tmp_path / "policy.yaml")
    upstreams = json.loads((tmp_path / "upstreams.json").read_text(encoding="utf-8"))
    assert policy.prefilter.path_sandbox.roots == ["."]
    assert upstreams["mcpServers"]["filesystem"]["args"][-1] == "."
    # Nothing in the written policy points at a folder only the repository has.
    assert "examples/sandbox" not in starting_policy()


def test_init_keeps_the_comments_that_explain_each_setting(tmp_path):
    main(["--dir", str(tmp_path)])

    text = (tmp_path / "policy.yaml").read_text(encoding="utf-8")
    assert "# at or above this, a person decides" in text


def test_init_does_not_overwrite_an_existing_file_unless_told_to(tmp_path, capsys):
    (tmp_path / "policy.yaml").write_text("mine: true\n", encoding="utf-8")

    assert main(["--dir", str(tmp_path)]) == 1

    assert (tmp_path / "policy.yaml").read_text(encoding="utf-8") == "mine: true\n"
    assert not (tmp_path / "upstreams.json").exists()
    assert "policy.yaml already exists" in capsys.readouterr().err

    assert main(["--dir", str(tmp_path), "--force"]) == 0
    assert load_policy(tmp_path / "policy.yaml").decision.timeout_ms > 0
