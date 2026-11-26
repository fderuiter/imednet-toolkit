from scripts.ci_plan import is_ignored_path, parse_dependency_name, plan_ci_impact


def test_parse_dependency_name_simple():
    assert parse_dependency_name("requests") == "requests"
    assert parse_dependency_name("imednet-workflows") == "imednet-workflows"
    assert parse_dependency_name("my.package_name-123") == "my.package_name-123"


def test_parse_dependency_name_with_version():
    assert parse_dependency_name("requests>=2.0.0") == "requests"
    assert parse_dependency_name("requests<=2.0.0") == "requests"
    assert parse_dependency_name("requests==2.0.0") == "requests"
    assert parse_dependency_name("requests~=2.0.0") == "requests"
    assert parse_dependency_name("requests!=2.0.0") == "requests"
    assert parse_dependency_name("requests > 2.0.0") == "requests"
    assert parse_dependency_name("imednet-workflows < 0.6.0") == "imednet-workflows"


def test_parse_dependency_name_with_extras():
    assert parse_dependency_name("requests[security]") == "requests"
    assert parse_dependency_name("requests[security,socks]") == "requests"
    assert parse_dependency_name("requests [security]") == "requests"


def test_parse_dependency_name_with_extras_and_version():
    assert parse_dependency_name("requests[security]>=2.0.0") == "requests"
    assert parse_dependency_name("requests[security,socks] == 2.0.0") == "requests"


def test_parse_dependency_name_with_environment_markers():
    assert parse_dependency_name("requests; python_version < '3.8'") == "requests"
    assert parse_dependency_name("requests ; python_version < '3.8'") == "requests"
    assert parse_dependency_name("requests>=2.0.0; sys_platform == 'win32'") == "requests"
    assert parse_dependency_name("requests[security]; sys_platform == 'win32'") == "requests"
    assert parse_dependency_name("requests[security]>=2.0.0; sys_platform == 'win32'") == "requests"


def test_parse_dependency_name_edge_cases():
    assert parse_dependency_name("  requests  ") == "requests"
    assert parse_dependency_name("A") == "A"
    assert parse_dependency_name("1") == "1"
    assert parse_dependency_name("foo.bar-baz_qux") == "foo.bar-baz_qux"


def test_is_ignored_path_exempts_agents_docs_examples_and_markdown():
    assert is_ignored_path(".agents/skills/workflow-guide/agents/openai.yaml") is True
    assert is_ignored_path(r".agents\skills\wizard\template.sh") is True
    assert is_ignored_path("./.agents/skills.json") is True
    assert is_ignored_path("docs/agents/issue-tracker.md") is True
    assert is_ignored_path("examples/demo.py") is True
    assert is_ignored_path("AGENTS.md") is True
    assert is_ignored_path("scripts/setup_agent_harnesses.py") is False


def test_plan_ci_impact_skips_packages_for_agents_only_changes():
    packages = {
        "core": {"name": "imednet", "deps": []},
        "plugins-workflows": {"name": "imednet-workflows", "deps": ["imednet"]},
    }
    result = plan_ci_impact(
        [
            ".agents/skills/triage/SKILL.md",
            ".agents/skills/triage/agents/openai.yaml",
            "AGENTS.md",
        ],
        packages,
    )
    assert result == {"test": [], "build": []}


def test_plan_ci_impact_falls_back_on_scripts_and_propagates_core_deps():
    packages = {
        "core": {"name": "imednet", "deps": []},
        "plugins-workflows": {"name": "imednet-workflows", "deps": ["imednet"]},
    }
    assert set(plan_ci_impact(["scripts/setup_agent_harnesses.py"], packages)["test"]) == {
        "core",
        "plugins-workflows",
    }
    core_change = plan_ci_impact(["packages/core/src/imednet/client.py"], packages)
    assert core_change["build"] == ["core"]
    assert set(core_change["test"]) == {"core", "plugins-workflows"}


def test_plan_ci_impact_maps_top_level_tests_without_building_wheels():
    packages = {
        "core": {"name": "imednet", "deps": []},
        "plugins-sinks": {"name": "imednet-plugins-sinks", "deps": ["imednet"]},
        "plugins-tui": {"name": "imednet-tui", "deps": ["imednet"]},
        "plugins-workflows": {"name": "imednet-workflows", "deps": ["imednet"]},
        "providers-airflow": {"name": "apache-airflow-providers-imednet", "deps": ["imednet"]},
    }

    assert plan_ci_impact(
        ["tests/unit/test_airflow_operators.py", "tests/integration/test_airflow_dag.py"],
        packages,
    ) == {
        "build": [],
        "test": ["providers-airflow"],
    }
    assert plan_ci_impact(
        [
            "tests/unit/test_workflows_data_extraction.py",
            "tests/unit/test_record_mapper.py",
            "tests/unit/test_state_ledger_providers.py",
            "tests/integration/test_sync_worker_integration.py",
            "tests/integration/test_workflows_integration.py",
        ],
        packages,
    ) == {
        "build": [],
        "test": ["plugins-workflows"],
    }
    assert plan_ci_impact(
        ["tests/unit/tui/test_app_structure.py", "tests/unit/cli/test_tui_cli.py"],
        packages,
    ) == {
        "build": [],
        "test": ["plugins-tui"],
    }
    assert plan_ci_impact(["tests/unit/test_client.py"], packages) == {
        "build": [],
        "test": ["core"],
    }
    assert plan_ci_impact(
        [
            "tests/unit/cli/test_cli_forms.py",
            "tests/unit/test_batch_validation_and_polling.py",
            "tests/unit/test_idempotent_syncs_and_sdtm_lineage.py",
            "tests/unit/test_keyed_upserts_and_watermark_recovery.py",
            "tests/unit/test_mode_safe_plugin_protocols.py",
            "tests/unit/test_scoped_sdtm_enrichment_standards_contracts.py",
            "tests/unit/test_spi_streaming_checkpoints.py",
            "tests/unit/test_tabular_import.py",
        ],
        packages,
    ) == {
        "build": [],
        "test": ["core", "plugins-workflows"],
    }
    assert plan_ci_impact(["tests/unit/test_airflow_cas_and_wildcards.py"], packages) == {
        "build": [],
        "test": ["plugins-workflows", "providers-airflow"],
    }
    assert plan_ci_impact(["tests/unit/test_sink_phi_resolution_unification.py"], packages) == {
        "build": [],
        "test": ["core", "plugins-sinks", "plugins-workflows"],
    }
    shared_test = plan_ci_impact(["tests/conftest.py"], packages)
    assert shared_test["build"] == []
    assert shared_test["test"] == list(packages.keys())
