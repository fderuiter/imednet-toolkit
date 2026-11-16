"""Unit tests for restricted AST evaluation engine."""

from __future__ import annotations

import logging
import time

import pytest

from imednet.errors import EvaluationError, RestrictedEvaluationError
from imednet.utils.evaluator import (
    RestrictedAstEvaluator,
    evaluate_expression,
    parse_expression,
)


class DummyObject:
    """Helper test class with public and private attributes."""

    def __init__(self, value: int = 42) -> None:
        self.value = value
        self._secret = "hidden"

    def double(self) -> int:
        return self.value * 2

    def _private_method(self) -> str:
        return "private"


def test_evaluator_literals() -> None:
    """Test literal constant evaluations."""
    assert evaluate_expression("42") == 42
    assert evaluate_expression("3.14") == 3.14
    assert evaluate_expression("'hello world'") == "hello world"
    assert evaluate_expression("True") is True
    assert evaluate_expression("False") is False
    assert evaluate_expression("None") is None


def test_evaluator_arithmetic_operators() -> None:
    """Test arithmetic binary and unary operators."""
    env = {"a": 10, "b": 3}
    assert evaluate_expression("a + b", env) == 13
    assert evaluate_expression("a - b", env) == 7
    assert evaluate_expression("a * b", env) == 30
    assert evaluate_expression("a / 2", env) == 5.0
    assert evaluate_expression("a // b", env) == 3
    assert evaluate_expression("a % b", env) == 1
    assert evaluate_expression("2 ** 3", env) == 8
    assert evaluate_expression("a | b", env) == 11
    assert evaluate_expression("a & b", env) == 2
    assert evaluate_expression("a ^ b", env) == 9
    assert evaluate_expression("-a", env) == -10
    assert evaluate_expression("+a", env) == 10
    assert evaluate_expression("~a", env) == -11


def test_evaluator_arithmetic_guards() -> None:
    """Test arithmetic error and safety guards."""
    with pytest.raises(EvaluationError, match="Division by zero"):
        evaluate_expression("10 / 0", raise_on_error=True)

    with pytest.raises(EvaluationError, match="Integer division by zero"):
        evaluate_expression("10 // 0", raise_on_error=True)

    with pytest.raises(EvaluationError, match="Modulo by zero"):
        evaluate_expression("10 % 0", raise_on_error=True)

    with pytest.raises(EvaluationError, match="Exponent too large"):
        evaluate_expression("10 ** 10000", raise_on_error=True)


def test_evaluator_boolean_and_short_circuit() -> None:
    """Test boolean operators and verify short-circuiting."""
    called: list[str] = []

    class Tracker:
        @property
        def side_effect(self) -> bool:
            called.append("accessed")
            return True

    env = {"obj": Tracker()}
    # False and ... should not evaluate the second operand
    assert evaluate_expression("False and obj.side_effect", env) is False
    assert len(called) == 0

    # True or ... should not evaluate the second operand
    assert evaluate_expression("True or obj.side_effect", env) is True
    assert len(called) == 0

    assert evaluate_expression("not False") is True
    assert evaluate_expression("not True") is False


def test_evaluator_comparisons_and_chaining() -> None:
    """Test comparison operators and chained comparisons."""
    assert evaluate_expression("1 < 2 < 3") is True
    assert evaluate_expression("1 < 5 < 3") is False
    assert evaluate_expression("5 == 5") is True
    assert evaluate_expression("5 != 6") is True
    assert evaluate_expression("5 <= 5") is True
    assert evaluate_expression("5 >= 4") is True
    assert evaluate_expression("'a' in ['a', 'b']") is True
    assert evaluate_expression("'c' not in ['a', 'b']") is True
    assert evaluate_expression("None is None") is True
    assert evaluate_expression("5 is not None") is True


def test_evaluator_conditional_ternary() -> None:
    """Test if-else ternary expression with short-circuiting."""
    called: list[str] = []

    class Tracker:
        @property
        def boom(self) -> int:
            called.append("boom")
            return 999

    env = {"t": Tracker()}
    assert evaluate_expression("'yes' if 10 > 5 else t.boom", env) == "yes"
    assert len(called) == 0

    assert evaluate_expression("t.boom if 10 < 5 else 'no'", env) == "no"
    assert len(called) == 0


