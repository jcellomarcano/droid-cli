"""Fase 0 del inspector: identifica apps de TUS proyectos (AndroidStudioProjects) instaladas en un dispositivo."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import adb as adbmod

APP_ID_RE = re.compile(r'applicationId\s*=?\s*["\']([A-Za-z0-9_.]+)["\']')
SUFFIX_RE = re.compile(r'applicationIdSuffix\s*=?\s*["\']([A-Za-z0-9_.]+)["\']')
NAMESPACE_RE = re.compile(r'namespace\s*=?\s*["\']([A-Za-z0-9_.]+)["\']')
SKIP_DIRS = {"build", ".gradle", ".git", ".idea", "node_modules", ".kotlin", "out", ".cxx", "Pods", ".venv"}
SKIP_PACKAGES = ("com.example.",)   # ids de plantilla: se marcan pero se muestran en gris


@dataclass
class ProjectApp:
    app_id: str
    project: str
    module: str
    gradle: Path
    suffixes: List[str] = field(default_factory=list)

    def matches(self, pkg: str) -> bool:
        return pkg == self.app_id or pkg.startswith(self.app_id + ".")

    @property
    def is_template(self) -> bool:
        return self.app_id.startswith(SKIP_PACKAGES)

    @property
    def is_archived(self) -> bool:
        n = self.project.lower()
        return n.startswith("_") or "archiv" in n or "legacy" in n or "backup" in n or "old" == n[-3:]

    @property
    def mtime(self) -> float:
        try:
            return self.gradle.stat().st_mtime
        except OSError:
            return 0.0


@dataclass
class InstalledApp:
    package: str
    project: ProjectApp
    version_name: str = ""
    version_code: str = ""
    debuggable: bool = False
    pids: List[int] = field(default_factory=list)
    last_update: str = ""
    target_sdk: str = ""
    others: List["ProjectApp"] = field(default_factory=list)


def _walk_gradle(base: Path, max_depth: int):
    for dirpath, dirnames, filenames in os.walk(base):
        rel = Path(dirpath).relative_to(base)
        depth = len(rel.parts)
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f in ("build.gradle", "build.gradle.kts"):
                yield Path(dirpath) / f


def scan_projects(root: Path, max_depth: int = 4) -> List[ProjectApp]:
    apps: List[ProjectApp] = []
    if not root.exists():
        return apps
    for project in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not project.is_dir() or project.name.startswith(".") or project.name in SKIP_DIRS:
            continue
        for gradle in _walk_gradle(project, max_depth):
            try:
                text = gradle.read_text(errors="replace")
            except OSError:
                continue
            is_app = ("com.android.application" in text or "android.application" in text
                      or "androidApplication" in text or "android-application" in text)
            m = APP_ID_RE.search(text)
            if not is_app and not m:
                continue
            app_id = m.group(1) if m else None
            if not app_id:
                ns = NAMESPACE_RE.search(text)
                app_id = ns.group(1) if ns else None
            if not app_id:
                continue
            module = str(gradle.parent.relative_to(project)) or "."
            if any(a.app_id == app_id and a.project == project.name for a in apps):
                continue
            apps.append(ProjectApp(app_id, project.name, module, gradle, sorted(set(SUFFIX_RE.findall(text)))))
    return apps


def installed_packages(serial: str) -> List[str]:
    out = adbmod.shell(serial, "pm list packages 2>/dev/null", timeout=20)
    return [l.partition(":")[2].strip() for l in out.splitlines() if l.startswith("package:")]


def package_details(serial: str, packages: List[str]) -> Dict[str, dict]:
    if not packages:
        return {}
    script = ";".join(
        f'echo "##{p}";dumpsys package {p} 2>/dev/null | grep -m4 -E "versionName=|versionCode=|pkgFlags=|lastUpdateTime=";echo "PIDS=$(pidof {p} 2>/dev/null)"'
        for p in packages
    )
    out = adbmod.shell(serial, script, timeout=60)
    result: Dict[str, dict] = {}
    cur: Optional[dict] = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("##"):
            cur = result.setdefault(line[2:], {})
            continue
        if cur is None:
            continue
        if line.startswith("versionName="):
            cur["version_name"] = line.split("=", 1)[1]
        elif line.startswith("versionCode="):
            cur["version_code"] = line.split()[0].split("=", 1)[1]
            m = re.search(r"targetSdk=(\d+)", line)
            if m:
                cur["target_sdk"] = m.group(1)
        elif line.startswith("pkgFlags=") or line.startswith("flags="):
            cur["debuggable"] = "DEBUGGABLE" in line
        elif line.startswith("lastUpdateTime="):
            cur["last_update"] = line.split("=", 1)[1]
        elif line.startswith("PIDS="):
            cur["pids"] = [int(x) for x in line[5:].split() if x.isdigit()]
    return result


def find_project_apps(serial: str, projects: List[ProjectApp]) -> List[InstalledApp]:
    installed = installed_packages(serial)
    matches: List[InstalledApp] = []
    for pkg in installed:
        cands = [pa for pa in projects if pa.matches(pkg)]
        if not cands:
            continue
        cands.sort(key=lambda pa: (pa.is_archived, -pa.mtime))
        matches.append(InstalledApp(pkg, cands[0], others=cands[1:]))
    details = package_details(serial, [m.package for m in matches])
    for m in matches:
        d = details.get(m.package, {})
        m.version_name = d.get("version_name", "")
        m.version_code = d.get("version_code", "")
        m.debuggable = d.get("debuggable", False)
        m.pids = d.get("pids", [])
        m.last_update = d.get("last_update", "")
        m.target_sdk = d.get("target_sdk", "")
    matches.sort(key=lambda m: (not m.debuggable, m.project.project.lower(), m.package))
    return matches
