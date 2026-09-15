"""droid.apps.scan_projects contra proyectos gradle sinteticos en tmp_path."""
from pathlib import Path

from droid import apps


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_scan_projects_finds_application_id(tmp_path):
    root = tmp_path / "AndroidStudioProjects"
    _write(root / "MyApp" / "app" / "build.gradle", """
    plugins { id 'com.android.application' }
    android {
        defaultConfig { applicationId "com.example.myapp" }
    }
    """)
    found = apps.scan_projects(root)
    assert len(found) == 1
    app = found[0]
    assert app.app_id == "com.example.myapp"
    assert app.project == "MyApp"
    assert app.module == "app"


def test_scan_projects_uses_namespace_when_no_applicationid_but_is_application(tmp_path):
    root = tmp_path / "AndroidStudioProjects"
    _write(root / "OtherApp" / "app" / "build.gradle.kts", """
    plugins { id("com.android.application") }
    android { namespace = "com.example.other" }
    """)
    found = apps.scan_projects(root)
    assert len(found) == 1
    assert found[0].app_id == "com.example.other"


def test_scan_projects_skips_library_module_without_application_id(tmp_path):
    root = tmp_path / "AndroidStudioProjects"
    _write(root / "LibOnly" / "lib" / "build.gradle", """
    plugins { id 'com.android.library' }
    android { namespace = "com.example.lib" }
    """)
    assert apps.scan_projects(root) == []


def test_scan_projects_skips_build_and_hidden_dirs(tmp_path):
    root = tmp_path / "AndroidStudioProjects"
    _write(root / "MyApp" / "app" / "build" / "generated" / "build.gradle", """
    plugins { id 'com.android.application' }
    applicationId "com.example.generated"
    """)
    assert apps.scan_projects(root) == []


def test_scan_projects_picks_up_suffixes(tmp_path):
    root = tmp_path / "AndroidStudioProjects"
    _write(root / "MyApp" / "app" / "build.gradle", """
    plugins { id 'com.android.application' }
    applicationId "com.example.myapp"
    buildTypes {
        debug { applicationIdSuffix ".debug" }
    }
    """)
    found = apps.scan_projects(root)
    assert found[0].suffixes == [".debug"]


def test_project_app_matches_and_is_template():
    pa = apps.ProjectApp("com.example.myapp", "MyApp", "app", Path("/tmp/x/build.gradle"))
    assert pa.matches("com.example.myapp")
    assert pa.matches("com.example.myapp.debug")
    assert not pa.matches("com.example.other")
    template = apps.ProjectApp("com.example.demo", "Demo", "app", Path("/tmp/x/build.gradle"))
    assert template.is_template


def test_scan_projects_nonexistent_root_returns_empty(tmp_path):
    assert apps.scan_projects(tmp_path / "does_not_exist") == []
