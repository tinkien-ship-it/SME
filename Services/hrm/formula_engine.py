# -*- coding: utf-8 -*-
"""Dynamic Formula Builder — Excel-like an toàn (không eval Python tùy ý)."""
from __future__ import annotations

import ast
import operator
import re
import sqlite3
from typing import Any

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Biến chuẩn đưa vào payroll
VAR_ALIASES = {
    'GROSS_SALARY': 'Gross_Salary',
    'BASE_SALARY': 'Base_Salary',
    'TIME_SALARY': 'Time_Salary',
    'ACTUAL_WORKING_DAYS': 'Actual_Working_Days',
    'STANDARD_DAYS': 'Standard_Days',
    'KPI_SCORE': 'KPI_Score',
    'OT_HOURS': 'OT_Hours',
    'OT_HOURS_NIGHT': 'OT_Hours_Night',
    'ALLOWANCE_FUND': 'Allowance_Fund',
    'ALLOWANCE_OTHER': 'Allowance_Other',
    'BONUS': 'Bonus',
    'DEPENDENTS': 'Dependents',
}


class FormulaError(ValueError):
    pass


def _normalize_expr(expr: str) -> str:
    text = (expr or '').strip()
    # IF(cond, a, b) → ((cond) and (a) or (b)) với cond so sánh
    text = re.sub(r'\bIF\s*\(', '_IF(', text, flags=re.I)
    text = re.sub(r'\bMAX\s*\(', '_MAX(', text, flags=re.I)
    text = re.sub(r'\bMIN\s*\(', '_MIN(', text, flags=re.I)
    text = re.sub(r'\bROUND\s*\(', '_ROUND(', text, flags=re.I)
    # Excel-like: không dùng = so sánh trong AST; hỗ trợ >, <, >=, <=, !=, ==
    return text


def _eval_node(node: ast.AST, env: dict[str, float]) -> float:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body, env)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        key = node.id
        if key in env:
            return float(env[key])
        up = key.upper()
        if up in VAR_ALIASES and VAR_ALIASES[up] in env:
            return float(env[VAR_ALIASES[up]])
        # case-insensitive match
        for k, v in env.items():
            if k.lower() == key.lower():
                return float(v)
        raise FormulaError(f'Biến không hợp lệ: {key}')
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return float(_ALLOWED_BINOPS[type(node.op)](
            _eval_node(node.left, env), _eval_node(node.right, env)
        ))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
        return float(_ALLOWED_UNARY[type(node.op)](_eval_node(node.operand, env)))
    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval_node(comp, env)
            ok = False
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            else:
                raise FormulaError('Toán tử so sánh không hỗ trợ')
            if not ok:
                return 0.0
            left = right
        return 1.0
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            val = 1.0
            for v in node.values:
                val = _eval_node(v, env)
                if not val:
                    return 0.0
            return float(val)
        if isinstance(node.op, ast.Or):
            for v in node.values:
                val = _eval_node(v, env)
                if val:
                    return float(val)
            return 0.0
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        fname = node.func.id.upper()
        args = [_eval_node(a, env) for a in node.args]
        if fname == '_IF' and len(args) == 3:
            return args[1] if args[0] else args[2]
        if fname == '_MAX':
            return max(args)
        if fname == '_MIN':
            return min(args)
        if fname == '_ROUND' and 1 <= len(args) <= 2:
            nd = int(args[1]) if len(args) == 2 else 0
            return round(args[0], nd)
        raise FormulaError(f'Hàm không hỗ trợ: {node.func.id}')
    raise FormulaError('Biểu thức không hợp lệ')


def evaluate_formula(expression: str, variables: dict[str, Any]) -> float:
    env = {str(k): float(v or 0) for k, v in (variables or {}).items()}
    expr = _normalize_expr(expression)
    if not expr:
        return 0.0
    try:
        tree = ast.parse(expr, mode='eval')
    except SyntaxError as exc:
        raise FormulaError(f'Cú pháp sai: {exc}') from exc
    # chặn attribute / subscript
    for n in ast.walk(tree):
        if isinstance(n, (ast.Attribute, ast.Subscript, ast.Lambda, ast.Dict, ast.List)):
            raise FormulaError('Cú pháp bị cấm')
    return float(_eval_node(tree, env))


def list_formulas(conn: sqlite3.Connection) -> list[dict]:
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    return [dict(r) for r in conn.execute(
        'SELECT * FROM hrm_payroll_formulas WHERE COALESCE(is_active,1)=1 ORDER BY code'
    ).fetchall()]


def apply_formulas_to_line(
    conn: sqlite3.Connection,
    line: dict[str, Any],
    *,
    standard_days: float,
    kpi_score: float = 0.0,
    ot_hours: float = 0.0,
    ot_hours_night: float = 0.0,
) -> dict[str, Any]:
    """Áp công thức active → cộng vào bonus / allowance_other."""
    from Services.hrm.schema import ensure_hrm_schema
    ensure_hrm_schema(conn)
    row = conn.execute(
        'SELECT COALESCE(hrm_formula_enabled, 1) AS en FROM business_info LIMIT 1'
    ).fetchone()
    if row and int((row['en'] if hasattr(row, 'keys') else row[0]) or 0) == 0:
        return line
    vars_ = {
        'Gross_Salary': float(
            line.get('total_income') or line.get('time_salary') or line.get('base_salary') or 0
        ),
        'Base_Salary': float(line.get('base_salary') or 0),
        'Time_Salary': float(line.get('time_salary') or 0),
        'Actual_Working_Days': float(line.get('actual_working_days') or 0),
        'Standard_Days': float(standard_days or 0),
        'KPI_Score': float(kpi_score or 0),
        'OT_Hours': float(ot_hours or 0),
        'OT_Hours_Night': float(ot_hours_night or 0),
        'Allowance_Fund': float(line.get('allowance_fund') or 0),
        'Allowance_Other': float(line.get('allowance_other') or 0),
        'Bonus': float(line.get('bonus') or 0),
        'Dependents': float(line.get('dependents') or 0),
    }
    bonus_add = 0.0
    allow_add = 0.0
    applied = []
    for f in list_formulas(conn):
        try:
            val = evaluate_formula(f.get('expression') or '', vars_)
        except FormulaError:
            continue
        out = (f.get('output_field') or 'bonus').strip().lower()
        if out in ('allowance_other', 'allowance'):
            allow_add += val
        else:
            bonus_add += val
        applied.append({'code': f.get('code'), 'value': round(val), 'output': out})
    line = dict(line)
    line['bonus'] = float(line.get('bonus') or 0) + round(bonus_add)
    line['allowance_other'] = float(line.get('allowance_other') or 0) + round(allow_add)
    line['formula_applied'] = applied
    return line