def test_evaluator_containers_and_subscripts() -> None:
    """Test list, tuple, dict, set literals and subscript access."""
    env = {
        "items": [10, 20, 30, 40],
        "mapping": {"key": "val", "nested": {"count": 7}},
    }
    assert evaluate_expression("[1, 2, 3]") == [1, 2, 3]
    assert evaluate_expression("(1, 2)") == (1, 2)
    assert evaluate_expression("{'a': 1, 'b': 2}") == {"a": 1, "b": 2}
    assert evaluate_expression("{1, 2, 3}") == {1, 2, 3}
    assert evaluate_expression("items[1]", env) == 20
    assert evaluate_expression("items[1:3]", env) == [20, 30]
    assert evaluate_expression("mapping['key']", env) == "val"
    assert evaluate_expression("mapping['nested']['count']", env) == 7


def test_evaluator_allowed_primitive_functions() -> None:
    """Test allowlisted functions: float, int, str, bool, len."""
    env = {"num_str": "123", "val": 42.9, "seq": [1, 2, 3, 4]}
    assert evaluate_expression("float(num_str)", env) == 123.0
    assert evaluate_expression("int(val)", env) == 42
    assert evaluate_expression("str(val)", env) == "42.9"
    assert evaluate_expression("bool(seq)", env) is True
    assert evaluate_expression("len(seq)", env) == 4


def test_evaluator_safe_method_calls() -> None:
    """Test calling safe methods on strings and dictionaries."""
    env = {
        "text": "   Clean Me   ",
        "d": {"status": "ACTIVE"},
    }
    assert evaluate_expression("text.strip().lower()", env) == "clean me"
    assert evaluate_expression("d.get('status', 'DEFAULT')", env) == "ACTIVE"
    assert evaluate_expression("d.get('missing', 'FALLBACK')", env) == "FALLBACK"


def test_evaluator_rejects_unapproved_method_calls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that calling unapproved or mutator methods raises RestrictedEvaluationError."""
    env = {
        "payload": {"record_id": 100, "patient": "John"},
        "obj": DummyObject(25),
    }

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RestrictedEvaluationError, match="Method 'clear' is forbidden"):
            evaluate_expression("payload.clear()", env=env, raise_on_error=True)

    assert any("Security warning: Method 'clear' is forbidden" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RestrictedEvaluationError, match="Method 'pop' is forbidden"):
            evaluate_expression("payload.pop('patient')", env=env, raise_on_error=True)

    assert any("Security warning: Method 'pop' is forbidden" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RestrictedEvaluationError, match="Method 'double' is forbidden"):
            evaluate_expression("obj.double()", env=env, raise_on_error=True)

    assert any(
        "Security warning: Method 'double' is forbidden" in r.message for r in caplog.records
    )

    # Verify payload remained intact and unmutated
    assert env["payload"] == {"record_id": 100, "patient": "John"}


def test_evaluator_rejects_private_attribute_access(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that accessing private attributes starting with underscore is rejected."""
    env = {"obj": DummyObject()}

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RestrictedEvaluationError, match="Access to private attribute"):
            evaluate_expression("obj._secret", env, raise_on_error=True)

    assert any("Security warning" in r.message for r in caplog.records)


def test_evaluator_rejects_private_method_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that calling private methods starting with underscore is rejected."""
    env = {"obj": DummyObject()}

    with caplog.at_level(logging.WARNING):
        with pytest.raises(
            RestrictedEvaluationError, match="Method call '_private_method' is forbidden"
        ):
            evaluate_expression("obj._private_method()", env, raise_on_error=True)

    assert any("Security warning" in r.message for r in caplog.records)


def test_evaluator_rejects_private_identifiers() -> None:
    """Test that name lookups starting with underscore are rejected."""
    with pytest.raises(RestrictedEvaluationError, match="Access to private identifier"):
        evaluate_expression("_hidden_var", {"_hidden_var": 123}, raise_on_error=True)


def test_evaluator_rejects_unapproved_functions(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that non-allowlisted function calls are blocked."""
    with caplog.at_level(logging.WARNING):
        with pytest.raises(RestrictedEvaluationError, match="Function call 'open' is forbidden"):
            evaluate_expression("open('/etc/passwd')", raise_on_error=True)

    with pytest.raises(RestrictedEvaluationError, match="Function call 'eval' is forbidden"):
        evaluate_expression("eval('1 + 1')", raise_on_error=True)

    with pytest.raises(RestrictedEvaluationError, match="Function call 'exec' is forbidden"):
        evaluate_expression("exec('x = 1')", raise_on_error=True)


