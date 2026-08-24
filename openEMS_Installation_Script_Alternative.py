# openEMS Colab installer.
"""
Authored by Onri Jay Benally (2026)

Open Access (CC-BY-4.0)
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path


# =============================================================================
# CONTROL KNOBS
# =============================================================================

# Installation strategy: "auto", "apt", "source", or "none".
INSTALL_MODE = "auto"
RUN_APT_UPDATE = True
APT_RETRIES = 3

# Current openEMS documentation favors the official source installer on Linux.
# This PPA switch is an optional compatibility escape hatch and stays disabled
# unless the repository is independently confirmed for the active Ubuntu image.
TRY_OPENEMS_PPA = False
OPENEMS_PPA = "ppa:openems/openems"
PPA_REQUIRED = False

# Source installation controls.
OPENEMS_REPOSITORY = "https://github.com/thliebig/openEMS-Project.git"
SOURCE_DIR = Path("/content/openEMS-Project")
INSTALL_PREFIX = Path("/content/openEMS")
UPDATE_EXISTING_SOURCE_TREE = True
BUILD_GUI = False
BUILD_CTB = False
BUILD_MPI = False
BUILD_JOBS = 0  # 0 selects a RAM-aware automatic value.
RAM_GIB_PER_BUILD_JOB = 1.5

# Bridge requirements.
PYTHON_BRIDGE_REQUIRED = False
OCTAVE_BRIDGE_REQUIRED = False
APPCSXCAD_REQUIRED = False

# Diagnostics and memory warnings.
WARN_ON_LOW_MEMORY = True
MIN_AVAILABLE_RAM_GIB = 2.0
MIN_FREE_DISK_GIB_FOR_SOURCE_BUILD = 6.0

LOG_DIR = Path("/content/openems_install_logs")
BRIDGE_MODULE_PATH = Path("/content/openems_colab_bridge.py")
SMOKE_TEST_DIR = Path("/content/openems_smoke_tests")

# Fast APT path. Only the openems package is mandatory for this path to count
# as a valid openEMS installation source. The rest are installed when present.
APT_REQUIRED_PACKAGES = (
    "openems",
)

APT_OPTIONAL_PACKAGES = (
    "python3-openems",
    "libopenems-dev",
    "octave-openems",
    "octave",
    "python3-numpy",
    "python3-h5py",
    "python3-matplotlib",
    "python3-scipy",
)

# Minimal bootstrap packages needed before the official source tree can manage
# its own dependency installation.
SOURCE_BOOTSTRAP_PACKAGES = (
    "ca-certificates",
    "git",
)

SOURCE_MPI_PACKAGES = (
    "openmpi-bin",
    "libopenmpi-dev",
)

# Extra candidate interpreters may be appended here. System Python discovery
# is automatic, so version-specific paths generally belong here only as an
# explicit override.
EXTRA_PYTHON_CANDIDATES: tuple[str, ...] = ()


# =============================================================================
# COMMAND HELPERS
# =============================================================================


def run_command(
    command: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    log_name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, print its output tail, and optionally save a full log."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    print("$ " + " ".join(command))

    result = subprocess.run(
        command,
        check=False,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    output = result.stdout or ""
    if log_name is not None:
        path = LOG_DIR / log_name
        path.write_text(output, encoding="utf-8", errors="replace")
        print(f"log written to {path}")

    if output:
        print(output[-5000:])

    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed with code {result.returncode}: "
            + " ".join(command)
        )

    return result


def command_exists(command_name: str) -> bool:
    """Return True if a command is available on PATH."""
    return shutil.which(command_name) is not None


def apt_env() -> dict[str, str]:
    """Return a deterministic noninteractive environment for APT commands."""
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


def apt_update(*, log_name: str = "apt_update.log") -> bool:
    """Refresh APT metadata and return whether the operation succeeded."""
    if not RUN_APT_UPDATE:
        return True

    result = run_command(
        [
            "apt-get",
            "-o",
            f"Acquire::Retries={APT_RETRIES}",
            "update",
            "-qq",
        ],
        check=False,
        env=apt_env(),
        log_name=log_name,
    )
    return result.returncode == 0


def apt_candidate_exists(package_name: str) -> bool:
    """Return True if APT reports an installable candidate for a package."""
    if not command_exists("apt-cache"):
        return False

    result = run_command(
        ["apt-cache", "policy", package_name],
        check=False,
        env=apt_env(),
        log_name=f"apt_policy_{package_name}.log",
    )
    text = result.stdout or ""
    return "Candidate:" in text and "Candidate: (none)" not in text


def apt_install(packages: tuple[str, ...], *, log_name: str) -> None:
    """Install an explicit package tuple with APT retries enabled."""
    if not packages:
        return

    run_command(
        [
            "apt-get",
            "-o",
            f"Acquire::Retries={APT_RETRIES}",
            "install",
            "-y",
            "-qq",
            *packages,
        ],
        check=True,
        env=apt_env(),
        log_name=log_name,
    )


def add_openems_ppa() -> bool:
    """Attempt to add the optional openEMS PPA and report success."""
    if not TRY_OPENEMS_PPA:
        return False

    print(f"Attempting optional PPA: {OPENEMS_PPA}")
    if not command_exists("add-apt-repository"):
        apt_install(
            ("software-properties-common",),
            log_name="apt_install_software_properties.log",
        )

    result = run_command(
        ["add-apt-repository", "-y", OPENEMS_PPA],
        check=False,
        env=apt_env(),
        log_name="add_openems_ppa.log",
    )

    if result.returncode == 0:
        apt_update(log_name="apt_update_after_ppa.log")
        return True

    message = (
        f"Optional PPA failed with code {result.returncode}: {OPENEMS_PPA}"
    )
    if PPA_REQUIRED:
        raise RuntimeError(message)

    print(message)
    return False


# =============================================================================
# SYSTEM AND MEMORY DIAGNOSTICS
# =============================================================================


def read_meminfo_bytes() -> dict[str, int]:
    """Read selected Linux memory counters from /proc/meminfo."""
    path = Path("/proc/meminfo")
    if not path.exists():
        return {}

    values: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", maxsplit=1)
        fields = raw_value.strip().split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError:
            continue

        multiplier = 1024 if len(fields) > 1 and fields[1] == "kB" else 1
        values[key] = value * multiplier

    return values


def read_first_integer(paths: tuple[Path, ...]) -> int | None:
    """Return the first finite nonnegative integer read from candidate files."""
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue

        if raw == "max":
            continue

        try:
            value = int(raw)
        except ValueError:
            continue

        if value >= 0:
            return value

    return None


def memory_status() -> dict[str, float | None]:
    """Return RAM counters adjusted for common Linux cgroup limits."""
    meminfo = read_meminfo_bytes()
    host_total = meminfo.get("MemTotal")
    host_available = meminfo.get("MemAvailable")

    cgroup_limit = read_first_integer(
        (
            Path("/sys/fs/cgroup/memory.max"),
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
        )
    )
    cgroup_current = read_first_integer(
        (
            Path("/sys/fs/cgroup/memory.current"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        )
    )

    effective_total = host_total
    if cgroup_limit is not None and cgroup_limit < (1 << 60):
        if effective_total is None:
            effective_total = cgroup_limit
        else:
            effective_total = min(effective_total, cgroup_limit)

    effective_available = host_available
    if (
        cgroup_limit is not None
        and cgroup_current is not None
        and cgroup_limit < (1 << 60)
    ):
        cgroup_available = max(cgroup_limit - cgroup_current, 0)
        if effective_available is None:
            effective_available = cgroup_available
        else:
            effective_available = min(effective_available, cgroup_available)

    gib = float(1024**3)
    return {
        "total_gib": (
            effective_total / gib if effective_total is not None else None
        ),
        "available_gib": (
            effective_available / gib
            if effective_available is not None
            else None
        ),
        "host_total_gib": host_total / gib if host_total is not None else None,
        "host_available_gib": (
            host_available / gib if host_available is not None else None
        ),
    }


def print_resource_diagnostics() -> None:
    """Print OS, CPU, RAM, and /content free-space diagnostics."""
    print("\nEnvironment diagnostics")
    print(f"  Notebook Python: {sys.executable}")
    print(f"  CPU count:       {os.cpu_count()}")

    os_release = Path("/etc/os-release")
    if os_release.exists():
        fields: dict[str, str] = {}
        for line in os_release.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", maxsplit=1)
            fields[key] = value.strip().strip('"')
        print(
            "  OS:              "
            f"{fields.get('PRETTY_NAME', fields.get('NAME', 'unknown'))}"
        )

    status = memory_status()
    total = status["total_gib"]
    available = status["available_gib"]
    if total is not None:
        print(f"  Effective RAM:   {total:.2f} GiB")
    if available is not None:
        print(f"  RAM available:   {available:.2f} GiB")

    content_path = Path("/content") if Path("/content").exists() else Path("/")
    disk = shutil.disk_usage(content_path)
    print(f"  Free disk:       {disk.free / 1024**3:.2f} GiB")

    if WARN_ON_LOW_MEMORY and available is not None:
        if available < MIN_AVAILABLE_RAM_GIB:
            print(
                "WARNING: available RAM is below "
                f"{MIN_AVAILABLE_RAM_GIB:.1f} GiB. Large FDTD meshes may be "
                "terminated by the Colab runtime."
            )


def source_build_jobs() -> int:
    """Choose a conservative build parallelism from CPU and available RAM."""
    if BUILD_JOBS > 0:
        return BUILD_JOBS

    cpu_count = max(os.cpu_count() or 1, 1)
    available = memory_status()["available_gib"]
    if available is None:
        return cpu_count

    ram_limited = max(int(available / RAM_GIB_PER_BUILD_JOB), 1)
    return max(min(cpu_count, ram_limited), 1)


def check_source_disk_space() -> None:
    """Warn if the source-build filesystem has little free space."""
    content_path = Path("/content") if Path("/content").exists() else Path("/")
    free_gib = shutil.disk_usage(content_path).free / 1024**3
    if free_gib < MIN_FREE_DISK_GIB_FOR_SOURCE_BUILD:
        print(
            "WARNING: source installation has only "
            f"{free_gib:.2f} GiB free. A larger free-space margin is preferred."
        )


# =============================================================================
# INSTALLATION PATHS
# =============================================================================


def install_from_apt() -> bool:
    """Install openEMS from APT if the solver package is genuinely available."""
    apt_update()

    required_ready = all(
        apt_candidate_exists(package_name)
        for package_name in APT_REQUIRED_PACKAGES
    )

    if not required_ready and TRY_OPENEMS_PPA:
        add_openems_ppa()
        required_ready = all(
            apt_candidate_exists(package_name)
            for package_name in APT_REQUIRED_PACKAGES
        )

    if not required_ready:
        print("APT openEMS package candidate is unavailable.")
        return False

    optional_available = tuple(
        package_name
        for package_name in APT_OPTIONAL_PACKAGES
        if apt_candidate_exists(package_name)
    )
    optional_skipped = tuple(
        package_name
        for package_name in APT_OPTIONAL_PACKAGES
        if package_name not in optional_available
    )

    if optional_skipped:
        print("Skipped unavailable optional APT packages:")
        for package_name in optional_skipped:
            print(f"  {package_name}")

    packages = APT_REQUIRED_PACKAGES + optional_available
    apt_install(packages, log_name="apt_install_openems.log")
    return True


def bootstrap_source_tools() -> None:
    """Install the small package set required to fetch the source repository."""
    apt_update(log_name="apt_update_source_bootstrap.log")
    available = tuple(
        package_name
        for package_name in SOURCE_BOOTSTRAP_PACKAGES
        if apt_candidate_exists(package_name)
    )
    missing = tuple(
        package_name
        for package_name in SOURCE_BOOTSTRAP_PACKAGES
        if package_name not in available
    )
    if missing:
        raise RuntimeError(
            "Missing source-bootstrap APT packages: " + ", ".join(missing)
        )
    apt_install(available, log_name="apt_install_source_bootstrap.log")

    if BUILD_MPI:
        mpi_available = tuple(
            package_name
            for package_name in SOURCE_MPI_PACKAGES
            if apt_candidate_exists(package_name)
        )
        mpi_missing = tuple(
            package_name
            for package_name in SOURCE_MPI_PACKAGES
            if package_name not in mpi_available
        )
        if mpi_missing:
            raise RuntimeError(
                "Missing MPI APT packages: " + ", ".join(mpi_missing)
            )
        apt_install(mpi_available, log_name="apt_install_openmpi.log")


def clone_or_update_source_tree() -> None:
    """Clone the official openEMS meta-repository or refresh an existing tree."""
    if not SOURCE_DIR.exists():
        run_command(
            [
                "git",
                "clone",
                "--recursive",
                OPENEMS_REPOSITORY,
                str(SOURCE_DIR),
            ],
            check=True,
            log_name="git_clone_openems.log",
        )
        return

    if not (SOURCE_DIR / ".git").exists():
        raise RuntimeError(
            f"{SOURCE_DIR} already exists but is not a Git working tree."
        )

    if not UPDATE_EXISTING_SOURCE_TREE:
        print(f"Reusing existing source tree without update: {SOURCE_DIR}")
        run_command(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=SOURCE_DIR,
            check=True,
            log_name="git_submodule_update.log",
        )
        return

    run_command(
        ["git", "fetch", "--tags", "--prune"],
        cwd=SOURCE_DIR,
        check=True,
        log_name="git_fetch_openems.log",
    )
    run_command(
        ["git", "pull", "--ff-only"],
        cwd=SOURCE_DIR,
        check=True,
        log_name="git_pull_openems.log",
    )
    run_command(
        ["git", "submodule", "update", "--init", "--recursive"],
        cwd=SOURCE_DIR,
        check=True,
        log_name="git_submodule_update.log",
    )


def install_source_dependencies() -> None:
    """Use the repository's own dependency installer for the active distro."""
    script = SOURCE_DIR / "scripts" / "install_deps.sh"
    if not script.exists():
        raise RuntimeError(f"Missing official dependency script: {script}")

    command = [str(script), "--auto", "--python"]
    if not BUILD_GUI:
        command.append("--disable-gui")
    if BUILD_CTB:
        command.append("--with-ctb")

    run_command(
        command,
        cwd=SOURCE_DIR,
        check=True,
        env=apt_env(),
        log_name="source_install_dependencies.log",
    )


