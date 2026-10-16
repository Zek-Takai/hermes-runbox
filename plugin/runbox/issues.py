"""Turn checker, test and traceback output into one plain issue list.

An issue is ``{"file", "line", "col"?, "code", "message", "source"}``; ``render`` gives the
one-line form, e.g. ``broken.py:2:7: F821 Undefined name 'x'``.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET

WORKSPACE = "/workspace/"
MAX_MESSAGE = 300
_TB_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+)')
_TB_ERROR = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)|AssertionError)(?::\s*(.*))?$")
_LOCATION = re.compile(r"^(\S+?\.py):(\d+):")


def _rel(path: str) -> str:
    path = path or ""
    if path.startswith(WORKSPACE):
        return path[len(WORKSPACE):]
    return path


def _short(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= MAX_MESSAGE else text[:MAX_MESSAGE - 3] + "..."


def render(issue: dict) -> str:
    where = issue.get("file") or "?"
    if issue.get("line"):
        where += f":{issue['line']}"
        if issue.get("col"):
            where += f":{issue['col']}"
    code = f" {issue['code']}" if issue.get("code") else ""
    return f"{where}:{code} {issue.get('message', '')}".rstrip()


def ruff_issues(stdout: str) -> list[dict]:
    """Parse ``ruff check --output-format json``."""
    try:
        rows = json.loads(stdout or "[]")
    except ValueError:
        return []
    issues = []
    for row in rows if isinstance(rows, list) else []:
        loc = row.get("location") or {}
        issues.append({
            "file": _rel(row.get("filename", "")),
            "line": loc.get("row"),
            "col": loc.get("column"),
            "code": row.get("code") or "syntax",
            "message": _short(row.get("message", "")),
            "source": "ruff",
        })
    issues.sort(key=lambda i: (i["file"], i["line"] or 0, i["col"] or 0))
    return issues


def _failure_location(text: str, fallback_file: str, fallback_line) -> tuple[str, int | None]:
    """The deepest workspace frame in a pytest traceback (``--tb=short``)."""
    found = None
    for line in (text or "").splitlines():
        match = _LOCATION.match(line.strip())
        if match and (not match.group(1).startswith("/") or match.group(1).startswith(WORKSPACE)):
            found = (_rel(match.group(1)), int(match.group(2)))
    if found:
        return found
    return fallback_file, fallback_line


def pytest_report(junit_xml: str) -> tuple[dict, list[dict]]:
    """Parse a pytest junit report (``junit_family=xunit1``) into (summary, issues)."""
    summary = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "passed": 0}
    junit_xml = (junit_xml or "").strip()
    if not junit_xml:
        return summary, []
    try:
        root = ET.fromstring(junit_xml)
    except ET.ParseError:
        return summary, []
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    issues = []
    for suite in suites:
        for key in ("tests", "failures", "errors", "skipped"):
            summary[key] += int(suite.get(key, 0) or 0)
        for case in suite.iter("testcase"):
            for kind in ("failure", "error"):
                node = case.find(kind)
                if node is None:
                    continue
                file_attr = _rel(case.get("file", ""))
                line_attr = case.get("line")
                fallback_line = int(line_attr) + 1 if line_attr and line_attr.isdigit() else None
                file, line = _failure_location(node.text or "", file_attr, fallback_line)
                name = case.get("name", "")
                test_id = f"{file_attr or case.get('classname', '')}::{name}".strip(":")
                issues.append({
                    "file": file,
                    "line": line,
                    "code": "test-failed" if kind == "failure" else "test-error",
                    "message": _short(f"{test_id}: {node.get('message') or (node.text or '').strip()}"),
                    "source": "pytest",
                })
    summary["passed"] = max(0, summary["tests"] - summary["failures"] - summary["errors"] - summary["skipped"])
    return summary, issues


def traceback_issue(stderr: str) -> dict | None:
    """The failing workspace frame and the exception line of a Python traceback, if any."""
    lines = (stderr or "").splitlines()
    if not any(line.startswith("Traceback (most recent call last)") for line in lines):
        return None
    frame = None
    for line in lines:
        match = _TB_FRAME.match(line)
        if match and (match.group(1).startswith(WORKSPACE) or not match.group(1).startswith("/")):
            frame = (_rel(match.group(1)), int(match.group(2)))
    error = next((line.strip() for line in reversed(lines) if _TB_ERROR.match(line.strip())), None)
    if frame is None and error is None:
        return None
    code, _, message = (error or "").partition(":")
    return {
        "file": frame[0] if frame else "",
        "line": frame[1] if frame else None,
        "code": code.strip() or "error",
        "message": _short(message.strip() or code.strip()),
        "source": "python",
    }