def test_evaluator_rejects_class_hierarchy_traversal_payloads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that classic Python class hierarchy traversal RCE payloads are blocked."""
    payloads = [
        "''.__class__",
        "().__class__.__bases__",
        "(1).__class__.__mro__[1].__subclasses__()",
        "value.__class__.__base__.__subclasses__()",
        "str.__init__",
        "int.__subclasses__()",
    ]

    for payload in payloads:
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            with pytest.raises(RestrictedEvaluationError):
                evaluate_expression(payload, {"value": 42}, raise_on_error=True)
            assert any("Security warning" in r.message for r in caplog.records)


def test_evaluator_rejects_complex_rce_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that complex multi-step RCE payloads fail safely without executing."""
    payload = (
        "[c for c in ().__class__.__bases__[0].__subclasses__() "
        "if c.__name__ == 'catch_warnings'][0]()._module.__builtins__['__import__']('os').system('id')"
    )

    with caplog.at_level(logging.WARNING):
        with pytest.raises(RestrictedEvaluationError):
            evaluate_expression(payload, raise_on_error=True)

    assert any("Security warning" in r.message for r in caplog.records)


def test_evaluator_rejects_forbidden_syntax_nodes() -> None:
    """Test rejection of statements, lambdas, generator expressions, and comprehensions."""
    # Statements
    with pytest.raises(EvaluationError):
        evaluate_expression("x = 1; x", raise_on_error=True)

    with pytest.raises(EvaluationError):
        evaluate_expression("import os", raise_on_error=True)

    # Lambdas
    with pytest.raises(RestrictedEvaluationError, match="AST node 'Lambda' is forbidden"):
        evaluate_expression("lambda x: x + 1", raise_on_error=True)

    with pytest.raises(RestrictedEvaluationError, match="Call target 'Lambda' is forbidden"):
        evaluate_expression("(lambda x: x + 1)(5)", raise_on_error=True)

    # Generator expressions
    with pytest.raises(RestrictedEvaluationError, match="AST node 'GeneratorExp' is forbidden"):
        evaluate_expression("(x for x in [1, 2, 3])", raise_on_error=True)

    # List comprehensions
    with pytest.raises(RestrictedEvaluationError, match="AST node 'ListComp' is forbidden"):
        evaluate_expression("[x * 2 for x in [1, 2, 3]]", raise_on_error=True)


