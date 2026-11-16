"""Restricted AST evaluation engine for safe study business logic execution."""

from __future__ import annotations

import ast
import functools
import logging
import types
from collections.abc import Callable
from typing import Any

from imednet.errors import EvaluationError, RestrictedEvaluationError

logger = logging.getLogger(__name__)

# Default allowed callable functions (primitives only)
DEFAULT_SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "float": float,
    "int": int,
    "str": str,
    "bool": bool,
    "len": len,
}

# Default allowed method names (safe read-only operations only)
DEFAULT_SAFE_METHODS: set[str] = {
    # String inspection & transformation (immutable)
    "capitalize",
    "casefold",
    "center",
    "count",
    "decode",
    "encode",
    "endswith",
    "find",
    "format",
    "format_map",
    "index",
    "isalnum",
    "isalpha",
    "isascii",
    "isdecimal",
    "isdigit",
    "isidentifier",
    "islower",
    "isnumeric",
    "isprintable",
    "isspace",
    "istitle",
    "isupper",
    "join",
    "ljust",
    "lower",
    "lstrip",
    "partition",
    "removeprefix",
    "removesuffix",
    "replace",
    "rfind",
    "rindex",
    "rjust",
    "rpartition",
    "rsplit",
    "rstrip",
    "split",
    "splitlines",
    "startswith",
    "strip",
    "swapcase",
    "title",
    "upper",
    "zfill",
    # Dict read-only accessors
    "get",
    "items",
    "keys",
    "values",
    # Sequence / Set / Container non-mutating operations
    "copy",
    "difference",
    "intersection",
    "isdisjoint",
    "issubset",
    "issuperset",
    "symmetric_difference",
    "union",
    # Numeric methods
    "as_integer_ratio",
    "bit_count",
    "bit_length",
    "conjugate",
    "hex",
    "is_integer",
    # Date / Time formatting & extraction
    "date",
    "isoformat",
    "strftime",
    "time",
    "timestamp",
    "weekday",
    "isoweekday",
}

# Allowlist of permitted AST node types
ALLOWED_NODE_TYPES: set[type[ast.AST]] = {
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    # Unary & Binary operators
    ast.UnaryOp,
    ast.UAdd,
    ast.USub,
    ast.Not,
    ast.Invert,
    ast.BinOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.BitOr,
    ast.BitAnd,
    ast.BitXor,
    # Boolean operators
    ast.BoolOp,
    ast.And,
    ast.Or,
    # Comparison operators
    ast.Compare,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Is,
    ast.IsNot,
    ast.In,
    ast.NotIn,
    # Conditional ternary
    ast.IfExp,
    # Subscript & Slice
    ast.Subscript,
    ast.Slice,
    # Containers
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    # Calls & Attributes
    ast.Call,
    ast.keyword,
    ast.Attribute,
}


