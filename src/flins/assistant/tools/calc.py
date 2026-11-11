"""calculator: exact arithmetic without `eval`.

The expression is parsed with Python's `ast` and walked by hand. Only numbers, the listed
operators, a few maths functions and the constants pi / e are accepted: no names, attributes,
calls to anything else, strings or comprehensions. Sizes are limited so "9**9**9" can't hang
the process.
"""

from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from flins.assistant.tools.base import Tool, ToolResult, fail, ok

MAX_EXPR_CHARS = 200
MAX_NODES = 80
MAX_POWER = 512
MAX_BITS = 4096

Number = int | float

_BIN: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_FUNCS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "min": min,
    "max": max,
    "log": math.log,
    "ln": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "radians": math.radians,
    "degrees": math.degrees,
    "factorial": math.factorial,
}
_CONSTS = {"pi": math.pi, "e": math.e}


class CalcError(ValueError):
    pass


def _power(a: Number, b: Number) -> Number:
    if isinstance(a, int) and isinstance(b, int):
        if b > MAX_POWER or (a and abs(a).bit_length() * max(b, 1) > MAX_BITS):
            raise CalcError("that power is too large")
    elif abs(b) > MAX_POWER:
        raise CalcError("that power is too large")
    result = a**b
    if isinstance(result, complex):
        raise CalcError("the result is not a real number")
    return result


def _eval(node: ast.AST) -> Number:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise CalcError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd | ast.USub):
        value = _eval(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp):
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow):
            return _power(left, right)
        op = _BIN.get(type(node.op))
        if op is None:
            raise CalcError("that operator isn't supported")
        result = op(left, right)
        if isinstance(result, int) and result.bit_length() > MAX_BITS:
            raise CalcError("the result is too large")
        return result  # type: ignore[no-any-return]
    if isinstance(node, ast.Name):
        if node.id.lower() in _CONSTS:
            return _CONSTS[node.id.lower()]
        raise CalcError(f"'{node.id}' isn't something I can calculate with")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise CalcError("that function isn't supported")
        fn = _FUNCS.get(node.func.id.lower())
        if fn is None:
            raise CalcError(f"the function '{node.func.id}' isn't supported")
        args = [_eval(a) for a in node.args]
        if node.func.id.lower() == "factorial" and (
            len(args) != 1 or not isinstance(args[0], int) or not 0 <= args[0] <= 170
        ):
            raise CalcError("factorial needs a whole number from 0 to 170")
        result = fn(*args)
        if isinstance(result, bool) or not isinstance(result, int | float):
            raise CalcError("the result is not a number")
        return result
    raise CalcError("that isn't an arithmetic expression")


def _prepare(expr: str) -> str:
    e = expr.strip().rstrip("=?").strip()
    e = e.translate(
        {0xD7: "*", 0xF7: "/", 0x2212: "-"}
    )  # typographic multiply, divide and minus signs
    e = e.replace("^", "**")
    e = re.sub(r"(?<=\d)\s*[xX]\s*(?=\d)", "*", e)  # "25 x 4"
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\b", r"(\1/100)*", e, flags=re.IGNORECASE)  # "15% of 240"
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%(?!\s*\d)", r"(\1/100)", e)  # "240 + 15%" stays literal
    return e


def show(value: Number) -> str:
    if isinstance(value, int):
        return str(value)
    if math.isnan(value) or math.isinf(value):
        raise CalcError("the result is not a finite number")
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.12g}"


def calculate(expr: str) -> str:
    """'25 * 4' -> '100'. Raises CalcError with a plain reason."""
    if not expr.strip():
        raise CalcError("there's nothing to calculate")
    if len(expr) > MAX_EXPR_CHARS:
        raise CalcError("the expression is too long")
    prepared = _prepare(expr)
    try:
        tree = ast.parse(prepared, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        raise CalcError("I couldn't read that as arithmetic") from None
    if sum(1 for _ in ast.walk(tree)) > MAX_NODES:
        raise CalcError("the expression is too complicated")
    try:
        return show(_eval(tree))
    except ZeroDivisionError:
        raise CalcError("that divides by zero") from None
    except (OverflowError, ValueError, TypeError, RecursionError) as exc:
        if isinstance(exc, CalcError):
            raise
        raise CalcError("that can't be calculated") from None


async def _run(arg: str, now: datetime) -> ToolResult:
    try:
        return ok(f"{arg.strip()} = {calculate(arg)}")
    except CalcError as exc:
        return fail(f"Can't calculate '{arg.strip()[:80]}': {exc}.")


CALCULATOR = Tool(
    name="calculator",
    description="Exact arithmetic. Use it for any sum you would otherwise do in your head.",
    usage=(
        "one arithmetic expression with plain numbers, e.g. '1499 * 12 * 0.18', "
        "'15% of 240', 'sqrt(2) * 10', '(3 + 4) ** 2'. No units, words or thousands separators."
    ),
    risk="read",
    run=_run,
)
