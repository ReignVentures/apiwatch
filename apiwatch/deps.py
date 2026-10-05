"""Dependency pins from manifests and lockfiles, as canonical `dep:<ecosystem>:name@version` lines.

Lockfiles put a package's name and version on separate lines (package-lock.json, poetry.lock, uv.lock,
yarn.lock, Pipfile.lock, composer.lock), so a plain text search for "stripe 16" can't see them. A record's
`repo_context` can say "dep:pypi:stripe@16." and match whichever file pins it. Use the ecosystem form: the
same name can be two packages (stripe on PyPI and on npm).

Offline and best-effort: a file that doesn't parse adds nothing and never stops a scan. Resolved versions
(lockfiles) win: when a directory has a lockfile, its manifests' version ranges are left out.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

_VER = re.compile(r"\d+(?:\.[0-9A-Za-z]+)*(?:[-+][0-9A-Za-z.-]+)?")

LOCKFILES = {"package-lock.json": "npm", "npm-shrinkwrap.json": "npm", "yarn.lock": "npm", "pnpm-lock.yaml": "npm",
             "poetry.lock": "pypi", "uv.lock": "pypi", "pipfile.lock": "pypi", "gemfile.lock": "rubygems",
             "composer.lock": "packagist", "cargo.lock": "cargo"}
MANIFESTS = {"package.json": "npm", "pyproject.toml": "pypi", "pipfile": "pypi", "composer.json": "packagist", "go.mod": "go"}


def is_requirements(name: str, parent: str = "") -> bool:
    """requirements.txt, dev-requirements.txt, requirements-dev.in, constraints.txt, requirements/base.txt."""
    n = name.lower()
    return (n.endswith((".txt", ".in")) and ("requirements" in n or n.startswith("constraints"))) or \
        (parent.lower() == "requirements" and n.endswith((".txt", ".in")))


def ecosystem(path: Path) -> str:
    n = path.name.lower()
    if n in LOCKFILES:
        return LOCKFILES[n]
    if n in MANIFESTS:
        return MANIFESTS[n]
    return "pypi" if is_requirements(path.name, path.parent.name) else ""


def is_lockfile(path: Path) -> bool:
    return path.name.lower() in LOCKFILES


def _ver(spec) -> str:
    """The first concrete version in a spec: "^23.1.0" → "23.1.0", ">=16.0,<17" → "16.0", "v22.0.0" → "22.0.0"."""
    m = _VER.search(str(spec or ""))
    return m.group(0) if m else ""


def _pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# ---------- formats ----------

def _npm_lock(data: dict) -> list[tuple[str, str]]:
    out = []
    for key, v in (data.get("packages") or {}).items():          # v2/v3
        if "node_modules/" in key and isinstance(v, dict) and not v.get("link"):
            out.append((v.get("name") or key.rsplit("node_modules/", 1)[-1], v.get("version", "")))

    def walk(deps):                                               # v1, nested
        for k, v in (deps or {}).items():
            if isinstance(v, dict):
                out.append((k, v.get("version", "")))
                walk(v.get("dependencies"))
    walk(data.get("dependencies"))
    return out


def _json_manifest(data: dict) -> list[tuple[str, str]]:
    out = []
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies", "require", "require-dev"):
        for name, spec in (data.get(key) or {}).items():
            if isinstance(spec, str):
                alias = re.match(r"npm:(@?[^@]+)@(.+)", spec)        # "stripe16": "npm:stripe@16.0.0"
                out.append((alias.group(1), _ver(alias.group(2))) if alias else (name, _ver(spec)))
    return out


def _toml_packages(text: str) -> list[tuple[str, str]]:            # poetry.lock, uv.lock, Cargo.lock
    data = tomllib.loads(text)
    return [(p.get("name", ""), p.get("version", "")) for p in data.get("package", []) if isinstance(p, dict)]


_PEP508 = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*(?:\[[^\]]*\])?\s*\(?\s*([^;#]*)")


def _pep508(req: str) -> tuple[str, str] | None:
    """'stripe<17,>=16' / 'stripe (>=16.0)' / 'stripe[async]==16.0.0; python_version>"3"' → (name, lower bound)."""
    m = _PEP508.match(req)
    if not m or not m.group(2):
        return None
    specs = [s.strip() for s in m.group(2).split(",") if s.strip()]
    lower = [s for s in specs if s.startswith(("==", ">=", "~=", "===", ">"))] or specs
    v = _ver(lower[0]) if lower else ""
    return (m.group(1), v) if v else None


def _pyproject(text: str) -> list[tuple[str, str]]:
    data = tomllib.loads(text)
    out = []
    project = data.get("project") or {}
    reqs = list(project.get("dependencies") or [])
    for group in (project.get("optional-dependencies") or {}).values():
        reqs += list(group or [])
    for group in (data.get("dependency-groups") or {}).values():
        reqs += [r for r in group or [] if isinstance(r, str)]
    out += [p for r in reqs if isinstance(r, str) and (p := _pep508(r))]
    poetry = (data.get("tool") or {}).get("poetry") or {}
    tables = [poetry.get("dependencies") or {}, poetry.get("dev-dependencies") or {}]
    tables += [(g or {}).get("dependencies") or {} for g in (poetry.get("group") or {}).values()]
    for table in tables:
        for name, spec in table.items():
            if name.lower() == "python":
                continue
            v = _ver(spec.get("version") if isinstance(spec, dict) else spec)
            if v:
                out.append((name, v))
    return out


def _pipfile(text: str) -> list[tuple[str, str]]:
    data = tomllib.loads(text)
    out = []
    for section in ("packages", "dev-packages"):
        for name, spec in (data.get(section) or {}).items():
            v = _ver(spec.get("version") if isinstance(spec, dict) else spec)
            if v:
                out.append((name, v))
    return out


def _go_mod(text: str) -> list[tuple[str, str]]:
    """require lines and blocks, plus replace targets; exclude/retract blocks are ignored."""
    out, block = [], ""
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if block:
            if line == ")":
                block = ""
                continue
            stmt = f"{block} {line}"
        elif line.endswith("(") and line.split()[0] in ("require", "replace", "exclude", "retract"):
            block = line.split()[0]
            continue
        else:
            stmt = line
        parts = stmt.split()
        if parts[0] == "require" and len(parts) >= 3:
            out.append((parts[1], _ver(parts[2])))
        elif parts[0] == "replace" and "=>" in parts:
            rhs = parts[parts.index("=>") + 1:]
            if len(rhs) >= 2:
                out.append((rhs[0], _ver(rhs[1])))
    return out


def pins(path: Path, text: str) -> list[tuple[str, str]]:
    """(name, version) pairs a manifest or lockfile declares."""
    name = path.name.lower()
    text = text.lstrip("﻿")
    if name in ("package.json", "composer.json"):
        return _json_manifest(json.loads(text))
    if name in ("package-lock.json", "npm-shrinkwrap.json"):
        return _npm_lock(json.loads(text))
    if name == "pipfile.lock":
        data = json.loads(text)
        return [(k, _ver(v.get("version", ""))) for sect in ("default", "develop")
                for k, v in (data.get(sect) or {}).items() if isinstance(v, dict)]
    if name == "composer.lock":
        data = json.loads(text)
        return [(p.get("name", ""), _ver(p.get("version", ""))) for sect in ("packages", "packages-dev")
                for p in (data.get(sect) or []) if isinstance(p, dict)]
    if name in ("poetry.lock", "uv.lock", "cargo.lock"):
        return _toml_packages(text)
    if name == "pyproject.toml":
        return _pyproject(text)
    if name == "pipfile":
        return _pipfile(text)
    if name == "go.mod":
        return _go_mod(text)
    if name == "yarn.lock":                                      # "stripe@^23.0.0":  version "23.0.0" (v1 and berry)
        out, current = [], None
        for line in text.splitlines():                           # line by line: no regex backtracking on odd files
            if line and not line[0].isspace() and line.rstrip().endswith(":"):
                first = line.split(",")[0].strip().strip('"')
                m = re.match(r"(@?[^@\s]+)@", first)
                current = m.group(1) if m else None
            elif current and line.lstrip().startswith("version"):
                v = line.strip()[len("version"):].lstrip(": ").strip().strip('"')
                out.append((current, v))
                current = None
        return out
    if name == "pnpm-lock.yaml":
        if re.search(r"(?m)^lockfileVersion:[ \t]*'?5", text):     # v5: /stripe/16.0.0: or /@scope/name/1.2.3_peer:
            return re.findall(r"(?m)^[ \t]{2}/(@?[^/\s]+(?:/[^/\s@]+)?)/(\d[^_:(\s]*)", text)
        return re.findall(r"(?m)^[ \t]+'?/?(@?[^@\s'/][^@\s']*)@(\d[^:'\s(]*)", text)   # v6+: /stripe@23.0.0: or stripe@23.0.0:
    if name == "gemfile.lock":                                   #     stripe (20.0.0)
        return re.findall(r"(?m)^[ \t]{4}([A-Za-z0-9_.-]+) \(([0-9][^)]*)\)", text)
    if is_requirements(path.name, path.parent.name):
        out = []
        for raw in text.splitlines():
            line = raw.strip()
            if line and not line.startswith(("#", "-", "git+", "http")) and (p := _pep508(line)):
                out.append(p)
        return out
    return []


def dep_lines(path: Path, text: str) -> str:
    """Canonical lines for a manifest: 'dep:pypi:stripe@16.0.0' plus an unqualified 'dep:stripe@16.0.0'. A bare
    major ("^23") becomes "23.x", so "dep:npm:stripe@23." matches it. Lowercased; PyPI names normalized
    (PEP 503). Never raises: a malformed file contributes nothing."""
    try:
        found = pins(path, text)
    except Exception:                        # noqa: BLE001 — one odd file must not stop a scan
        return ""
    eco = ecosystem(path)
    out = []
    for n, v in found:
        n, v = str(n or "").strip(), str(v or "").strip()
        if not (n and v and _VER.match(v)):
            continue
        n = _pep503(n) if eco == "pypi" else n.lower()
        v = v.lower() if "." in v else f"{v.lower()}.x"
        out.append(f"dep:{n}@{v}")
        if eco:
            out.append(f"dep:{eco}:{n}@{v}")
    return "\n".join(out)