def validate_ast(node: ast.AST, allowed_functions: set[str] | None = None) -> None:
    """Validate that an AST contains only safe and permitted syntax nodes.

    Args:
        node: The AST node or tree to validate.
        allowed_functions: Optional set of allowed callable function names.

    Raises:
        RestrictedEvaluationError: If an unsafe node, private attribute,
            or unapproved callable is detected.
    """
    safe_fn_names = (
        allowed_functions if allowed_functions is not None else set(DEFAULT_SAFE_FUNCTIONS.keys())
    )

    for n in ast.walk(node):
        if type(n) not in ALLOWED_NODE_TYPES:
            msg = f"AST node '{type(n).__name__}' is forbidden in restricted expressions"
            logger.warning(f"Security warning: {msg}")
            raise RestrictedEvaluationError(msg)

        if isinstance(n, ast.Name):
            if n.id.startswith("_"):
                msg = f"Access to private identifier '{n.id}' is forbidden"
                logger.warning(f"Security warning: {msg}")
                raise RestrictedEvaluationError(msg)

        elif isinstance(n, ast.Attribute):
            if n.attr.startswith("_"):
                msg = f"Access to private attribute '{n.attr}' is forbidden"
                logger.warning(f"Security warning: {msg}")
                raise RestrictedEvaluationError(msg)

        elif isinstance(n, ast.Dict):
            if any(k is None for k in n.keys):
                msg = "Dictionary unpacking is forbidden"
                logger.warning(f"Security warning: {msg}")
                raise RestrictedEvaluationError(msg)

        elif isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                func_name = n.func.id
                if func_name.startswith("_") or func_name not in safe_fn_names:
                    msg = f"Function call '{func_name}' is forbidden"
                    logger.warning(f"Security warning: {msg}")
                    raise RestrictedEvaluationError(msg)
            elif isinstance(n.func, ast.Attribute):
                if n.func.attr.startswith("_"):
                    msg = f"Method call '{n.func.attr}' is forbidden"
                    logger.warning(f"Security warning: {msg}")
                    raise RestrictedEvaluationError(msg)
            else:
                msg = f"Call target '{type(n.func).__name__}' is forbidden"
                logger.warning(f"Security warning: {msg}")
                raise RestrictedEvaluationError(msg)

            for kw in n.keywords:
                if kw.arg and kw.arg.startswith("_"):
                    msg = f"Keyword argument '{kw.arg}' starting with underscore is forbidden"
                    logger.warning(f"Security warning: {msg}")
                    raise RestrictedEvaluationError(msg)


@functools.lru_cache(maxsize=1024)
def parse_expression(expr: str) -> ast.Expression:
    """Parse and validate an expression string into a restricted AST.

    Args:
        expr: Python expression string to parse.

    Returns:
        The validated ast.Expression tree.

    Raises:
        EvaluationError: If parsing fails due to syntax error or empty input.
        RestrictedEvaluationError: If forbidden syntax or attributes are found.
    """
    if not isinstance(expr, str):
        raise EvaluationError(f"Expression must be a string, got {type(expr).__name__}")

    stripped = expr.strip()
    if not stripped:
        raise EvaluationError("Expression string cannot be empty")

    try:
        tree = ast.parse(stripped, mode="eval")
    except SyntaxError as exc:
        raise EvaluationError(f"Syntax error parsing expression: {exc}") from exc

    validate_ast(tree)
    return tree


