from repopilot.agents.contracts import (
    contract_violations,
    extract_normative_contracts,
    patch_hygiene_violations,
)


def test_explicit_exception_propagation_contract_blocks_conflicting_catch() -> None:
    contracts = extract_normative_contracts({
        "parser.py": (
            'def convert(value):\n'
            '    """TypeError and unrelated exceptions must propagate."""\n'
            '    return int(value)\n'
        ),
    })
    diff = """diff --git a/parser.py b/parser.py
--- a/parser.py
+++ b/parser.py
@@ -1 +1 @@
-    except ValueError:
+    except (TypeError, ValueError):
"""

    violations = contract_violations(diff, contracts)

    assert len(violations) == 1
    assert violations[0]["symbol"] == "TypeError"
    assert violations[0]["contract_file"] == "parser.py"


def test_contract_guard_allows_catch_not_forbidden_by_source_contract() -> None:
    contracts = extract_normative_contracts({
        "parser.py": '"""TypeError must propagate."""\n',
    })
    diff = """diff --git a/parser.py b/parser.py
--- a/parser.py
+++ b/parser.py
@@ -1 +1 @@
-    except TypeError:
+    except ValueError:
"""

    assert contract_violations(diff, contracts) == []


def test_patch_hygiene_blocks_temporary_artifacts_and_unrequested_test_changes() -> None:
    diff = """diff --git a/run_probe.py b/run_probe.py
new file mode 100644
--- /dev/null
+++ b/run_probe.py
@@ -0,0 +1 @@
+print('probe')
diff --git a/tests/test_parser.py b/tests/test_parser.py
--- a/tests/test_parser.py
+++ b/tests/test_parser.py
@@ -1 +1 @@
-assert parse(False) == 'False'
+assert parse(False) == False
"""

    violations = patch_hygiene_violations(diff, allow_test_changes=False)

    assert {(item["kind"], item["file"]) for item in violations} == {
        ("temporary_artifact", "run_probe.py"),
        ("unrequested_test_change", "tests/test_parser.py"),
    }


def test_patch_hygiene_allows_requested_test_changes_and_product_files() -> None:
    diff = """diff --git a/parser.py b/parser.py
--- a/parser.py
+++ b/parser.py
diff --git a/tests/test_parser.py b/tests/test_parser.py
--- a/tests/test_parser.py
+++ b/tests/test_parser.py
"""
    assert patch_hygiene_violations(diff, allow_test_changes=True) == []
