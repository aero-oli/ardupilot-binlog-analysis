#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

INSTALL_HINT = "Install dependencies with: pip install -r requirements.txt"
MIN_PYTHON = (3, 10)

REQUIRED_PACKAGES = [
    ("pymavlink", "pymavlink"),
    ("pandas", "pandas"),
    ("numpy", "numpy"),
    ("plotly", "plotly"),
    ("PyYAML", "yaml"),
]

CORE_MODULES = [
    "ap_common",
    "ap_log_validate",
    "ap_log_index",
    "ap_log_extract",
    "ap_log_diagnose",
    "ap_log_investigation_manifest",
    "ap_log_custom_plot",
    "ap_log_fft",
]


class FakeMsg:
    def __init__(self, typ: str, **fields: Any):
        self.typ = typ
        self.fields = fields

    def get_type(self) -> str:
        return self.typ

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.fields)


def _status(ok: bool, required: bool = True) -> str:
    if ok:
        return "pass"
    return "fail" if required else "warn"


def _exc_message(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _add_check(checks: List[Dict[str, Any]], name: str, ok: bool, message: str, required: bool = True, detail: Any = None) -> None:
    item: Dict[str, Any] = {
        "name": name,
        "status": _status(ok, required),
        "required": bool(required),
        "message": message,
    }
    if detail is not None:
        item["detail"] = detail
    checks.append(item)


def check_python(checks: List[Dict[str, Any]]) -> None:
    version = sys.version_info
    ok = version >= MIN_PYTHON
    version_text = f"{version.major}.{version.minor}.{version.micro}"
    required_text = ".".join(str(v) for v in MIN_PYTHON)
    message = f"Python {version_text}" if ok else f"Python {version_text}; Python {required_text}+ is required"
    _add_check(checks, "python_version", ok, message, detail={"version": version_text, "minimum": required_text})


def check_requirements_file(checks: List[Dict[str, Any]]) -> None:
    path = REPO_ROOT / "requirements.txt"
    ok = path.exists()
    message = f"Found {path.name}" if ok else f"Missing requirements.txt. {INSTALL_HINT}"
    _add_check(checks, "requirements_txt", ok, message, detail={"path": str(path)})


def check_packages(checks: List[Dict[str, Any]]) -> None:
    for package_name, import_name in REQUIRED_PACKAGES:
        try:
            module = importlib.import_module(import_name)
            version = getattr(module, "__version__", None)
            message = f"{package_name} import OK" + (f" ({version})" if version else "")
            _add_check(checks, f"package:{package_name}", True, message, detail={"import": import_name, "version": version})
        except Exception as exc:
            _add_check(
                checks,
                f"package:{package_name}",
                False,
                f"Cannot import {import_name} for {package_name}. {INSTALL_HINT}",
                detail={"import": import_name, "error": _exc_message(exc)},
            )


def check_core_modules(checks: List[Dict[str, Any]]) -> None:
    for module_name in CORE_MODULES:
        try:
            importlib.import_module(module_name)
            _add_check(checks, f"module:{module_name}", True, f"{module_name} import OK")
        except Exception as exc:
            _add_check(
                checks,
                f"module:{module_name}",
                False,
                f"Cannot import {module_name}. {INSTALL_HINT}",
                detail={"error": _exc_message(exc), "traceback": traceback.format_exc(limit=4)},
            )


def check_output_dir(checks: List[Dict[str, Any]], output_dir: Path) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        probe = output_dir / ".skill_doctor_write_test"
        probe.write_text("ok\n", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "ok\n":
            raise RuntimeError("write/read probe mismatch")
        probe.unlink(missing_ok=True)
        _add_check(checks, "output_directory", True, f"Can write to {output_dir}", detail={"path": str(output_dir)})
    except Exception as exc:
        _add_check(
            checks,
            "output_directory",
            False,
            f"Cannot create/write output directory {output_dir}",
            detail={"path": str(output_dir), "error": _exc_message(exc)},
        )


def check_synthetic_path(checks: List[Dict[str, Any]]) -> None:
    try:
        import pandas as pd

        from ap_common import collect_dataflash
        from ap_compass_yaw import mag_field_frame, yaw_error_frame
        from ap_log_extract import write_jsonl_stream

        messages = [
            FakeMsg("ATT", TimeUS=0, DesYaw=10.0, Yaw=9.5),
            FakeMsg("RATE", TimeUS=1000000, YDes=0.0, Y=0.0, YOut=0.1),
            FakeMsg("MAG", TimeUS=1000000, MagX=100.0, MagY=20.0, MagZ=40.0),
        ]
        rows, index, stats = collect_dataflash(messages, include=["ATT", "RATE", "MAG"], source="skill-doctor")
        tables = {
            "ATT": pd.DataFrame(rows.get("ATT", [])),
            "MAG": pd.DataFrame(rows.get("MAG", [])),
        }
        yaw = yaw_error_frame(tables)
        mag = mag_field_frame(tables)
        if yaw is None or "yaw_error_abs" not in yaw.columns:
            raise RuntimeError("synthetic ATT yaw-error dataframe was not produced")
        if mag is None or "mag_field" not in mag.columns:
            raise RuntimeError("synthetic MAG field dataframe was not produced")
        with tempfile.TemporaryDirectory(prefix="ap_skill_doctor_") as tmp:
            extracted, _stream_index, stream_stats = write_jsonl_stream(messages, Path(tmp), include=["ATT"], source="skill-doctor")
        if extracted.get("ATT", {}).get("rows") != 1:
            raise RuntimeError("synthetic JSONL extraction did not write one ATT row")
        _add_check(
            checks,
            "synthetic_dataframe_path",
            True,
            "Synthetic message collection, DataFrame helpers, and JSONL extraction OK",
            detail={
                "messages_indexed": sorted(index.get("messages", {}).keys()),
                "rows_collected": stats.get("collected_rows"),
                "jsonl_rows": stream_stats.get("collected_rows"),
            },
        )
    except Exception as exc:
        _add_check(
            checks,
            "synthetic_dataframe_path",
            False,
            f"Synthetic no-log workflow failed. {INSTALL_HINT}",
            detail={"error": _exc_message(exc), "traceback": traceback.format_exc(limit=4)},
        )


def summarize(checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    required_failures = [c for c in checks if c["status"] == "fail" and c["required"]]
    warnings = [c for c in checks if c["status"] == "warn"]
    if required_failures:
        exit_code = 2
        status = "fail"
    elif warnings:
        exit_code = 1
        status = "warn"
    else:
        exit_code = 0
        status = "pass"
    return {
        "status": status,
        "exit_code": exit_code,
        "install_hint": INSTALL_HINT,
        "checks": checks,
        "summary": {
            "passed": sum(1 for c in checks if c["status"] == "pass"),
            "warnings": len(warnings),
            "failed_required": len(required_failures),
        },
    }


def run_doctor(output_dir: Path) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    check_python(checks)
    check_requirements_file(checks)
    check_packages(checks)
    check_core_modules(checks)
    check_output_dir(checks, output_dir)
    check_synthetic_path(checks)
    return summarize(checks)


def print_human(result: Dict[str, Any]) -> None:
    summary = result["summary"]
    print("ArduPilot binlog skill doctor")
    print(f"Status: {result['status']} ({summary['passed']} passed, {summary['warnings']} warnings, {summary['failed_required']} required failures)")
    for check in result["checks"]:
        marker = "OK" if check["status"] == "pass" else ("WARN" if check["status"] == "warn" else "FAIL")
        print(f"[{marker}] {check['name']}: {check['message']}")
        detail = check.get("detail") or {}
        error = detail.get("error") if isinstance(detail, dict) else None
        if error:
            print(f"      {error}")
    if result["exit_code"] == 2:
        print(result["install_hint"])


def write_result(path: Path, result: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, sort_keys=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check dependencies and local environment for the ArduPilot binlog analysis skill.")
    parser.add_argument("--json", dest="json_path", default=None, help="Optional JSON result path, for example out/skill_doctor.json")
    parser.add_argument("--out-dir", default=os.environ.get("AP_SKILL_DOCTOR_OUT", "out/skill_doctor"), help="Directory used for write-probe checks")
    args = parser.parse_args()

    result = run_doctor(Path(args.out_dir))
    print_human(result)
    if args.json_path:
        write_result(Path(args.json_path), result)
        print(f"Wrote JSON: {args.json_path}")
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