def test_evaluator_fallback_and_default_handling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test fallback and default value behavior when raise_on_error=False."""
    with caplog.at_level(logging.WARNING):
        res1 = evaluate_expression("invalid +++ syntax", default="FALLBACK")
    assert res1 == "FALLBACK"

    with caplog.at_level(logging.WARNING):
        res2 = evaluate_expression("obj.__class__", env={"obj": 123}, fallback="DEFAULT_VAL")
    assert res2 == "DEFAULT_VAL"

    with caplog.at_level(logging.WARNING):
        res3 = evaluate_expression("1 / 0", default=0)
    assert res3 == 0


def test_evaluator_empty_expression() -> None:
    """Test that empty or whitespace expressions return default safely."""
    assert evaluate_expression("", default="N/A") == "N/A"
    assert evaluate_expression("   ", default=None) is None
    with pytest.raises(EvaluationError, match="Expression string cannot be empty"):
        evaluate_expression("   ", raise_on_error=True)


def test_restricted_ast_evaluator_class() -> None:
    """Test the RestrictedAstEvaluator object-oriented wrapper."""
    evaluator = RestrictedAstEvaluator("float(x) * 2 + 1")
    assert evaluator.evaluate({"x": "4.5"}) == 10.0
    assert evaluator.evaluate({"x": 10}) == 21.0


def test_evaluator_performance_under_5ms_per_record() -> None:
    """Verify evaluation speed constraint: must execute in < 5ms per record."""
    expr = "float(payload.get('result', 0)) * 2.5 + state.get('offset', 1.0)"
    tree = parse_expression(expr)

    payload = {"result": 42.0}
    state = {"offset": 5.0}
    env = {"payload": payload, "state": state}

    iterations = 1000
    start = time.perf_counter()
    for _ in range(iterations):
        evaluate_expression(tree, env)
    elapsed = time.perf_counter() - start

    time_per_eval_ms = (elapsed / iterations) * 1000.0
    # Must be less than 5ms (typically < 0.05ms)
    assert time_per_eval_ms < 5.0, f"Evaluation was too slow: {time_per_eval_ms:.3f} ms/record"


def test_ast_eval_module_reexports() -> None:
    """Test that imednet.utils.ast_eval re-exports symbols correctly."""
    from imednet.utils import ast_eval

    assert callable(ast_eval.evaluate_expression)
    assert callable(ast_eval.parse_expression)
    assert issubclass(ast_eval.RestrictedAstEvaluator, object)
    assert "float" in ast_eval.DEFAULT_SAFE_FUNCTIONS
    assert "get" in ast_eval.DEFAULT_SAFE_METHODS
    assert "strip" in ast_eval.DEFAULT_SAFE_METHODS


def test_evaluator_dict_unpacking_forbidden() -> None:
    """Test that dictionary unpacking '**' is rejected."""
    with pytest.raises(RestrictedEvaluationError, match="Dictionary unpacking is forbidden"):
        evaluate_expression("{**d}", {"d": {"a": 1}}, raise_on_error=True)


def test_evaluator_keyword_arguments_and_safety() -> None:
    """Test function calls with keyword arguments and underscore guard."""
    assert evaluate_expression("int('ff', base=16)") == 255
    with pytest.raises(RestrictedEvaluationError, match="Keyword argument '_secret'"):
        evaluate_expression("int('10', _secret=1)", raise_on_error=True)


def test_evaluator_type_and_module_attribute_access_forbidden() -> None:
    """Test that accessing attributes on type or module objects is rejected."""
    import math

    with pytest.raises(RestrictedEvaluationError, match="Access to attributes on type/module"):
        evaluate_expression("mod.sqrt", {"mod": math}, raise_on_error=True)

    with pytest.raises(RestrictedEvaluationError, match="Method calls on type/module"):
        evaluate_expression("mod.sqrt(4)", {"mod": math}, raise_on_error=True)


def test_evaluator_attribute_and_method_errors() -> None:
    """Test missing attribute, missing method, and non-callable method errors."""
    dummy = DummyObject(10)
    with pytest.raises(EvaluationError, match="has no attribute 'not_found'"):
        evaluate_expression("obj.not_found", {"obj": dummy}, raise_on_error=True)

    with pytest.raises(EvaluationError, match="has no method 'not_found'"):
        evaluate_expression("obj.not_found()", {"obj": dummy}, raise_on_error=True)

    with pytest.raises(EvaluationError, match="is not callable"):
        evaluate_expression("obj.value()", {"obj": dummy}, raise_on_error=True)


def test_evaluator_slice_with_step() -> None:
    """Test slicing with start, stop, and step."""
    assert evaluate_expression("items[0:4:2]", {"items": [10, 20, 30, 40, 50]}) == [10, 30]


def test_evaluator_invalid_argument_types() -> None:
    """Test invalid argument types to evaluate_expression."""
    with pytest.raises(EvaluationError, match="Expected str or ast.AST"):
        evaluate_expression(12345, raise_on_error=True)  # type: ignore[arg-type]


def test_evaluator_subscript_and_name_errors() -> None:
    """Test subscript KeyError and undefined Name error."""
    with pytest.raises(EvaluationError, match="Subscript lookup failed"):
        evaluate_expression("d['missing']", {"d": {}}, raise_on_error=True)

    with pytest.raises(EvaluationError, match="Name 'missing_var' is not defined"):
        evaluate_expression("missing_var", {}, raise_on_error=True)


def test_evaluator_function_call_execution_error() -> None:
    """Test error raised when an allowed function raises an exception."""
    with pytest.raises(EvaluationError, match="Error calling function 'int'"):
        evaluate_expression("int('not-an-int')", raise_on_error=True)


def test_evaluator_ast_preparsed_input() -> None:
    """Test evaluate_expression accepting a pre-parsed ast.Expression."""
    tree = parse_expression("a + b")
    assert evaluate_expression(tree, {"a": 1, "b": 2}) == 3
