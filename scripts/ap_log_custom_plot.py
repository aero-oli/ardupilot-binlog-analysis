#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ap_common import (
    AnalysisError, ensure_dir, event_markers_from_tables, filter_tables_by_time, get_col, load_tables,
    parse_time_window, require_package, write_json
)

FIELD_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b")
ALLOWED_BINOPS = (ast.Add, ast.Sub, ast.Mult, ast.Div)
ALLOWED_UNARYOPS = (ast.USub, ast.UAdd)


def _plotly():
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
        return go, make_subplots
    except Exception as exc:
        raise AnalysisError("plotly is required for custom HTML plots. Install dependencies with pip install -r requirements.txt") from exc


def parse_series_spec(spec: str) -> dict:
    """Parse MESSAGE.FIELD, arithmetic expressions, and optional =Label."""
    raw = spec.strip()
    if not raw:
        raise AnalysisError("Empty --series value")
    if "=" in raw:
        target, label = raw.split("=", 1)
        label = label.strip() or None
    else:
        target, label = raw, None
    fields = FIELD_TOKEN_RE.findall(target)
    if not fields:
        raise AnalysisError(f"Series '{spec}' must include at least one MESSAGE.FIELD token, for example GPS.Alt")
    if len(fields) == 1 and target.strip() == fields[0]:
        message, field = fields[0].split(".", 1)
        message = message.strip().upper()
        field = field.strip()
        return {"kind": "field", "message": message, "field": field, "label": label or f"{message}.{field}", "target": f"{message}.{field}", "fields": [f"{message}.{field}"]}
    return {"kind": "expression", "expression": target.strip(), "label": label or target.strip(), "target": target.strip(), "fields": fields}


def validate_expression_ast(expr: str) -> ast.AST:
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and not isinstance(node.op, ALLOWED_BINOPS):
            raise AnalysisError(f"Unsupported operator in expression: {expr}")
        if isinstance(node, ast.UnaryOp) and not isinstance(node.op, ALLOWED_UNARYOPS):
            raise AnalysisError(f"Unsupported unary operator in expression: {expr}")
        if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Name, ast.Load, ast.Constant, *ALLOWED_BINOPS, *ALLOWED_UNARYOPS)):
            continue
        raise AnalysisError(f"Unsupported syntax in expression: {expr}")
    return tree


def resolve_series(tables, specs):
    resolved = []
    missing = []
    for item in specs:
        if item.get("kind") == "expression":
            resolved.append(resolve_expression_series(tables, item))
            continue
        message = item["message"]
        if message not in tables:
            missing.append(f"{item['target']}: message {message} not extracted")
            continue
        df = tables[message]
        col = get_col(df, [item["field"]])
        if not col:
            available = ", ".join(str(c) for c in df.columns[:30])
            suffix = "..." if len(df.columns) > 30 else ""
            missing.append(f"{item['target']}: field not found in {message}; available: {available}{suffix}")
            continue
        resolved.append({**item, "df": df, "column": col})
    if missing:
        raise AnalysisError("Could not resolve requested plot series:\n" + "\n".join(f"- {m}" for m in missing))
    return resolved


def resolve_field(tables, token):
    message, field = token.split(".", 1)
    message = message.upper()
    if message not in tables:
        raise AnalysisError(f"{token}: message {message} not extracted")
    df = tables[message]
    col = get_col(df, [field])
    if not col:
        available = ", ".join(str(c) for c in df.columns[:30])
        suffix = "..." if len(df.columns) > 30 else ""
        raise AnalysisError(f"{token}: field not found in {message}; available: {available}{suffix}")
    if "TimeS" not in df.columns:
        raise AnalysisError(f"{token}: expression fields require TimeS")
    return message, col, df[["TimeS", col]].dropna().sort_values("TimeS")


def resolve_expression_series(tables, item):
    pd = require_package("pandas")
    fields = []
    var_map = {}
    frames = []
    for idx, token in enumerate(item["fields"]):
        message, col, frame = resolve_field(tables, token)
        normalized = f"{message}.{col}"
        var = f"v{idx}"
        var_map[token] = var
        fields.append(normalized)
        frames.append((var, frame.rename(columns={col: var})))
    base = frames[0][1][["TimeS"]].copy()
    for var, frame in frames:
        base = pd.merge_asof(base.sort_values("TimeS"), frame.sort_values("TimeS"), on="TimeS", direction="nearest")
    expr = item["expression"]
    for token, var in sorted(var_map.items(), key=lambda kv: len(kv[0]), reverse=True):
        expr = re.sub(rf"\b{re.escape(token)}\b", var, expr)
    tree = validate_expression_ast(expr)
    env = {var: pd.to_numeric(base[var], errors="coerce") for var, _frame in frames}
    env["__builtins__"] = {}
    values = eval(compile(tree, "<custom-plot-expression>", "eval"), env, {})
    out = pd.DataFrame({"TimeS": base["TimeS"], "value": values})
    return {**item, "df": out, "column": "value", "fields": fields}


