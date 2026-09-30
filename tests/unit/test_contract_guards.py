from repopilot.agents.contracts import contract_violations, extract_normative_contracts


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