def _eval_node(node: ast.AST, env: dict[str, Any]) -> Any:
    """Recursively evaluate an AST node within the given environment."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.Name):
        name = node.id
        if name.startswith("_"):
            msg = f"Access to private identifier '{name}' is forbidden"
            logger.warning(f"Security warning: {msg}")
            raise RestrictedEvaluationError(msg)
        if name in env:
            return env[name]
        if name == "None":
            return None
        if name == "True":
            return True
        if name == "False":
            return False
        if name in DEFAULT_SAFE_FUNCTIONS:
            return DEFAULT_SAFE_FUNCTIONS[name]
        raise EvaluationError(f"Name '{name}' is not defined")

    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand, env)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.Invert):
            return ~operand
        raise EvaluationError(f"Unsupported unary operator: {type(node.op).__name__}")

    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, env)
        right = _eval_node(node.right, env)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise EvaluationError("Division by zero")
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            if right == 0:
                raise EvaluationError("Integer division by zero")
            return left // right
        if isinstance(node.op, ast.Mod):
            if right == 0:
                raise EvaluationError("Modulo by zero")
            return left % right
        if isinstance(node.op, ast.Pow):
            if isinstance(right, (int, float)) and (right > 1000 or right < -1000):
                raise EvaluationError("Exponent too large")
            return left**right
        if isinstance(node.op, ast.BitOr):
            return left | right
        if isinstance(node.op, ast.BitAnd):
            return left & right
        if isinstance(node.op, ast.BitXor):
            return left ^ right
        raise EvaluationError(f"Unsupported binary operator: {type(node.op).__name__}")

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            val: Any = True
            for val_node in node.values:
                val = _eval_node(val_node, env)
                if not val:
                    return val
            return val
        if isinstance(node.op, ast.Or):
            val = False
            for val_node in node.values:
                val = _eval_node(val_node, env)
                if val:
                    return val
            return val
        raise EvaluationError(f"Unsupported boolean operator: {type(node.op).__name__}")

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, env)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _eval_node(comparator, env)
            if isinstance(op, ast.Eq):
                matched = left == right
            elif isinstance(op, ast.NotEq):
                matched = left != right
            elif isinstance(op, ast.Lt):
                matched = left < right
            elif isinstance(op, ast.LtE):
                matched = left <= right
            elif isinstance(op, ast.Gt):
                matched = left > right
            elif isinstance(op, ast.GtE):
                matched = left >= right
            elif isinstance(op, ast.Is):
                matched = left is right
            elif isinstance(op, ast.IsNot):
                matched = left is not right
            elif isinstance(op, ast.In):
                matched = left in right
            elif isinstance(op, ast.NotIn):
                matched = left not in right
            else:
                raise EvaluationError(f"Unsupported comparison operator: {type(op).__name__}")
            if not matched:
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        test_val = _eval_node(node.test, env)
        if test_val:
            return _eval_node(node.body, env)
        return _eval_node(node.orelse, env)

    if isinstance(node, ast.Attribute):
        attr = node.attr
        if attr.startswith("_"):
            msg = f"Access to private attribute '{attr}' is forbidden"
            logger.warning(f"Security warning: {msg}")
            raise RestrictedEvaluationError(msg)
        target = _eval_node(node.value, env)
        if isinstance(target, (type, types.ModuleType)):
            msg = f"Access to attributes on type/module '{type(target).__name__}' is forbidden"
            logger.warning(f"Security warning: {msg}")
            raise RestrictedEvaluationError(msg)
        try:
            return getattr(target, attr)
        except AttributeError as exc:
            raise EvaluationError(
                f"'{type(target).__name__}' object has no attribute '{attr}'"
            ) from exc

    if isinstance(node, ast.Subscript):
        target = _eval_node(node.value, env)
        index = _eval_node(node.slice, env)
        try:
            return target[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise EvaluationError(f"Subscript lookup failed: {exc}") from exc

    if isinstance(node, ast.Slice):
        lower = _eval_node(node.lower, env) if node.lower is not None else None
        upper = _eval_node(node.upper, env) if node.upper is not None else None
        step = _eval_node(node.step, env) if node.step is not None else None
        return slice(lower, upper, step)

    if isinstance(node, ast.List):
        return [_eval_node(elt, env) for elt in node.elts]

    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(elt, env) for elt in node.elts)

    if isinstance(node, ast.Dict):
        dict_val: dict[Any, Any] = {}
        for k, v in zip(node.keys, node.values, strict=True):
            if k is None:
                raise RestrictedEvaluationError("Dictionary unpacking is forbidden")
            dict_val[_eval_node(k, env)] = _eval_node(v, env)
        return dict_val

    if isinstance(node, ast.Set):
        return {_eval_node(elt, env) for elt in node.elts}

    if isinstance(node, ast.Call):
        args = [_eval_node(arg, env) for arg in node.args]
        kwargs = {}
        for kw in node.keywords:
            if kw.arg and kw.arg.startswith("_"):
                msg = f"Keyword argument '{kw.arg}' is forbidden"
                logger.warning(f"Security warning: {msg}")
                raise RestrictedEvaluationError(msg)
            if kw.arg:
                kwargs[kw.arg] = _eval_node(kw.value, env)

        if isinstance(node.func, ast.Name):
            func_name = node.func.id
            if func_name.startswith("_") or func_name not in DEFAULT_SAFE_FUNCTIONS:
                msg = f"Function '{func_name}' is forbidden"
                logger.warning(f"Security warning: {msg}")
                raise RestrictedEvaluationError(msg)
            func = DEFAULT_SAFE_FUNCTIONS[func_name]
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                raise EvaluationError(f"Error calling function '{func_name}': {exc}") from exc

        if isinstance(node.func, ast.Attribute):
            method_name = node.func.attr
            if method_name.startswith("_"):
                msg = f"Method '{method_name}' is forbidden"
                logger.warning(f"Security warning: {msg}")
                raise RestrictedEvaluationError(msg)
            target = _eval_node(node.func.value, env)
            if isinstance(target, (type, types.ModuleType)):
                msg = f"Method calls on type/module '{type(target).__name__}' are forbidden"
                logger.warning(f"Security warning: {msg}")
                raise RestrictedEvaluationError(msg)
            if not hasattr(target, method_name):
                raise EvaluationError(
                    f"'{type(target).__name__}' object has no method '{method_name}'"
                )
            method = getattr(target, method_name)
            if not callable(method):
                raise EvaluationError(
                    f"Attribute '{method_name}' on '{type(target).__name__}' is not callable"
                )
            if method_name not in DEFAULT_SAFE_METHODS:
                msg = f"Method '{method_name}' is forbidden"
                logger.warning(f"Security warning: {msg}")
                raise RestrictedEvaluationError(msg)
            try:
                return method(*args, **kwargs)
            except Exception as exc:
                raise EvaluationError(f"Error calling method '{method_name}': {exc}") from exc

        raise RestrictedEvaluationError(f"Call target '{type(node.func).__name__}' is forbidden")

    raise RestrictedEvaluationError(f"AST node '{type(node).__name__}' is not supported")


def evaluate_expression(
    expr: str | ast.AST,
    env: dict[str, Any] | None = None,
    default: Any = None,
    fallback: Any = None,
    raise_on_error: bool = False,
) -> Any:
    """Evaluate a Python expression safely using restricted AST traversal.

    Args:
        expr: Python expression string or pre-parsed AST node.
        env: Variable environment mapping identifiers to values.
        default: Default value returned if evaluation fails and raise_on_error is False.
        fallback: Alternative name for default value (used if default is None).
        raise_on_error: If True, raise EvaluationError/RestrictedEvaluationError on error.

    Returns:
        The result of the evaluated expression, or default/fallback value if evaluation fails.
    """
    effective_default = fallback if fallback is not None and default is None else default
    eval_env = dict(env) if env is not None else {}

    try:
        tree: ast.AST
        if isinstance(expr, str):
            tree = parse_expression(expr)
        elif isinstance(expr, ast.AST):
            validate_ast(expr)
            tree = expr
        else:
            raise EvaluationError(f"Expected str or ast.AST, got {type(expr).__name__}")

        return _eval_node(tree, eval_env)
    except (RestrictedEvaluationError, EvaluationError) as exc:
        if raise_on_error:
            raise
        logger.warning(f"Expression evaluation failed: {exc}")
        return effective_default
    except Exception as exc:
        if raise_on_error:
            raise EvaluationError(f"Expression evaluation failed: {exc}") from exc
        logger.warning(f"Expression evaluation failed: {exc}")
        return effective_default


class RestrictedAstEvaluator:
    """Object-oriented interface for restricted expression parsing and evaluation."""

    def __init__(self, expr: str) -> None:
        """Initialize and validate the expression.

        Args:
            expr: The expression string to parse and validate.
        """
        self.expr = expr
        self.tree = parse_expression(expr)

    def evaluate(
        self,
        env: dict[str, Any] | None = None,
        default: Any = None,
        fallback: Any = None,
        raise_on_error: bool = False,
    ) -> Any:
        """Evaluate the pre-parsed AST expression.

        Args:
            env: Variable environment mapping identifiers to values.
            default: Default value if evaluation fails.
            fallback: Alternative default value.
            raise_on_error: Whether to raise errors on failure.

        Returns:
            The evaluated expression result or fallback.
        """
        return evaluate_expression(
            self.tree,
            env=env,
            default=default,
            fallback=fallback,
            raise_on_error=raise_on_error,
        )


__all__ = [
    "DEFAULT_SAFE_FUNCTIONS",
    "DEFAULT_SAFE_METHODS",
    "RestrictedAstEvaluator",
    "evaluate_expression",
    "parse_expression",
    "validate_ast",
]