def install_from_source() -> None:
    """Build openEMS using the official openEMS-Project installer."""
    check_source_disk_space()
    bootstrap_source_tools()
    clone_or_update_source_tree()
    install_source_dependencies()

    update_script = SOURCE_DIR / "update_openEMS.sh"
    if not update_script.exists():
        raise RuntimeError(f"Missing official build script: {update_script}")

    jobs = source_build_jobs()
    command = [
        str(update_script),
        str(INSTALL_PREFIX),
        "--python",
        "--python-venv-mode=venv",
        f"--njobs={jobs}",
    ]
    if not BUILD_GUI:
        command.append("--disable-GUI")
    if BUILD_CTB:
        command.append("--with-CTB")
    if BUILD_MPI:
        command.append("--with-MPI")

    print(f"Source build parallelism: {jobs} job(s)")
    run_command(
        command,
        cwd=SOURCE_DIR,
        check=True,
        log_name="source_build_openems.log",
    )


def perform_installation() -> str:
    """Install openEMS according to INSTALL_MODE and return the selected path."""
    mode = INSTALL_MODE.lower().strip()
    valid_modes = {"auto", "apt", "source", "none"}
    if mode not in valid_modes:
        raise ValueError(
            f"INSTALL_MODE must be one of {sorted(valid_modes)}, received {mode!r}."
        )

    if mode == "none":
        return "existing"

    if mode == "apt":
        if not install_from_apt():
            raise RuntimeError(
                "INSTALL_MODE='apt' was requested, but no openEMS APT "
                "candidate was available."
            )
        return "apt"

    if mode == "source":
        install_from_source()
        return "source"

    if install_from_apt():
        return "apt"

    print("Falling back to the official source installer.")
    install_from_source()
    return "source"


