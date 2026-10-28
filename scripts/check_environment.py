"""Check core dependency versions and existing SC2 resources."""
import csv
import importlib.metadata as metadata
import importlib.util
import json
import os
from pathlib import Path
import platform
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.environment import load_environment

CONFIG = load_environment(ROOT)


def inspect_environment():
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Python 3.12 is required")
    packages = {}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in project["project"]["dependencies"]:
        name, expected = requirement.split("==", 1)
        actual = metadata.version(name)
        if actual != expected:
            raise RuntimeError(f"{name}: expected {expected}, got {actual}")
        packages[name] = actual
    try:
        metadata.version("sc2")
    except metadata.PackageNotFoundError:
        pass
    else:
        raise RuntimeError("Legacy sc2 distribution must not coexist with burnysc2")

    sc2_path = Path(CONFIG["sc2_path"]).expanduser().resolve()
    os.environ["SC2PATH"] = str(sc2_path)
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["OPENHANDS_SUPPRESS_BANNER"] = "1"
    versions = []
    build_info = sc2_path / ".build.info"
    if build_info.is_file():
        with build_info.open(encoding="utf-8-sig") as stream:
            rows = csv.DictReader(stream, delimiter="|")
            versions = sorted({row["Version!STRING:0"] for row in rows if row.get("Active!DEC:1") == "1"})
    from sc2.paths import BINPATH, PF
    builds = sorted(int(p.name[4:]) for p in (sc2_path / "Versions").glob("Base*")
                    if (p / BINPATH[PF]).is_file())
    if not builds:
        raise RuntimeError(f"No SC2 executable found under {sc2_path}")
    notes = []
    if ((CONFIG.get("sc2_build") is not None and builds[-1] != CONFIG["sc2_build"])
            or (CONFIG.get("installed_game_version") and versions
                and versions != [CONFIG["installed_game_version"]])):
        notes.append("SC2 differs from the configured version hint; using the installed client")

    from sc2 import maps
    smoke_map = Path(maps.get(CONFIG["check_map"]).path)
    if not smoke_map.is_file():
        raise RuntimeError(f"Map not found: {smoke_map}")
    for module_name in ("sc2.bot_ai", "sc2.main", "openhands.sdk"):
        if importlib.util.find_spec(module_name) is None:
            raise RuntimeError(f"Module not found: {module_name}")
    return {
        "status": "ok", "python": platform.python_version(),
        "python_executable": sys.executable, "core_packages": packages,
        "sc2": {"path": str(sc2_path), "installed_versions": versions,
                "build": builds[-1], "smoke_map": str(smoke_map)},
        "notes": notes,
    }


def main():
    print(json.dumps(inspect_environment(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
