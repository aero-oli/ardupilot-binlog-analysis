#!/usr/bin/env python3
"""Shared helpers for ArduPilot DataFlash log analysis.

The scripts in this skill intentionally avoid hard-coded firmware schemas where
possible. ArduPilot DataFlash logs are self-describing, so these helpers parse
message names and fields dynamically via pymavlink.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import numpy as np
except Exception:  # pragma: no cover - dependency checked at runtime
    np = None

try:
    import pandas as pd
except Exception:  # pragma: no cover - dependency checked at runtime
    pd = None

DEFAULT_MESSAGES = [
    "ATT", "RATE", "PIDR", "PIDP", "PIDY", "PIDA", "VIBE", "IMU", "GYR", "ACC",
    "GPS", "GPA", "GPS2", "MAG", "XKF1", "XKF2", "XKF3", "XKF4", "XKFS", "NKF1", "NKF2", "NKF3", "NKF4",
    "BAT", "BCL", "POWR", "RCOU", "RCO2", "RCO3", "RCIN", "ESC", "ESCX", "EDT2", "CTUN", "NTUN", "POS", "BARO", "RNGF",
    "MODE", "MSG", "EV", "ERR", "ARM", "ATUN", "SID", "SIDD", "SIDS", "ISBH", "ISBD", "PARM"
]

AXIS_MAP = {
    "roll": {"att_des": "DesRoll", "att": "Roll", "rate_des": "RDes", "rate": "R", "out": "ROut", "pid": "PIDR"},
    "pitch": {"att_des": "DesPitch", "att": "Pitch", "rate_des": "PDes", "rate": "P", "out": "POut", "pid": "PIDP"},
    "yaw": {"att_des": "DesYaw", "att": "Yaw", "rate_des": "YDes", "rate": "Y", "out": "YOut", "pid": "PIDY"},
}

RATE_FIELDS = ["RDes", "R", "ROut", "PDes", "P", "POut", "YDes", "Y", "YOut", "ADes", "A", "AOut"]
PID_FIELDS = ["Tar", "Act", "Err", "P", "I", "D", "FF", "DFF", "Dmod", "SRate", "Flags"]
OUTPUT_MESSAGE_NAMES = ("RCOU", "RCO2", "RCO3")
OUTPUT_FUNCTIONS = {
    -1: ("gpio", "other"),
    0: ("disabled", "other"),
    1: ("rc_passthrough", "passthrough"),
    30: ("motor_enable_switch", "other"),
    31: ("rotor_head_speed", "heli"),
    32: ("tail_rotor_speed", "heli"),
    33: ("motor1", "motor"),
    34: ("motor2", "motor"),
    35: ("motor3", "motor"),
    36: ("motor4", "motor"),
    37: ("motor5", "motor"),
    38: ("motor6", "motor"),
    39: ("motor7", "motor"),
    40: ("motor8", "motor"),
    41: ("motor_tilt", "tilt"),
    45: ("tilt_motor_rear", "tilt"),
    46: ("tilt_motor_rear_left", "tilt"),
    47: ("tilt_motor_rear_right", "tilt"),
    70: ("throttle", "throttle"),
    73: ("bicopter_motor_left", "motor"),
    74: ("bicopter_motor_right", "motor"),
    75: ("tilt_motor_left", "tilt"),
    76: ("tilt_motor_right", "tilt"),
    81: ("boost_throttle", "throttle"),
    82: ("motor9", "motor"),
    83: ("motor10", "motor"),
    84: ("motor11", "motor"),
    85: ("motor12", "motor"),
}

class AnalysisError(RuntimeError):
    pass

def require_package(name: str, import_name: Optional[str] = None) -> Any:
    import importlib
    try:
        return importlib.import_module(import_name or name)
    except Exception as exc:
        raise AnalysisError(
            f"Required Python package '{name}' is not installed. Install dependencies with: pip install -r requirements.txt"
        ) from exc

def ensure_dir(path: os.PathLike | str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p

def write_json(path: os.PathLike | str, data: Any) -> None:
    path = Path(path)
    ensure_dir(path.parent)
    path.write_text(json.dumps(data, indent=2, sort_keys=False, default=json_default), encoding="utf-8")

def read_json(path: os.PathLike | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))

def json_default(obj: Any) -> Any:
    if np is not None:
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, (np.ndarray,)):
            return obj.tolist()
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            pass
    return str(obj)

def safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None:
            return default
        if isinstance(v, str) and v.strip() == "":
            return default
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except Exception:
        return default

def safe_int(v: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if v is None:
            return default
        if isinstance(v, str) and v.strip() == "":
            return default
        return int(float(v))
    except Exception:
        return default

def message_to_dict(msg: Any) -> Dict[str, Any]:
    if hasattr(msg, "to_dict"):
        d = dict(msg.to_dict())
        d.pop("mavpackettype", None)
        return d
    fields = getattr(msg, "_fieldnames", []) or []
    return {f: getattr(msg, f, None) for f in fields}

def message_type(msg: Any) -> str:
    if hasattr(msg, "get_type"):
        return msg.get_type()
    return getattr(msg, "_type", type(msg).__name__)

def open_dataflash(path: os.PathLike | str):
    DFReader = require_package("pymavlink", "pymavlink.DFReader")
    path = str(path)
    if not os.path.exists(path):
        raise AnalysisError(f"Log file not found: {path}")
    lower = path.lower()
    try:
        if lower.endswith(".bin"):
            return DFReader.DFReader_binary(path)
        else:
            # DFReader_text handles many .log text dataflash logs. Binary reader may also work for some .log files.
            try:
                return DFReader.DFReader_text(path)
            except Exception:
                return DFReader.DFReader_binary(path)
    except Exception as exc:
        raise AnalysisError(f"Could not open '{path}' as an ArduPilot DataFlash log: {exc}") from exc


def iter_dataflash_messages(path: os.PathLike | str, max_messages: Optional[int] = None):
    mlog = open_dataflash(path)
    count = 0
    while True:
        try:
            msg = mlog.recv_match()
        except Exception as exc:
            raise AnalysisError(f"Error while reading log near message {count}: {exc}") from exc
        if msg is None:
            break
        yield msg
        count += 1
        if max_messages and count >= max_messages:
            break


def _message_iter(source: Any, max_messages: Optional[int] = None):
    if isinstance(source, (str, os.PathLike)):
        yield from iter_dataflash_messages(source, max_messages=max_messages)
        return
    count = 0
    for msg in source:
        yield msg
        count += 1
        if max_messages and count >= max_messages:
            break


def _row_in_time_window(row: Dict[str, Any], start_s: Optional[float] = None, end_s: Optional[float] = None) -> bool:
    if start_s is None and end_s is None:
        return True
    _, ts = time_column(row)
    if ts is None:
        return True
    if start_s is not None and ts < start_s:
        return False
    if end_s is not None and ts > end_s:
        return False
    return True


def _armed_value(row: Dict[str, Any]) -> Optional[bool]:
    for key in ["Armed", "ArmState", "ARM", "State"]:
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, str):
            text = value.strip().lower()
            if text in {"armed", "arm", "true", "yes", "1"}:
                return True
            if text in {"disarmed", "disarm", "false", "no", "0"}:
                return False
        n = safe_int(value)
        if n is not None:
            return n != 0
    return None


def _detect_dropout(typ: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    dropout_fields = {}
    for key, value in row.items():
        lower = str(key).lower()
        if lower in {"dp", "drop", "drops", "dropped", "dropout", "dropouts", "lost", "skipped"} or "drop" in lower:
            numeric = safe_float(value)
            if numeric is not None and numeric > 0:
                dropout_fields[key] = numeric
    if not dropout_fields and typ.upper() not in {"DSF", "DRO", "DROP"}:
        return None
    _, ts = time_column(row)
    return {"time_s": ts, "message": typ, "fields": dropout_fields or strip_private(row)}


class StreamingIndexBuilder:
    def __init__(self, path: os.PathLike | str):
        self.path = path
        self.messages: Dict[str, Dict[str, Any]] = {}
        self.parameters: Dict[str, Any] = {}
        self.firmware_messages: List[str] = []
        self.modes: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.errors: List[Dict[str, Any]] = []
        self.logging_dropouts: List[Dict[str, Any]] = []
        self.start_s: Optional[float] = None
        self.end_s: Optional[float] = None

    def add_row(self, typ: str, row: Dict[str, Any]) -> None:
        entry = self.messages.setdefault(typ, {"count": 0, "fields": []})
        entry["count"] += 1
        if entry["count"] <= 200:
            fields = set(entry["fields"])
            fields.update(k for k in row.keys() if not k.startswith("_"))
            entry["fields"] = sorted(fields)
        _, ts = time_column(row)
        if ts is not None:
            self.start_s = ts if self.start_s is None else min(self.start_s, ts)
            self.end_s = ts if self.end_s is None else max(self.end_s, ts)
        if typ == "PARM":
            name = str(row.get("Name") or row.get("name") or "").strip()
            val = row.get("Value", row.get("value"))
            if name:
                self.parameters[name] = val
        elif typ == "MSG" and len(self.firmware_messages) < 200:
            msg_text = str(row.get("Message") or row.get("Msg") or row.get("message") or "").strip()
            if msg_text:
                self.firmware_messages.append(msg_text)
        elif typ == "MODE" and len(self.modes) < 500:
            self.modes.append({"time_s": ts, "mode": row.get("Mode") or row.get("ModeNum") or row.get("Name"), "raw": strip_private(row)})
        elif typ == "EV" and len(self.events) < 500:
            self.events.append({"time_s": ts, "id": row.get("Id"), "raw": strip_private(row)})
        elif typ == "ERR" and len(self.errors) < 500:
            self.errors.append({"time_s": ts, "subsys": row.get("Subsys"), "ecode": row.get("ECode"), "raw": strip_private(row)})
        dropout = _detect_dropout(typ, row)
        if dropout and len(self.logging_dropouts) < 200:
            self.logging_dropouts.append(dropout)

    def to_index(self, stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        vehicle = infer_vehicle(self.firmware_messages, self.parameters)
        firmware = infer_firmware(self.firmware_messages)
        path = Path(self.path)
        return {
            "file": str(self.path),
            "file_name": path.name,
            "file_size_bytes": path.stat().st_size if path.exists() else None,
            "vehicle": vehicle,
            "firmware": firmware,
            "duration_s": None if self.start_s is None or self.end_s is None else round(float(self.end_s - self.start_s), 3),
            "start_time_s": self.start_s,
            "end_time_s": self.end_s,
            "messages": self.messages,
            "message_names": sorted(self.messages.keys()),
            "parameters": self.parameters,
            "parameter_count": len(self.parameters),
            "firmware_messages": self.firmware_messages[:100],
            "modes": self.modes[:500],
            "events": self.events[:500],
            "errors": self.errors[:500],
            "logging_dropouts": self.logging_dropouts[:200],
            "parser_stats": stats or {},
        }


def collect_dataflash(
    data_source: Any,
    include: Optional[Sequence[str]] = None,
    max_messages: Optional[int] = None,
    start_s: Optional[float] = None,
    end_s: Optional[float] = None,
    armed_only: bool = False,
    source: Optional[str] = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Any], Dict[str, Any]]:
    """Stream a log/source once, counting all messages while storing only selected rows."""
    display = source or (str(data_source) if isinstance(data_source, (str, os.PathLike)) else "stream")
    include_set = {m.strip().upper() for m in include} if include is not None else None
    rows: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    builder = StreamingIndexBuilder(display)
    total = 0
    collected = 0
    armed_state = not armed_only
    armed_filter_supported = not armed_only
    for msg in _message_iter(data_source, max_messages=max_messages):
        total += 1
        typ = message_type(msg)
        row = message_to_dict(msg)
        row["_type"] = typ
        if typ == "ARM":
            arm = _armed_value(row)
            if arm is not None:
                armed_state = arm
                armed_filter_supported = True
        builder.add_row(typ, row)
        if include_set is not None and typ.upper() not in include_set:
            continue
        if not _row_in_time_window(row, start_s=start_s, end_s=end_s):
            continue
        if armed_only and not armed_state:
            continue
        rows[typ].append(row)
        collected += 1
    stats = {
        "total_messages_read": total,
        "collected_rows": collected,
        "message_filter": sorted(include_set) if include_set is not None else None,
        "start_time_s": start_s,
        "end_time_s": end_s,
        "armed_only": armed_only,
        "armed_filter_supported": armed_filter_supported,
        "max_messages": max_messages,
        "max_messages_reached": bool(max_messages and total >= max_messages),
    }
    return dict(rows), builder.to_index(stats=stats), stats


def parse_dataflash(
    path: os.PathLike | str,
    include: Optional[Sequence[str]] = None,
    max_messages: Optional[int] = None,
    start_s: Optional[float] = None,
    end_s: Optional[float] = None,
    armed_only: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """Parse selected DataFlash rows keyed by message type."""
    rows, _index, _stats = collect_dataflash(path, include=include, max_messages=max_messages, start_s=start_s, end_s=end_s, armed_only=armed_only)
    return rows

def parse_index_only(path: os.PathLike | str, max_messages: Optional[int] = None) -> Dict[str, Any]:
    _rows, index, _stats = collect_dataflash(path, include=[], max_messages=max_messages)
    return index

def time_column(row: Dict[str, Any]) -> Tuple[Optional[str], Optional[float]]:
    for key, scale in [("TimeUS", 1e-6), ("TimeMS", 1e-3), ("Time", 1.0), ("SampleUS", 1e-6), ("TS", 1.0)]:
        if key in row:
            val = safe_float(row.get(key))
            if val is not None:
                return key, val * scale
    return None, None

def add_time_s(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        rr = dict(r)
        _, ts = time_column(rr)
        rr["TimeS"] = ts
        out.append(rr)
    return out

def rows_to_dataframe(rows: List[Dict[str, Any]]):
    require_package("pandas")
    import pandas as pd  # type: ignore
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(add_time_s(rows))
    for col in df.columns:
        if col in {"_type", "Name", "Message", "Mode", "Type"}:
            continue
        try:
            df[col] = pd.to_numeric(df[col])
        except Exception:
            pass
    if "TimeS" in df.columns:
        df = df.sort_values("TimeS", kind="stable")
    return df

def write_table(rows: List[Dict[str, Any]], path: os.PathLike | str, fmt: str = "csv") -> None:
    ensure_dir(Path(path).parent)
    df = rows_to_dataframe(rows)
    if fmt == "parquet":
        try:
            df.to_parquet(path, index=False)
            return
        except Exception:
            # fallback handled by caller when needed
            raise
    df.to_csv(path, index=False)

def read_table(path: os.PathLike | str):
    require_package("pandas")
    import pandas as pd  # type: ignore
    path = Path(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)

def load_tables(tables_dir: os.PathLike | str) -> Dict[str, Any]:
    p = Path(tables_dir)
    if not p.exists():
        raise AnalysisError(f"Tables directory not found: {p}")
    tables = {}
    failures = []
    for f in sorted(p.glob("*.csv")) + sorted(p.glob("*.parquet")):
        typ = f.stem.upper()
        try:
            tables[typ] = read_table(f)
        except Exception as exc:
            failures.append(f"{f}: {exc}")
    if failures:
        detail = "\n".join(f"- {x}" for x in failures[:20])
        raise AnalysisError(f"Could not read one or more extracted tables:\n{detail}")
    if not tables:
        raise AnalysisError(f"No CSV or Parquet tables found in {p}")
    return tables

def df_duration(df: Any) -> Optional[float]:
    if df is None or len(df) == 0 or "TimeS" not in df.columns:
        return None
    s = df["TimeS"].dropna()
    if len(s) < 2:
        return None
    return float(s.max() - s.min())

def parse_time_window(value: str | None) -> Dict[str, Optional[float]]:
    if not value:
        return {"start_s": None, "end_s": None}
    text = str(value).strip()
    if text.startswith("around:"):
        try:
            _, center, radius = text.split(":", 2)
        except ValueError as exc:
            raise AnalysisError(f"Invalid --window value: {value}. Use around:CENTER:RADIUS") from exc
        c = safe_float(center)
        r = safe_float(radius)
        if c is None or r is None or r < 0:
            raise AnalysisError(f"Invalid --window value: {value}")
        return {"start_s": max(0.0, c - r), "end_s": c + r}
    if ":" in text:
        start, end = text.split(":", 1)
        start_s = safe_float(start, None) if start.strip() else None
        end_s = safe_float(end, None) if end.strip() else None
        if start_s is not None and start_s < 0:
            raise AnalysisError(f"Invalid --window value, start must be non-negative: {value}")
        if end_s is not None and end_s < 0:
            raise AnalysisError(f"Invalid --window value, end must be non-negative: {value}")
        if start_s is not None and end_s is not None and end_s < start_s:
            raise AnalysisError(f"Invalid --window value, end before start: {value}")
        return {"start_s": start_s, "end_s": end_s}
    raise AnalysisError(f"Invalid --window value: {value}. Use START:END or around:CENTER:RADIUS")

def filter_tables_by_time(tables: Dict[str, Any], start_s: Optional[float] = None, end_s: Optional[float] = None) -> Dict[str, Any]:
    if start_s is None and end_s is None:
        return tables
    out = {}
    for name, df in tables.items():
        if df is None or not hasattr(df, "columns") or "TimeS" not in df.columns:
            out[name] = df
            continue
        mask = df["TimeS"].notna()
        if start_s is not None:
            mask = mask & (df["TimeS"] >= start_s)
        if end_s is not None:
            mask = mask & (df["TimeS"] <= end_s)
        out[name] = df.loc[mask].copy()
    return out

def params_from_tables(tables: Dict[str, Any]) -> Dict[str, Any]:
    if "PARM" not in tables:
        return {}
    params = {}
    parm = tables["PARM"]
    if parm is None or not hasattr(parm, "to_dict"):
        return params
    for row in parm.to_dict(orient="records"):
        name = row.get("Name")
        if not name:
            continue
        value = row.get("Value", row.get("Default"))
        params[str(name)] = value
    return params

def output_mapping_from_params(params: Dict[str, Any], max_outputs: int = 32) -> Dict[str, Dict[str, Any]]:
    mapping = {}
    for idx in range(1, max_outputs + 1):
        key = f"SERVO{idx}_FUNCTION"
        function_id = safe_int(params.get(key))
        if function_id is None:
            continue
        role, category = OUTPUT_FUNCTIONS.get(function_id, (f"function_{function_id}", "other"))
        mapping[f"C{idx}"] = {
            "parameter": key,
            "function_id": function_id,
            "role": role,
            "category": category,
        }
    return mapping

def output_mapping_from_tables(tables: Dict[str, Any], max_outputs: int = 32) -> Dict[str, Dict[str, Any]]:
    return output_mapping_from_params(params_from_tables(tables), max_outputs=max_outputs)

def motor_channels_from_mapping(mapping: Dict[str, Dict[str, Any]], fallback_channels: Sequence[str]) -> List[str]:
    mapped = [
        c for c, info in sorted(mapping.items(), key=lambda kv: safe_int(kv[0][1:], 999) or 999)
        if info.get("category") == "motor"
    ]
    return mapped or list(fallback_channels)[:14]

def output_channel_label(channel: str, mapping: Dict[str, Dict[str, Any]]) -> str:
    role = mapping.get(channel, {}).get("role")
    return f"{channel} {role}".strip() if role else channel

def output_channel_columns(df: Any) -> List[str]:
    if df is None or not hasattr(df, "columns"):
        return []
    return [c for c in df.columns if str(c).startswith("C") and str(c)[1:].isdigit()]

def combined_rcout_dataframe(tables: Dict[str, Any]):
    require_package("pandas")
    import pandas as pd  # type: ignore
    frames = []
    for name in OUTPUT_MESSAGE_NAMES:
        df = tables.get(name)
        if df is None or len(df) == 0:
            continue
        cols = output_channel_columns(df)
        if not cols:
            continue
        if "TimeS" in df.columns:
            frames.append(df[["TimeS", *cols]].copy())
        else:
            frame = df[cols].copy()
            frame.insert(0, "TimeS", range(len(frame)))
            frames.append(frame)
    if not frames:
        return None
    out = frames[0]
    for frame in frames[1:]:
        duplicate_cols = [c for c in frame.columns if c != "TimeS" and c in out.columns]
        frame = frame.drop(columns=duplicate_cols)
        out = pd.merge(out.sort_values("TimeS"), frame.sort_values("TimeS"), on="TimeS", how="outer")
    return out.sort_values("TimeS", kind="stable").reset_index(drop=True)

def output_channels_from_tables(tables: Dict[str, Any]) -> List[str]:
    rc = combined_rcout_dataframe(tables)
    return [] if rc is None else output_channel_columns(rc)

def first_existing(tables: Dict[str, Any], names: Sequence[str]) -> Tuple[Optional[str], Any]:
    for n in names:
        if n in tables and len(tables[n]) > 0:
            return n, tables[n]
    return None, None

def get_col(df: Any, candidates: Sequence[str]) -> Optional[str]:
    if df is None:
        return None
    lower = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c in df.columns:
            return c
        if c.lower() in lower:
            return lower[c.lower()]
    return None

def numeric_series(df: Any, candidates: Sequence[str]):
    col = get_col(df, candidates)
    if col is None:
        return None
    s = df[col]
    try:
        return pd.to_numeric(s, errors="coerce") if pd is not None else s
    except Exception:
        return s

def build_index(path: os.PathLike | str, rows_by_type: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    builder = StreamingIndexBuilder(path)
    for typ, rows in rows_by_type.items():
        for row in rows:
            builder.add_row(typ, row)
    return builder.to_index(stats={"source": "rows_by_type", "stored_rows": sum(len(rows) for rows in rows_by_type.values())})

def strip_private(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if not k.startswith("_")}

def infer_vehicle(msgs: Sequence[str], params: Dict[str, Any]) -> Optional[str]:
    text = "\n".join(msgs).lower()
    for name in ["arducopter", "arduplane", "ardurover", "ardusub", "antennatracker"]:
        if name in text:
            return name
    frame_class = params.get("FRAME_CLASS")
    if frame_class is not None:
        return "ArduCopter/Multirotor likely"
    return None

def event_markers_from_tables(tables: Dict[str, Any], limit: int = 200) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []
    if "MODE" in tables:
        mode = tables["MODE"]
        col = get_col(mode, ["Mode", "Name", "ModeNum"])
        if col:
            for row in mode.to_dict(orient="records"):
                t = safe_float(row.get("TimeS"))
                if t is not None:
                    markers.append({"time_s": t, "label": f"MODE {row.get(col)}", "source": "MODE"})
    if "ERR" in tables:
        for row in tables["ERR"].to_dict(orient="records"):
            t = safe_float(row.get("TimeS"))
            if t is not None:
                markers.append({"time_s": t, "label": f"ERR {row.get('Subsys')}:{row.get('ECode')}", "source": "ERR"})
    if "EV" in tables:
        for row in tables["EV"].to_dict(orient="records"):
            t = safe_float(row.get("TimeS"))
            if t is not None:
                markers.append({"time_s": t, "label": f"EV {row.get('Id')}", "source": "EV"})
    if "MSG" in tables:
        msg = tables["MSG"]
        text_col = get_col(msg, ["Message", "Msg", "Text"])
        if text_col:
            for row in msg.to_dict(orient="records"):
                t = safe_float(row.get("TimeS"))
                if t is not None:
                    text = str(row.get(text_col, ""))[:80]
                    markers.append({"time_s": t, "label": f"MSG {text}", "source": "MSG"})
    return sorted(markers, key=lambda x: x["time_s"])[:limit]

def mode_segments_from_tables(tables: Dict[str, Any], log_end_s: Optional[float] = None) -> List[Dict[str, Any]]:
    if "MODE" not in tables:
        return []
    mode = tables["MODE"]
    mode_col = get_col(mode, ["Mode", "Name", "ModeNum"])
    if not mode_col or "TimeS" not in mode.columns:
        return []
    rows = mode.dropna(subset=["TimeS"]).sort_values("TimeS").to_dict(orient="records")
    segments = []
    for i, row in enumerate(rows):
        start = safe_float(row.get("TimeS"))
        if start is None:
            continue
        end = safe_float(rows[i + 1].get("TimeS")) if i + 1 < len(rows) else log_end_s
        duration = None if end is None else max(0.0, end - start)
        segments.append({"mode": str(row.get(mode_col)), "start_s": start, "end_s": end, "duration_s": duration})
    return segments

def vehicle_scope(index: Dict[str, Any]) -> Dict[str, Any]:
    vehicle_text = str(index.get("vehicle") or "")
    firmware = str(index.get("firmware") or "")
    combined = f"{vehicle_text} {firmware}".lower()
    params = index.get("parameters", {}) or {}
    primary = "Unknown"
    if "copter" in combined or any(str(k).startswith("MOT_") for k in params):
        primary = "Copter"
    elif "plane" in combined:
        primary = "Plane"
    elif "rover" in combined:
        primary = "Rover"
    elif "sub" in combined:
        primary = "Sub"
    confidence = "high" if primary == "Copter" else ("medium" if primary == "Unknown" else "low")
    notes = []
    if primary in {"Plane", "Rover", "Sub"}:
        notes.append(f"{primary} detected; generic parsing and plotting still work, but Copter tuning and motor-mix diagnosis are partial.")
    elif primary == "Unknown":
        notes.append("Vehicle type could not be confirmed from firmware strings or parameters; keep vehicle-specific conclusions conservative.")
    return {"primary_vehicle": primary, "copter_heuristics_confidence": confidence, "notes": notes}

def infer_firmware(msgs: Sequence[str]) -> Optional[str]:
    for m in msgs:
        if "Ardu" in m or "APM" in m or "Copter" in m:
            return m
    return msgs[0] if msgs else None

def missing_messages(index: Dict[str, Any], required: Sequence[str]) -> List[str]:
    present = set(index.get("messages", {}).keys())
    return [m for m in required if m not in present]

def clip_columns(df: Any) -> List[str]:
    if df is None:
        return []
    cols = []
    for col in df.columns:
        c = str(col).lower()
        if c == "clip" or re.fullmatch(r"clip\d+", c) or re.fullmatch(r"clp\d+", c):
            cols.append(col)
    return cols

def wrap_angle_deg(err: Any) -> Any:
    if np is None:
        return err
    return ((err + 180.0) % 360.0) - 180.0

def rms(values: Sequence[float]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    if not vals:
        return None
    return math.sqrt(sum(v*v for v in vals) / len(vals))

def percentile(values: Sequence[float], p: float) -> Optional[float]:
    vals = [float(v) for v in values if v is not None and not math.isnan(float(v))]
    if not vals:
        return None
    if np is not None:
        return float(np.nanpercentile(vals, p))
    vals = sorted(vals)
    idx = int(round((len(vals)-1)*p/100))
    return vals[idx]

def summarise_numeric(df: Any, fields: Sequence[str]) -> Dict[str, Any]:
    out = {}
    for f in fields:
        if f not in df.columns:
            continue
        s = pd.to_numeric(df[f], errors="coerce") if pd is not None else df[f]
        s = s.dropna()
        if len(s) == 0:
            continue
        out[f] = {
            "min": float(s.min()),
            "max": float(s.max()),
            "mean": float(s.mean()),
            "p95": float(s.quantile(0.95)),
            "p99": float(s.quantile(0.99)),
        }
    return out

def message_inventory_markdown(index: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"# Log index: {index.get('file_name')}\n")
    lines.append(f"- Vehicle: {index.get('vehicle') or 'unknown'}")
    lines.append(f"- Firmware: {index.get('firmware') or 'unknown'}")
    lines.append(f"- Duration: {index.get('duration_s')} s")
    lines.append(f"- Parameter count: {index.get('parameter_count')}\n")
    lines.append("## Messages\n")
    lines.append("| Message | Count | Fields |")
    lines.append("|---|---:|---|")
    for name, info in sorted(index.get("messages", {}).items()):
        fields = ", ".join(info.get("fields", [])[:30])
        if len(info.get("fields", [])) > 30:
            fields += ", ..."
        lines.append(f"| `{name}` | {info.get('count')} | {fields} |")
    if index.get("errors"):
        lines.append("\n## ERR messages\n")
        lines.append("| Time s | Subsys | ECode |")
        lines.append("|---:|---:|---:|")
        for e in index["errors"][:50]:
            lines.append(f"| {fmt(e.get('time_s'))} | {e.get('subsys')} | {e.get('ecode')} |")
    return "\n".join(lines) + "\n"

def fmt(v: Any, ndigits: int = 3) -> str:
    if v is None:
        return ""
    try:
        return f"{float(v):.{ndigits}f}"
    except Exception:
        return str(v)

def md_table(rows: List[Dict[str, Any]], columns: Sequence[str]) -> str:
    if not rows:
        return ""
    out = ["| " + " | ".join(columns) + " |", "|" + "|".join(["---"] * len(columns)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(r.get(c, "")) for c in columns) + " |")
    return "\n".join(out)

def classify_symptom(text: str, map_path: Optional[os.PathLike | str] = None) -> str:
    from ap_symptom_map import classify_symptom_from_map

    return classify_symptom_from_map(text, map_path)

def severity_rank(sev: str) -> int:
    order = {"safety-critical": 0, "likely-issue": 1, "worth-checking": 2, "info": 3}
    return order.get(sev, 9)

def confidence_rank(conf: str) -> int:
    order = {"high": 0, "medium": 1, "low": 2}
    return order.get(conf, 9)