def add_event_markers(fig, markers):
    if not markers:
        return
    shapes = []
    annotations = []
    for marker in markers[:80]:
        x = marker["time_s"]
        color = "#dc2626" if marker["source"] == "ERR" else ("#2563eb" if marker["source"] == "MODE" else "#64748b")
        shapes.append({"type": "line", "xref": "x", "yref": "paper", "x0": x, "x1": x, "y0": 0, "y1": 1, "line": {"color": color, "width": 1, "dash": "dot"}})
        annotations.append({"xref": "x", "yref": "paper", "x": x, "y": 1.02, "text": marker["label"], "showarrow": False, "textangle": -45, "font": {"size": 9, "color": color}})
    fig.update_layout(shapes=shapes, annotations=annotations)


def output_name(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.strip().lower()).strip("_")
    return (slug or "custom_plot") + ".html"


def make_custom_plot(tables, series_specs, out, title="Custom ArduPilot plot", secondary=None, mode="overlay", analysis_window=None, events=False):
    if not series_specs:
        raise AnalysisError("At least one --series value is required")
    go, make_subplots = _plotly()
    series = resolve_series(tables, [parse_series_spec(s) if isinstance(s, str) else s for s in series_specs])
    secondary_targets = {parse_series_spec(s)["target"].upper() for s in (secondary or [])}
    series_targets = {item["target"].upper() for item in series}
    unknown_secondary = sorted(secondary_targets - series_targets)
    if unknown_secondary:
        raise AnalysisError(
            "--secondary values must also be present as --series values: " + ", ".join(unknown_secondary)
        )
    out = Path(out)

    markers = event_markers_from_tables(tables) if events else []

    if mode == "subplots":
        fig = make_subplots(
            rows=len(series),
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.04,
            subplot_titles=tuple(item["label"] for item in series),
        )
        for row, item in enumerate(series, start=1):
            df = item["df"]
            x = df["TimeS"] if "TimeS" in df.columns else list(range(len(df)))
            fig.add_trace(go.Scatter(x=x, y=df[item["column"]], name=item["label"], mode="lines"), row=row, col=1)
        fig.update_yaxes(title_text="value")
    else:
        use_secondary = bool(secondary_targets)
        fig = make_subplots(specs=[[{"secondary_y": use_secondary}]])
        for item in series:
            df = item["df"]
            x = df["TimeS"] if "TimeS" in df.columns else list(range(len(df)))
            target_upper = item["target"].upper()
            fig.add_trace(
                go.Scatter(x=x, y=df[item["column"]], name=item["label"], mode="lines"),
                secondary_y=target_upper in secondary_targets if use_secondary else False,
            )
        fig.update_yaxes(title_text="primary")
        if use_secondary:
            fig.update_yaxes(title_text="secondary", secondary_y=True)

    fig.update_layout(title=title, template="plotly_white", hovermode="x unified", xaxis_title="TimeS")
    add_event_markers(fig, markers)
    if out.suffix.lower() == ".html":
        path = out
        ensure_dir(path.parent)
    else:
        path = ensure_dir(out) / output_name(title)
    fig.write_html(str(path), include_plotlyjs="cdn")
    manifest = {
        "plot": str(path),
        "title": title,
        "mode": mode,
        "analysis_window": analysis_window or {"start_s": None, "end_s": None},
        "events_overlay": bool(events),
        "series": [
            {
                "message": s.get("message"),
                "field": s.get("column"),
                "label": s["label"],
                "expression": s.get("expression"),
                "fields": s.get("fields", []),
            }
            for s in series
        ],
        "secondary": sorted(secondary_targets),
    }
    return manifest


def main() -> int:
    p = argparse.ArgumentParser(description="Generate an interactive custom HTML plot from extracted ArduPilot log tables.")
    p.add_argument("--tables", required=True, help="Directory produced by ap_log_extract.py")
    p.add_argument("--series", action="append", required=True, help="Series as MESSAGE.FIELD or MESSAGE.FIELD=Label; repeat for multiple traces")
    p.add_argument("--secondary", action="append", default=[], help="MESSAGE.FIELD series to plot on the right y-axis in overlay mode")
    p.add_argument("--mode", choices=["overlay", "subplots"], default="overlay")
    p.add_argument("--title", default="Custom ArduPilot plot")
    p.add_argument("--out", default="custom_plot.html", help="Output .html path or output directory")
    p.add_argument("--manifest", default=None)
    p.add_argument("--window", default=None, help="Optional TimeS window as START:END or around:CENTER:RADIUS")
    p.add_argument("--events", action="store_true", help="Overlay MODE/ERR/EV/MSG markers")
    args = p.parse_args()
    try:
        tables = load_tables(args.tables)
        window = parse_time_window(args.window)
        tables = filter_tables_by_time(tables, **window)
        manifest = make_custom_plot(tables, args.series, args.out, title=args.title, secondary=args.secondary, mode=args.mode, analysis_window=window, events=args.events)
        if args.manifest:
            write_json(args.manifest, manifest)
        print(f"Wrote custom plot to {manifest['plot']}")
        return 0
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