# =============================================================================
# RUNTIME ENVIRONMENT AND DISCOVERY
# =============================================================================


def unique_existing_paths(paths: list[Path]) -> list[Path]:
    """Return existing paths in original order with duplicates removed."""
    output: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        key = str(resolved)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        output.append(path)
    return output


def discover_binary(name: str) -> str | None:
    """Locate a binary in the source prefix, PATH, or common system paths."""
    candidates = [
        INSTALL_PREFIX / "bin" / name,
        Path("/usr/local/bin") / name,
        Path("/usr/bin") / name,
    ]

    which_path = shutil.which(name)
    if which_path is not None:
        candidates.insert(1, Path(which_path))

    existing = unique_existing_paths(candidates)
    return str(existing[0]) if existing else None


def discover_matlab_dir(project_name: str) -> Path | None:
    """Locate an openEMS or CSXCAD Matlab/Octave interface directory."""
    candidates = [
        INSTALL_PREFIX / "share" / project_name / "matlab",
        Path("/usr/local/share") / project_name / "matlab",
        Path("/usr/share") / project_name / "matlab",
    ]
    existing = unique_existing_paths(candidates)
    return existing[0] if existing else None


def base_runtime_env(
    *,
    include_system_python_path: bool = False,
) -> dict[str, str]:
    """Return a small runtime environment for openEMS subprocesses."""
    path_entries = [
        INSTALL_PREFIX / "venv" / "bin",
        INSTALL_PREFIX / "bin",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    ]
    library_entries = [
        INSTALL_PREFIX / "lib",
        INSTALL_PREFIX / "lib64",
        Path("/usr/local/lib"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/usr/lib"),
        Path("/lib/x86_64-linux-gnu"),
        Path("/lib"),
    ]

    env = {
        "HOME": os.environ.get("HOME", "/root"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": ":".join(str(path) for path in path_entries),
        "PYTHONNOUSERSITE": "1",
        "LD_LIBRARY_PATH": ":".join(str(path) for path in library_entries),
    }
    if include_system_python_path:
        env["PYTHONPATH"] = "/usr/lib/python3/dist-packages"
    return env


def discover_python_candidates() -> list[str]:
    """Discover actual Python 3 interpreters without hardcoded minor versions."""
    candidates: list[str] = [
        str(INSTALL_PREFIX / "venv" / "bin" / "python"),
        str(INSTALL_PREFIX / "venv" / "bin" / "python3"),
        sys.executable,
        "/usr/bin/python3",
        "/usr/local/bin/python3",
        *EXTRA_PYTHON_CANDIDATES,
    ]

    # Follow the general glob-based idea, then filter out python3-config and
    # other helper executables that merely share the python3 prefix.
    python_name = re.compile(r"^python3(?:\.\d+)?$")
    for raw_path in sorted(glob.glob("/usr/bin/python3*")):
        path = Path(raw_path)
        if python_name.fullmatch(path.name) and os.access(path, os.X_OK):
            candidates.append(str(path))

    for raw_path in sorted(glob.glob("/usr/local/bin/python3*")):
        path = Path(raw_path)
        if python_name.fullmatch(path.name) and os.access(path, os.X_OK):
            candidates.append(str(path))

    output: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = shutil.which(candidate) if "/" not in candidate else candidate
        if path is None:
            continue
        candidate_path = Path(path)
        if not candidate_path.exists() or not os.access(candidate_path, os.X_OK):
            continue
        try:
            resolved = str(candidate_path.resolve())
        except OSError:
            resolved = str(candidate_path)
        if resolved in seen:
            continue
        seen.add(resolved)
        output.append(str(candidate_path))

    return output


def candidate_uses_source_venv(python_executable: str) -> bool:
    """Return True if a Python candidate belongs to the source-build venv."""
    try:
        candidate = Path(python_executable).resolve()
        venv = (INSTALL_PREFIX / "venv").resolve()
        candidate.relative_to(venv)
    except (OSError, ValueError):
        return False
    return True


def openems_binaries_available() -> tuple[bool, str | None, str | None]:
    """Report solver and optional AppCSXCAD executable availability."""
    openems_binary = discover_binary("openEMS")
    appcsxcad_binary = discover_binary("AppCSXCAD")

    if openems_binary is None:
        print("Missing executable: openEMS")
    else:
        print(f"Found executable: openEMS -> {openems_binary}")

    if appcsxcad_binary is None:
        print("Optional executable unavailable: AppCSXCAD")
    else:
        print(f"Found executable: AppCSXCAD -> {appcsxcad_binary}")

    ok = openems_binary is not None
    if APPCSXCAD_REQUIRED:
        ok = ok and appcsxcad_binary is not None

    return ok, openems_binary, appcsxcad_binary


def print_package_locations() -> None:
    """Print installed Debian openEMS package locations for debugging."""
    if not command_exists("dpkg"):
        return

    for package_name in ("python3-openems", "openems", "octave-openems"):
        result = run_command(
            ["dpkg", "-L", package_name],
            check=False,
            env=base_runtime_env(include_system_python_path=True),
            log_name=f"dpkg_L_{package_name}.log",
        )
        if result.returncode != 0:
            continue

        interesting = [
            line
            for line in (result.stdout or "").splitlines()
            if (
                "dist-packages" in line
                or "/usr/share/openEMS" in line
                or "/usr/share/CSXCAD" in line
            )
        ]
        if interesting:
            print(f"Key files from {package_name}:")
            for line in interesting[:80]:
                print("  " + line)


# =============================================================================
# PYTHON AND OCTAVE PROBES
# =============================================================================


def python_probe_code() -> str:
    """Return the Python probe used to verify openEMS bindings."""
    return r'''
import importlib.util
import sys

print("python executable:", sys.executable)
print("python version:", sys.version.replace("\\n", " "))
print("sys.path:")
for item in sys.path:
    print("  " + str(item))

for module_name in ("CSXCAD", "openEMS"):
    spec = importlib.util.find_spec(module_name)
    print(f"spec {module_name}: {spec}")
    if spec is not None:
        print(f"origin {module_name}: {spec.origin}")
        print(f"locations {module_name}: {spec.submodule_search_locations}")

from CSXCAD import ContinuousStructure
from openEMS import openEMS

csx = ContinuousStructure()
fdtd = openEMS()

print("ContinuousStructure:", type(csx))
print("openEMS:", type(fdtd))
print("OPENEMS_PYTHON_BINDINGS_OK")
'''


def test_python_candidate(python_executable: str) -> bool:
    """Return True if one interpreter can import the openEMS bindings."""
    candidate = Path(python_executable)
    if not candidate.exists() or not os.access(candidate, os.X_OK):
        return False

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(candidate))
    safe_name = safe_name.strip("_.")[-100:]
    env = base_runtime_env(
        include_system_python_path=not candidate_uses_source_venv(
            python_executable
        )
    )
    result = run_command(
        [python_executable, "-c", python_probe_code()],
        check=False,
        env=env,
        log_name=f"probe_openems_{safe_name}.log",
    )
    return (
        result.returncode == 0
        and "OPENEMS_PYTHON_BINDINGS_OK" in (result.stdout or "")
    )


def select_openems_python() -> str | None:
    """Return the first discovered Python interpreter with working bindings."""
    candidates = discover_python_candidates()
    if not candidates:
        print("No Python 3 interpreter candidates were discovered.")
        return None

    print("Discovered Python candidates:")
    for candidate in candidates:
        print(f"  {candidate}")

    for candidate in candidates:
        print(f"Testing openEMS Python candidate: {candidate}")
        if test_python_candidate(candidate):
            print(f"Selected openEMS Python executable: {candidate}")
            return candidate

    return None


def octave_probe_code(
    openems_matlab_dir: Path,
    csxcad_matlab_dir: Path,
) -> str:
    """Return an Octave probe for discovered openEMS interface paths."""
    return f"""
addpath('{openems_matlab_dir}');
addpath('{csxcad_matlab_dir}');
disp('Octave path configured for openEMS');
which openEMS
which InitFDTD
which WriteOpenEMS
disp('OPENEMS_OCTAVE_INTERFACE_OK');
"""


def test_octave_interface(
    openems_matlab_dir: Path | None,
    csxcad_matlab_dir: Path | None,
) -> tuple[bool, str | None]:
    """Return Octave interface status and the selected Octave executable."""
    octave_binary = discover_binary("octave")
    if octave_binary is None:
        print("Octave executable is unavailable.")
        return False, None

    if openems_matlab_dir is None or csxcad_matlab_dir is None:
        print("Octave executable exists, but openEMS Matlab paths were unavailable.")
        return False, octave_binary

    SMOKE_TEST_DIR.mkdir(parents=True, exist_ok=True)
    script_path = SMOKE_TEST_DIR / "openems_octave_probe.m"
    script_path.write_text(
        octave_probe_code(openems_matlab_dir, csxcad_matlab_dir),
        encoding="utf-8",
    )

    result = run_command(
        [octave_binary, "--quiet", str(script_path)],
        check=False,
        env=base_runtime_env(),
        log_name="probe_openems_octave.log",
    )
    ok = (
        result.returncode == 0
        and "OPENEMS_OCTAVE_INTERFACE_OK" in (result.stdout or "")
    )
    return ok, octave_binary


# =============================================================================
# BRIDGE MODULE WRITER
# =============================================================================


def write_bridge_module(
    *,
    python_executable: str | None,
    openems_binary: str,
    octave_binary: str | None,
    octave_available: bool,
    openems_matlab_dir: Path | None,
    csxcad_matlab_dir: Path | None,
) -> None:
    """Write a Colab bridge with runtime-path and RAM diagnostics."""
    python_literal = repr(python_executable) if python_executable else "None"
    octave_literal = repr(octave_binary) if octave_binary else "None"
    octave_available_literal = "True" if octave_available else "False"
    openems_matlab_literal = (
        repr(str(openems_matlab_dir)) if openems_matlab_dir else "None"
    )
    csxcad_matlab_literal = (
        repr(str(csxcad_matlab_dir)) if csxcad_matlab_dir else "None"
    )

    bridge_code = f'''
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path
from typing import Mapping


OPENEMS_PYTHON = {python_literal}
OPENEMS_BINARY = {openems_binary!r}
OCTAVE_BINARY = {octave_literal}
OCTAVE_AVAILABLE = {octave_available_literal}
OPENEMS_PREFIX = Path({str(INSTALL_PREFIX)!r})
OPENEMS_MATLAB_DIR = {openems_matlab_literal}
CSXCAD_MATLAB_DIR = {csxcad_matlab_literal}
DEFAULT_WORK_DIR = Path("/content/openems_runs")
DEFAULT_MIN_AVAILABLE_RAM_GIB = {MIN_AVAILABLE_RAM_GIB!r}


def _read_meminfo_bytes() -> dict[str, int]:
    """Read selected Linux memory counters from /proc/meminfo."""
    path = Path("/proc/meminfo")
    if not path.exists():
        return {{}}

    values: dict[str, int] = {{}}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, raw_value = line.split(":", maxsplit=1)
        fields = raw_value.strip().split()
        if not fields:
            continue
        try:
            value = int(fields[0])
        except ValueError:
            continue
        multiplier = 1024 if len(fields) > 1 and fields[1] == "kB" else 1
        values[key] = value * multiplier
    return values


def _read_first_integer(paths: tuple[Path, ...]) -> int | None:
    """Return the first finite nonnegative integer from candidate files."""
    for path in paths:
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if raw == "max":
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value >= 0:
            return value
    return None


def memory_status() -> dict[str, float | None]:
    """Return effective total and available RAM in GiB."""
    meminfo = _read_meminfo_bytes()
    host_total = meminfo.get("MemTotal")
    host_available = meminfo.get("MemAvailable")

    cgroup_limit = _read_first_integer((
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ))
    cgroup_current = _read_first_integer((
        Path("/sys/fs/cgroup/memory.current"),
        Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
    ))

    effective_total = host_total
    if cgroup_limit is not None and cgroup_limit < (1 << 60):
        effective_total = (
            cgroup_limit
            if effective_total is None
            else min(effective_total, cgroup_limit)
        )

    effective_available = host_available
    if (
        cgroup_limit is not None
        and cgroup_current is not None
        and cgroup_limit < (1 << 60)
    ):
        cgroup_available = max(cgroup_limit - cgroup_current, 0)
        effective_available = (
            cgroup_available
            if effective_available is None
            else min(effective_available, cgroup_available)
        )

    gib = float(1024**3)
    return {{
        "total_gib": (
            effective_total / gib if effective_total is not None else None
        ),
        "available_gib": (
            effective_available / gib
            if effective_available is not None
            else None
        ),
    }}


def warn_if_low_memory(
    min_available_gib: float = DEFAULT_MIN_AVAILABLE_RAM_GIB,
) -> dict[str, float | None]:
    """Print a warning if available RAM falls below a caller-set threshold."""
    status = memory_status()
    available = status["available_gib"]
    total = status["total_gib"]

    if total is not None and available is not None:
        print(
            f"openEMS RAM: {{available:.2f}} GiB available / "
            f"{{total:.2f}} GiB effective total"
        )
        if available < min_available_gib:
            print(
                "WARNING: available RAM is below "
                f"{{min_available_gib:.2f}} GiB. A large FDTD mesh may be "
                "terminated by the runtime."
            )
    return status


def _base_env(
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a small environment containing the discovered openEMS prefix."""
    path_entries = [
        OPENEMS_PREFIX / "venv" / "bin",
        OPENEMS_PREFIX / "bin",
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path("/bin"),
        Path("/usr/sbin"),
        Path("/sbin"),
    ]
    library_entries = [
        OPENEMS_PREFIX / "lib",
        OPENEMS_PREFIX / "lib64",
        Path("/usr/local/lib"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/usr/lib"),
        Path("/lib/x86_64-linux-gnu"),
        Path("/lib"),
    ]

    env = {{
        "HOME": os.environ.get("HOME", "/root"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PATH": ":".join(str(path) for path in path_entries),
        "PYTHONNOUSERSITE": "1",
        "LD_LIBRARY_PATH": ":".join(str(path) for path in library_entries),
    }}

    if OPENEMS_PYTHON is not None:
        python_path = Path(OPENEMS_PYTHON)
        try:
            python_path.resolve().relative_to((OPENEMS_PREFIX / "venv").resolve())
            uses_source_venv = True
        except (OSError, ValueError):
            uses_source_venv = False
        if not uses_source_venv:
            env["PYTHONPATH"] = "/usr/lib/python3/dist-packages"

    if extra_env:
        env.update({{str(key): str(value) for key, value in extra_env.items()}})
    return env


def run_openems_binary(
    xml_path: str | Path,
    *,
    work_dir: str | Path | None = None,
    extra_args: tuple[str, ...] = (),
    check_memory: bool = True,
    min_available_gib: float = DEFAULT_MIN_AVAILABLE_RAM_GIB,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the native openEMS solver against an XML model."""
    if check_memory:
        warn_if_low_memory(min_available_gib)

    path = Path(xml_path)
    cwd = Path(work_dir) if work_dir is not None else path.parent
    result = subprocess.run(
        [OPENEMS_BINARY, *extra_args, str(path)],
        cwd=str(cwd),
        env=_base_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(result.stdout)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
        )
    return result


def run_openems_python_script(
    script_path: str | Path,
    *,
    work_dir: str | Path | None = None,
    extra_env: Mapping[str, str] | None = None,
    check_memory: bool = True,
    min_available_gib: float = DEFAULT_MIN_AVAILABLE_RAM_GIB,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a Python openEMS script in the compatible interpreter."""
    if OPENEMS_PYTHON is None:
        raise RuntimeError(
            "No compatible openEMS Python binding was found. "
            "Use run_openems_octave_code() or run_openems_binary()."
        )
    if check_memory:
        warn_if_low_memory(min_available_gib)

    path = Path(script_path)
    cwd = Path(work_dir) if work_dir is not None else path.parent
    cwd.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [OPENEMS_PYTHON, str(path)],
        cwd=str(cwd),
        env=_base_env(extra_env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(result.stdout)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
        )
    return result


def run_openems_python_code(
    code: str,
    *,
    work_dir: str | Path = DEFAULT_WORK_DIR,
    filename: str = "openems_case.py",
    extra_env: Mapping[str, str] | None = None,
    check_memory: bool = True,
    min_available_gib: float = DEFAULT_MIN_AVAILABLE_RAM_GIB,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Write and execute Python openEMS code in an isolated subprocess."""
    cwd = Path(work_dir)
    cwd.mkdir(parents=True, exist_ok=True)
    script_path = cwd / filename
    script_path.write_text(
        textwrap.dedent(code).strip() + "\\n",
        encoding="utf-8",
    )
    return run_openems_python_script(
        script_path,
        work_dir=cwd,
        extra_env=extra_env,
        check_memory=check_memory,
        min_available_gib=min_available_gib,
        check=check,
    )


def run_openems_octave_code(
    code: str,
    *,
    work_dir: str | Path = DEFAULT_WORK_DIR,
    filename: str = "openems_case.m",
    check_memory: bool = True,
    min_available_gib: float = DEFAULT_MIN_AVAILABLE_RAM_GIB,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Write and execute Octave openEMS code with discovered interface paths."""
    if not OCTAVE_AVAILABLE or OCTAVE_BINARY is None:
        raise RuntimeError("The Octave openEMS interface was unavailable.")
    if OPENEMS_MATLAB_DIR is None or CSXCAD_MATLAB_DIR is None:
        raise RuntimeError("The openEMS Matlab/Octave interface paths are missing.")
    if check_memory:
        warn_if_low_memory(min_available_gib)

    cwd = Path(work_dir)
    cwd.mkdir(parents=True, exist_ok=True)
    script_path = cwd / filename
    prefix = f"""
addpath('{{OPENEMS_MATLAB_DIR}}');
addpath('{{CSXCAD_MATLAB_DIR}}');
"""
    script_path.write_text(
        textwrap.dedent(prefix + "\\n" + code).strip() + "\\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [OCTAVE_BINARY, "--quiet", str(script_path)],
        cwd=str(cwd),
        env=_base_env(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    print(result.stdout)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
        )
    return result
'''

    BRIDGE_MODULE_PATH.write_text(
        textwrap.dedent(bridge_code).strip() + "\n",
        encoding="utf-8",
    )
    print(f"Wrote bridge module: {BRIDGE_MODULE_PATH}")


# =============================================================================
# MAIN
# =============================================================================


def main() -> None:
    """Install openEMS, probe interfaces, and write the Colab bridge module."""
    print("openEMS Colab installer")
    print(f"Log directory: {LOG_DIR}")
    print_resource_diagnostics()

    installation_path = perform_installation()

    binaries_ok, openems_binary, appcsxcad_binary = (
        openems_binaries_available()
    )
    if not binaries_ok or openems_binary is None:
        raise RuntimeError(
            "The openEMS command-line solver was unavailable after installation. "
            f"See logs in {LOG_DIR}."
        )

    print_package_locations()

    openems_python = select_openems_python()
    openems_matlab_dir = discover_matlab_dir("openEMS")
    csxcad_matlab_dir = discover_matlab_dir("CSXCAD")
    octave_ok, octave_binary = test_octave_interface(
        openems_matlab_dir,
        csxcad_matlab_dir,
    )

    if PYTHON_BRIDGE_REQUIRED and openems_python is None:
        raise RuntimeError(
            "A compatible Python binding was unavailable. "
            f"See logs in {LOG_DIR}."
        )

    if OCTAVE_BRIDGE_REQUIRED and not octave_ok:
        raise RuntimeError(
            "The Octave openEMS interface was unavailable. "
            f"See logs in {LOG_DIR}."
        )

    write_bridge_module(
        python_executable=openems_python,
        openems_binary=openems_binary,
        octave_binary=octave_binary,
        octave_available=octave_ok,
        openems_matlab_dir=openems_matlab_dir,
        csxcad_matlab_dir=csxcad_matlab_dir,
    )

    print("\nInstallation summary")
    print(f"  Installation path:      {installation_path}")
    print(f"  openEMS solver:         {openems_binary}")
    print(f"  AppCSXCAD:              {appcsxcad_binary}")
    print(f"  Python binding bridge:  {openems_python}")
    print(f"  Octave executable:      {octave_binary}")
    print(f"  Octave interface:       {octave_ok}")
    print(f"  openEMS Matlab path:    {openems_matlab_dir}")
    print(f"  CSXCAD Matlab path:     {csxcad_matlab_dir}")

    print("\nUse this in later Colab cells:")
    print(
        "import sys\n"
        "sys.path.insert(0, '/content')\n"
        "from openems_colab_bridge import (\n"
        "    memory_status,\n"
        "    run_openems_binary,\n"
        "    run_openems_octave_code,\n"
        "    run_openems_python_code,\n"
        "    warn_if_low_memory,\n"
        ")\n"
    )

    if openems_python is not None:
        print(
            "run_openems_python_code(\"\"\"\n"
            "from CSXCAD import ContinuousStructure\n"
            "from openEMS import openEMS\n"
            "print('openEMS Python bridge works')\n"
            "\"\"\")\n"
        )
    elif octave_ok:
        print(
            "run_openems_octave_code(\"\"\"\n"
            "disp('openEMS Octave bridge works');\n"
            "which openEMS\n"
            "\"\"\")\n"
        )
    else:
        print(
            "# Python and Octave interfaces were unavailable. The native solver "
            "can still run XML files:\n"
            "# run_openems_binary('/content/path/to/simulation.xml')\n"
        )


if __name__ == "__main__":
    main()
