import ctypes
import glob
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText


APP_NAME = "System Cleanup Utility"
APP_VERSION = "0.2.1"
DEFAULT_ASSET_NAME = "SystemCleanupUtility.exe"
UPDATE_CONFIG_FILE = "release_config.json"
WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class CleanupTarget:
    key: str
    label: str
    location: str
    path_specs: tuple[str, ...]
    description: str
    category: str = "standard"
    default_selected: bool = False


@dataclass
class CleanupStats:
    label: str
    path: str
    deleted_files: int = 0
    deleted_dirs: int = 0
    skipped_items: int = 0
    failed_items: int = 0
    bytes_freed: int = 0
    missing_root: bool = False

    def merge_into(self, other: "CleanupStats") -> None:
        other.deleted_files += self.deleted_files
        other.deleted_dirs += self.deleted_dirs
        other.skipped_items += self.skipped_items
        other.failed_items += self.failed_items
        other.bytes_freed += self.bytes_freed


@dataclass(frozen=True)
class UpdateConfig:
    github_owner: str = ""
    github_repo: str = ""
    asset_name: str = DEFAULT_ASSET_NAME

    @property
    def configured(self) -> bool:
        owner = self.github_owner.strip().lower()
        repo = self.github_repo.strip().lower()
        if not owner or not repo:
            return False
        if owner.startswith("your-") or repo.startswith("your-"):
            return False
        return True

    @property
    def latest_release_api(self) -> str:
        return f"https://api.github.com/repos/{self.github_owner}/{self.github_repo}/releases/latest"


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    asset_name: str
    asset_url: str
    page_url: str


def format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def clean_version_text(value: str) -> str:
    return value.strip().lstrip("vV")


def version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", clean_version_text(value))
    if not numbers:
        return (0,)
    return tuple(int(number) for number in numbers[:6])


def is_newer_version(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    if getattr(sys, "frozen", False):
        executable = sys.executable
        params = subprocess.list2cmdline(sys.argv[1:])
    else:
        executable = sys.executable
        params = subprocess.list2cmdline(sys.argv)

    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        executable,
        params,
        None,
        1,
    )
    return result > 32


def normalized(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def get_runtime_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_embedded_base_dir() -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return get_runtime_base_dir()


def get_update_config() -> UpdateConfig:
    candidate_paths = [
        get_runtime_base_dir() / UPDATE_CONFIG_FILE,
        get_embedded_base_dir() / UPDATE_CONFIG_FILE,
    ]

    seen: set[str] = set()
    for candidate in candidate_paths:
        candidate_key = normalized(candidate)
        if candidate_key in seen or not candidate.exists():
            continue
        seen.add(candidate_key)

        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        return UpdateConfig(
            github_owner=str(payload.get("github_owner", "")).strip(),
            github_repo=str(payload.get("github_repo", "")).strip(),
            asset_name=str(payload.get("asset_name", DEFAULT_ASSET_NAME)).strip() or DEFAULT_ASSET_NAME,
        )

    return UpdateConfig()


def get_protected_paths() -> set[str]:
    protected = {
        normalized(sys.executable),
        normalized(__file__),
    }

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        protected.add(normalized(meipass))

    return protected


def candidate_contains_protected(candidate: str, protected_paths: set[str]) -> bool:
    candidate_normalized = normalized(candidate)
    candidate_prefix = f"{candidate_normalized}{os.sep}"
    for protected in protected_paths:
        if protected == candidate_normalized or protected.startswith(candidate_prefix):
            return True
    return False


def has_glob_magic(path_spec: str) -> bool:
    return any(char in path_spec for char in "*?[]")


def make_writable(path: str) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


def get_targets() -> list[CleanupTarget]:
    return [
        CleanupTarget(
            key="user_temp",
            label="User Temp",
            location="%TEMP%",
            path_specs=("%TEMP%",),
            description="Clears the current user's temporary files.",
            default_selected=True,
        ),
        CleanupTarget(
            key="local_low_temp",
            label="LocalLow Temp",
            location="%USERPROFILE%\\AppData\\LocalLow\\Temp",
            path_specs=("%USERPROFILE%\\AppData\\LocalLow\\Temp",),
            description="Clears temp files used by some games and older apps.",
            default_selected=True,
        ),
        CleanupTarget(
            key="windows_temp",
            label="Windows Temp",
            location="%WINDIR%\\Temp",
            path_specs=("%WINDIR%\\Temp",),
            description="Clears the shared Windows temp folder.",
            default_selected=True,
        ),
        CleanupTarget(
            key="prefetch",
            label="Windows Prefetch",
            location="%WINDIR%\\Prefetch",
            path_specs=("%WINDIR%\\Prefetch",),
            description="Clears prefetch cache files. Windows rebuilds them as needed.",
            default_selected=True,
        ),
        CleanupTarget(
            key="directx_shader_cache",
            label="DirectX Shader Cache",
            location="%LOCALAPPDATA%\\D3DSCache",
            path_specs=("%LOCALAPPDATA%\\D3DSCache",),
            description="Clears DirectX shader cache. Games and 3D apps rebuild it on demand.",
            category="advanced",
        ),
        CleanupTarget(
            key="thumbnail_icon_cache",
            label="Thumbnail and Icon Cache",
            location="%LOCALAPPDATA%\\Microsoft\\Windows\\Explorer",
            path_specs=(
                "%LOCALAPPDATA%\\Microsoft\\Windows\\Explorer\\thumbcache_*.db",
                "%LOCALAPPDATA%\\Microsoft\\Windows\\Explorer\\iconcache_*.db",
            ),
            description="Clears Explorer thumbnail and icon cache files. Windows rebuilds them gradually.",
            category="advanced",
        ),
        CleanupTarget(
            key="windows_error_reports",
            label="Windows Error Reports",
            location="%LOCALAPPDATA%\\CrashDumps and %ProgramData%\\Microsoft\\Windows\\WER",
            path_specs=(
                "%LOCALAPPDATA%\\CrashDumps",
                "%ProgramData%\\Microsoft\\Windows\\WER\\ReportArchive",
                "%ProgramData%\\Microsoft\\Windows\\WER\\ReportQueue",
            ),
            description="Removes crash dumps and queued error reports that can build up over time.",
            category="advanced",
        ),
        CleanupTarget(
            key="delivery_optimization_cache",
            label="Delivery Optimization Cache",
            location="%WINDIR%\\ServiceProfiles\\NetworkService\\AppData\\Local\\Microsoft\\Windows\\DeliveryOptimization\\Cache",
            path_specs=(
                "%WINDIR%\\ServiceProfiles\\NetworkService\\AppData\\Local\\Microsoft\\Windows\\DeliveryOptimization\\Cache",
            ),
            description="Clears cached Windows delivery optimization files used for update distribution.",
            category="advanced",
        ),
        CleanupTarget(
            key="edge_cache",
            label="Microsoft Edge Cache",
            location="%LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\*",
            path_specs=(
                "%LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\*\\Cache\\Cache_Data",
                "%LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\*\\Code Cache",
                "%LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\*\\GPUCache",
            ),
            description="Clears Edge browser cache folders across detected profiles. Best results if Edge is closed.",
            category="advanced",
        ),
        CleanupTarget(
            key="chrome_cache",
            label="Google Chrome Cache",
            location="%LOCALAPPDATA%\\Google\\Chrome\\User Data\\*",
            path_specs=(
                "%LOCALAPPDATA%\\Google\\Chrome\\User Data\\*\\Cache\\Cache_Data",
                "%LOCALAPPDATA%\\Google\\Chrome\\User Data\\*\\Code Cache",
                "%LOCALAPPDATA%\\Google\\Chrome\\User Data\\*\\GPUCache",
            ),
            description="Clears Chrome cache folders across detected profiles. Best results if Chrome is closed.",
            category="advanced",
        ),
        CleanupTarget(
            key="brave_cache",
            label="Brave Cache",
            location="%LOCALAPPDATA%\\BraveSoftware\\Brave-Browser\\User Data\\*",
            path_specs=(
                "%LOCALAPPDATA%\\BraveSoftware\\Brave-Browser\\User Data\\*\\Cache\\Cache_Data",
                "%LOCALAPPDATA%\\BraveSoftware\\Brave-Browser\\User Data\\*\\Code Cache",
                "%LOCALAPPDATA%\\BraveSoftware\\Brave-Browser\\User Data\\*\\GPUCache",
            ),
            description="Clears Brave cache folders across detected profiles. Best results if Brave is closed.",
            category="advanced",
        ),
        CleanupTarget(
            key="firefox_cache",
            label="Mozilla Firefox Cache",
            location="%LOCALAPPDATA%\\Mozilla\\Firefox\\Profiles\\*",
            path_specs=(
                "%LOCALAPPDATA%\\Mozilla\\Firefox\\Profiles\\*\\cache2",
                "%LOCALAPPDATA%\\Mozilla\\Firefox\\Profiles\\*\\startupCache",
            ),
            description="Clears Firefox cache folders across detected profiles. Best results if Firefox is closed.",
            category="advanced",
        ),
        CleanupTarget(
            key="store_app_temp_cache",
            label="Store App Temp Cache",
            location="%LOCALAPPDATA%\\Packages\\*",
            path_specs=(
                "%LOCALAPPDATA%\\Packages\\*\\AC\\INetCache",
                "%LOCALAPPDATA%\\Packages\\*\\AC\\Temp",
                "%LOCALAPPDATA%\\Packages\\*\\TempState",
            ),
            description="Clears temp and web cache folders for Microsoft Store apps without touching app data.",
            category="advanced",
        ),
        CleanupTarget(
            key="windows_update_downloads",
            label="Windows Update Download Cache",
            location="%WINDIR%\\SoftwareDistribution\\Download",
            path_specs=("%WINDIR%\\SoftwareDistribution\\Download",),
            description="Clears cached Windows Update packages. Future updates may need to download them again.",
            category="advanced",
        ),
    ]


def resolve_target_roots(target: CleanupTarget) -> list[Path]:
    resolved: list[Path] = []
    exact_candidates: list[Path] = []
    seen: set[str] = set()

    for path_spec in target.path_specs:
        expanded = os.path.expandvars(path_spec)
        matches = [Path(match) for match in glob.glob(expanded)]
        if matches:
            for match in matches:
                match_key = normalized(match)
                if match_key not in seen:
                    seen.add(match_key)
                    resolved.append(match)
            continue

        if not has_glob_magic(expanded):
            candidate = Path(expanded)
            candidate_key = normalized(candidate)
            if candidate_key not in seen:
                seen.add(candidate_key)
                exact_candidates.append(candidate)

    if resolved:
        return resolved
    return exact_candidates


def delete_entry(path: str, stats: CleanupStats, protected_paths: set[str], log) -> None:
    if candidate_contains_protected(path, protected_paths):
        stats.skipped_items += 1
        log(f"Skipped protected path: {path}")
        return

    try:
        entry_stat = os.lstat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        stats.failed_items += 1
        log(f"Could not inspect {path}: {exc}")
        return

    if getattr(entry_stat, "st_file_attributes", 0) & WINDOWS_REPARSE_POINT:
        stats.skipped_items += 1
        log(f"Skipped reparse point: {path}")
        return

    if stat.S_ISDIR(entry_stat.st_mode):
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    delete_entry(entry.path, stats, protected_paths, log)
        except OSError as exc:
            stats.failed_items += 1
            log(f"Could not read folder {path}: {exc}")
            return

        try:
            make_writable(path)
            os.rmdir(path)
            stats.deleted_dirs += 1
        except FileNotFoundError:
            return
        except OSError as exc:
            stats.failed_items += 1
            log(f"Could not remove folder {path}: {exc}")
        return

    try:
        file_size = entry_stat.st_size
        make_writable(path)
        os.unlink(path)
        stats.deleted_files += 1
        stats.bytes_freed += file_size
    except FileNotFoundError:
        return
    except OSError as exc:
        stats.failed_items += 1
        log(f"Could not remove file {path}: {exc}")


def clean_root(root: Path, stats: CleanupStats, protected_paths: set[str], log) -> None:
    root_str = str(root)

    if candidate_contains_protected(root_str, protected_paths):
        stats.skipped_items += 1
        log(f"Skipped protected path: {root_str}")
        return

    try:
        entry_stat = os.lstat(root_str)
    except FileNotFoundError:
        return
    except OSError as exc:
        stats.failed_items += 1
        log(f"Could not inspect {root_str}: {exc}")
        return

    if getattr(entry_stat, "st_file_attributes", 0) & WINDOWS_REPARSE_POINT:
        stats.skipped_items += 1
        log(f"Skipped reparse point: {root_str}")
        return

    if stat.S_ISDIR(entry_stat.st_mode):
        try:
            with os.scandir(root_str) as entries:
                for entry in entries:
                    delete_entry(entry.path, stats, protected_paths, log)
        except OSError as exc:
            stats.failed_items += 1
            log(f"Could not access {root_str}: {exc}")
        return

    delete_entry(root_str, stats, protected_paths, log)


def clean_target(target: CleanupTarget, log) -> CleanupStats:
    stats = CleanupStats(label=target.label, path=target.location)
    protected_paths = get_protected_paths()
    roots = resolve_target_roots(target)

    if not roots:
        stats.missing_root = True
        log(f"No matching paths found for {target.location}")
        return stats

    existing_roots = [root for root in roots if root.exists()]
    if not existing_roots:
        stats.missing_root = True
        log(f"No matching paths found for {target.location}")
        return stats

    for root in existing_roots:
        log(f"Resolved path: {root}")
        clean_root(root, stats, protected_paths, log)

    return stats


def fetch_latest_release(config: UpdateConfig) -> ReleaseInfo:
    request = urllib.request.Request(
        config.latest_release_api,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub returned HTTP {exc.code} while checking for updates.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach GitHub to check for updates: {exc.reason}") from exc

    tag_name = str(payload.get("tag_name", "")).strip()
    version = clean_version_text(tag_name)
    if not version:
        raise RuntimeError("The latest GitHub release is missing a tag name.")

    assets = payload.get("assets", [])
    asset = next(
        (item for item in assets if str(item.get("name", "")).strip().lower() == config.asset_name.lower()),
        None,
    )
    if asset is None:
        asset = next(
            (item for item in assets if str(item.get("name", "")).strip().lower().endswith(".exe")),
            None,
        )

    if asset is None:
        raise RuntimeError("The latest GitHub release does not include a Windows .exe asset.")

    asset_url = str(asset.get("browser_download_url", "")).strip()
    asset_name = str(asset.get("name", config.asset_name)).strip() or config.asset_name
    if not asset_url:
        raise RuntimeError("The latest GitHub release is missing a downloadable asset URL.")

    return ReleaseInfo(
        version=version,
        asset_name=asset_name,
        asset_url=asset_url,
        page_url=str(payload.get("html_url", "")).strip(),
    )


def download_release_asset(release: ReleaseInfo, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        release.asset_url,
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as file_handle:
            while True:
                chunk = response.read(1024 * 64)
                if not chunk:
                    break
                file_handle.write(chunk)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub returned HTTP {exc.code} while downloading the update.") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not download the update: {exc.reason}") from exc


def start_update_installer(current_executable: Path, downloaded_executable: Path) -> None:
    temp_dir = downloaded_executable.parent
    script_path = temp_dir / "apply-update.ps1"
    script_contents = f"""param(
    [int]$ProcessId,
    [string]$CurrentExe,
    [string]$DownloadedExe
)

for ($attempt = 0; $attempt -lt 60; $attempt++) {{
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {{
        break
    }}
    Start-Sleep -Milliseconds 500
}}

for ($attempt = 0; $attempt -lt 20; $attempt++) {{
    try {{
        Move-Item -LiteralPath $DownloadedExe -Destination $CurrentExe -Force
        Start-Process -FilePath $CurrentExe
        exit 0
    }}
    catch {{
        Start-Sleep -Milliseconds 500
    }}
}}

exit 1
"""
    script_path.write_text(script_contents, encoding="utf-8")

    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
            "-ProcessId",
            str(os.getpid()),
            "-CurrentExe",
            str(current_executable),
            "-DownloadedExe",
            str(downloaded_executable),
        ],
        creationflags=CREATE_NO_WINDOW,
    )


class CleanupApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("900x760")
        self.root.minsize(780, 620)
        self.style = ttk.Style(root)
        self.default_ttk_theme = "vista" if "vista" in self.style.theme_names() else self.style.theme_use()

        self.targets = get_targets()
        self.target_vars: dict[str, tk.BooleanVar] = {}
        self.target_boxes: list[ttk.Checkbutton] = []
        self.preset_buttons: list[ttk.Button] = []
        self.scroll_canvases: list[tk.Canvas] = []
        self.release_config = get_update_config()
        self.latest_release: ReleaseInfo | None = None

        self.queue: Queue[tuple[str, object]] = Queue()
        self.worker_thread: threading.Thread | None = None
        self.action_running = False
        self.update_check_running = False
        self.dark_mode = False

        self.status_var = tk.StringVar(value="Ready.")
        self.summary_var = tk.StringVar(
            value="Standard temp targets are preselected. Advanced caches are optional and may be rebuilt by Windows or apps."
        )
        self.version_var = tk.StringVar(value=f"Version {APP_VERSION}")
        self.update_var = tk.StringVar(value=self.get_initial_update_text())

        self.build_ui()
        self.apply_theme()
        self.refresh_controls()

        if getattr(sys, "frozen", False) and self.release_config.configured:
            self.root.after(1200, lambda: self.start_update_check(silent=True))

    def get_initial_update_text(self) -> str:
        if self.release_config.configured:
            return "Auto-update is enabled through GitHub Releases."
        return "Set release_config.json before publishing to enable auto-update."

    def build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=16, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        title_row = ttk.Frame(header, style="Surface.TFrame")
        title_row.grid(row=0, column=0, sticky="ew")
        title_row.columnconfigure(0, weight=1)

        ttk.Label(
            title_row,
            text=APP_NAME,
            font=("Segoe UI", 18, "bold"),
            style="Title.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.theme_button = ttk.Button(title_row, text="Dark Mode", command=self.toggle_theme)
        self.theme_button.grid(row=0, column=1, sticky="e", padx=(0, 12))
        ttk.Label(
            title_row,
            textvariable=self.version_var,
            style="Version.TLabel",
        ).grid(row=0, column=2, sticky="e")

        ttk.Label(
            header,
            text="Use the standard tab for generic temp cleanup. Advanced caches are optional and may be rebuilt after cleanup.",
            style="Body.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        options = ttk.LabelFrame(self.root, text="Cleanup Targets", padding=12)
        options.grid(row=1, column=0, sticky="nsew", padx=16)
        options.columnconfigure(0, weight=1)
        options.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(options)
        notebook.grid(row=0, column=0, sticky="nsew")

        standard_tab = self.create_scrollable_tab(notebook, "Standard")
        advanced_tab = self.create_scrollable_tab(notebook, "Advanced Cache")

        standard_targets = [target for target in self.targets if target.category == "standard"]
        advanced_targets = [target for target in self.targets if target.category == "advanced"]

        self.build_target_rows(standard_tab, standard_targets)

        ttk.Label(
            advanced_tab,
            text=(
                "Advanced targets can reclaim more space, but some apps may need to rebuild cache data on next launch. "
                "Close browsers and game launchers first for better results."
            ),
            wraplength=760,
            style="Warning.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.build_target_rows(advanced_tab, advanced_targets, start_row=1)

        actions = ttk.Frame(self.root, padding=(16, 12), style="Surface.TFrame")
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(0, weight=1)

        button_bar = ttk.Frame(actions, style="Surface.TFrame")
        button_bar.grid(row=0, column=0, sticky="ew")
        button_bar.columnconfigure(3, weight=1)

        self.clean_button = ttk.Button(button_bar, text="Clean Selected", command=self.start_cleanup)
        self.clean_button.grid(row=0, column=0, sticky="w")

        self.update_button = ttk.Button(button_bar, text="Check for Updates", command=self.check_for_updates)
        self.update_button.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.progress = ttk.Progressbar(button_bar, mode="indeterminate", length=180)
        self.progress.grid(row=0, column=2, sticky="w", padx=(14, 0))

        preset_bar = ttk.Frame(actions, style="Surface.TFrame")
        preset_bar.grid(row=1, column=0, sticky="w", pady=(10, 6))

        standard_button = ttk.Button(preset_bar, text="Standard Only", command=lambda: self.apply_preset("standard"))
        standard_button.grid(row=0, column=0, sticky="w")
        self.preset_buttons.append(standard_button)

        all_button = ttk.Button(preset_bar, text="Select All", command=lambda: self.apply_preset("all"))
        all_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.preset_buttons.append(all_button)

        none_button = ttk.Button(preset_bar, text="Clear All", command=lambda: self.apply_preset("none"))
        none_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.preset_buttons.append(none_button)

        ttk.Label(actions, textvariable=self.summary_var, style="Body.TLabel").grid(row=2, column=0, sticky="w", pady=(4, 4))
        ttk.Label(actions, textvariable=self.update_var, style="Warning.TLabel").grid(
            row=3,
            column=0,
            sticky="w",
            pady=(0, 8),
        )

        self.log_box = ScrolledText(actions, wrap="word", font=("Consolas", 10), height=8)
        self.log_box.grid(row=4, column=0, sticky="ew")
        self.log_box.configure(state="disabled")

        status_bar = ttk.Frame(self.root, padding=(16, 0, 16, 16), style="Surface.TFrame")
        status_bar.grid(row=3, column=0, sticky="ew")
        status_bar.columnconfigure(0, weight=1)

        ttk.Label(status_bar, textvariable=self.status_var, style="Body.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            status_bar,
            text="Run as administrator for best results.",
            style="Warning.TLabel",
        ).grid(row=0, column=1, sticky="e")

    def create_scrollable_tab(self, notebook: ttk.Notebook, title: str) -> ttk.Frame:
        container = ttk.Frame(notebook, style="Panel.TFrame")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas, padding=14, style="Panel.TFrame")

        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        def update_scrollregion(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def resize_inner(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        def on_mousewheel(event) -> None:
            step = -1 if event.delta > 0 else 1
            canvas.yview_scroll(step, "units")

        inner.bind("<Configure>", update_scrollregion)
        canvas.bind("<Configure>", resize_inner)
        inner.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_mousewheel))
        inner.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))

        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        notebook.add(container, text=title)

        self.scroll_canvases.append(canvas)
        return inner

    def build_target_rows(self, parent: ttk.Frame, targets: list[CleanupTarget], start_row: int = 0) -> None:
        for index, target in enumerate(targets, start=start_row):
            variable = tk.BooleanVar(value=target.default_selected)
            self.target_vars[target.key] = variable

            row_frame = ttk.Frame(parent, style="Panel.TFrame")
            row_frame.grid(row=index, column=0, sticky="ew", pady=(0, 12))
            row_frame.columnconfigure(0, weight=1)

            box = ttk.Checkbutton(
                row_frame,
                text=target.label,
                variable=variable,
            )
            box.grid(row=0, column=0, sticky="w")
            self.target_boxes.append(box)

            ttk.Label(
                row_frame,
                text=target.location,
                wraplength=760,
                style="Path.TLabel",
            ).grid(row=1, column=0, sticky="w", padx=(24, 0))
            ttk.Label(
                row_frame,
                text=target.description,
                wraplength=760,
                style="Muted.TLabel",
            ).grid(row=2, column=0, sticky="w", padx=(24, 0), pady=(2, 0))

    def get_palette(self) -> dict[str, str]:
        if self.dark_mode:
            return {
                "bg": "#11161d",
                "surface": "#18212b",
                "panel": "#1d2833",
                "input": "#0f151c",
                "fg": "#edf2f7",
                "muted": "#a5b2be",
                "accent": "#7cb4ff",
                "warning": "#f0bc63",
                "border": "#33404d",
                "tab_selected": "#24303c",
                "button_bg": "#24303c",
                "button_active": "#314153",
                "button_fg": "#edf2f7",
            }
        return {
            "bg": "#f5f7fa",
            "surface": "#f5f7fa",
            "panel": "#ffffff",
            "input": "#ffffff",
            "fg": "#121820",
            "muted": "#5b6774",
            "accent": "#2f5f8f",
            "warning": "#8a5a00",
            "border": "#cfd7e0",
            "tab_selected": "#ffffff",
            "button_bg": "#f4f7fb",
            "button_active": "#e7edf6",
            "button_fg": "#121820",
        }

    def apply_theme(self) -> None:
        palette = self.get_palette()

        if self.dark_mode:
            if self.style.theme_use() != "clam":
                self.style.theme_use("clam")
        else:
            if self.style.theme_use() != self.default_ttk_theme:
                self.style.theme_use(self.default_ttk_theme)

        self.root.configure(background=palette["bg"])

        self.style.configure(".", background=palette["surface"], foreground=palette["fg"])
        self.style.configure("TFrame", background=palette["surface"])
        self.style.configure("Surface.TFrame", background=palette["surface"])
        self.style.configure("Panel.TFrame", background=palette["panel"])
        self.style.configure("TLabel", background=palette["surface"], foreground=palette["fg"])
        self.style.configure("Title.TLabel", background=palette["surface"], foreground=palette["fg"])
        self.style.configure("Body.TLabel", background=palette["surface"], foreground=palette["fg"])
        self.style.configure("Muted.TLabel", background=palette["panel"], foreground=palette["muted"])
        self.style.configure("Path.TLabel", background=palette["panel"], foreground=palette["accent"])
        self.style.configure("Warning.TLabel", background=palette["surface"], foreground=palette["warning"])
        self.style.configure("Version.TLabel", background=palette["surface"], foreground=palette["accent"])
        self.style.configure("TLabelframe", background=palette["surface"], bordercolor=palette["border"])
        self.style.configure("TLabelframe.Label", background=palette["surface"], foreground=palette["fg"])
        self.style.configure("TNotebook", background=palette["panel"], borderwidth=0)
        self.style.configure("TNotebook.Tab", background=palette["panel"], foreground=palette["fg"], padding=(10, 4))
        self.style.map(
            "TNotebook.Tab",
            background=[("selected", palette["tab_selected"])],
            foreground=[("selected", palette["fg"])],
        )
        self.style.configure("TCheckbutton", background=palette["panel"], foreground=palette["fg"])
        self.style.map(
            "TCheckbutton",
            background=[("active", palette["panel"])],
            foreground=[("disabled", palette["muted"])],
        )
        self.style.configure("TButton", background=palette["button_bg"], foreground=palette["button_fg"])
        self.style.map(
            "TButton",
            background=[("active", palette["button_active"])],
            foreground=[("disabled", palette["muted"])],
        )
        self.style.configure("Horizontal.TProgressbar", background=palette["accent"], troughcolor=palette["input"])
        self.style.configure("Vertical.TScrollbar", background=palette["panel"], troughcolor=palette["surface"])

        for canvas in self.scroll_canvases:
            canvas.configure(background=palette["panel"])

        self.log_box.configure(
            background=palette["input"],
            foreground=palette["fg"],
            insertbackground=palette["fg"],
            selectbackground=palette["accent"],
            selectforeground=palette["bg"],
            relief="flat",
            borderwidth=1,
        )
        self.theme_button.configure(text="Light Mode" if self.dark_mode else "Dark Mode")

    def toggle_theme(self) -> None:
        self.dark_mode = not self.dark_mode
        self.apply_theme()

    def append_log(self, message: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def apply_preset(self, mode: str) -> None:
        for target in self.targets:
            if mode == "standard":
                selected = target.category == "standard"
            elif mode == "all":
                selected = True
            else:
                selected = False
            self.target_vars[target.key].set(selected)

    def refresh_controls(self) -> None:
        target_state = "disabled" if self.action_running else "normal"
        button_state = "disabled" if self.action_running else "normal"
        update_state = "disabled"

        if not self.action_running and not self.update_check_running and self.release_config.configured:
            update_state = "normal"

        self.clean_button.configure(state=button_state)
        self.update_button.configure(state=update_state)
        for box in self.target_boxes:
            box.configure(state=target_state)
        for button in self.preset_buttons:
            button.configure(state=button_state)

        if self.action_running:
            self.progress.start(12)
        else:
            self.progress.stop()

    def set_action_running(self, running: bool, status: str | None = None) -> None:
        self.action_running = running
        if status:
            self.status_var.set(status)
        self.refresh_controls()

    def start_cleanup(self) -> None:
        selected_targets = [target for target in self.targets if self.target_vars[target.key].get()]
        if not selected_targets:
            messagebox.showinfo(APP_NAME, "Select at least one target.")
            return

        confirmation = "This will permanently remove files from the selected temp and cache locations. Continue?"
        if any(target.category == "advanced" for target in selected_targets):
            confirmation += (
                "\n\nAdvanced caches were selected. Some apps may need to rebuild cache data after cleanup, "
                "and open browsers or launchers may prevent some files from being removed."
            )

        if not messagebox.askyesno(APP_NAME, confirmation):
            return

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

        self.summary_var.set("Cleanup in progress...")
        self.set_action_running(True, "Cleaning selected folders and cache targets...")

        self.worker_thread = threading.Thread(
            target=self.run_cleanup,
            args=(selected_targets,),
            daemon=True,
        )
        self.worker_thread.start()
        self.root.after(100, self.process_queue)

    def run_cleanup(self, selected_targets: list[CleanupTarget]) -> None:
        total = CleanupStats(label="Total", path="")
        for target in selected_targets:
            self.queue.put(("status", f"Cleaning {target.label}..."))
            self.queue.put(("log", f"[{target.label}] {target.location}"))
            stats = clean_target(target, lambda message: self.queue.put(("log", message)))
            stats.merge_into(total)
            self.queue.put(("target_result", stats))
            self.queue.put(("log", ""))

        self.queue.put(("cleanup_done", total))

    def check_for_updates(self) -> None:
        self.start_update_check(silent=False)

    def start_update_check(self, silent: bool) -> None:
        if not self.release_config.configured or self.update_check_running:
            return

        self.update_check_running = True
        self.refresh_controls()
        if not silent:
            self.status_var.set("Checking GitHub Releases for updates...")

        threading.Thread(
            target=self.run_update_check,
            args=(silent,),
            daemon=True,
        ).start()
        self.root.after(100, self.process_queue)

    def run_update_check(self, silent: bool) -> None:
        try:
            release = fetch_latest_release(self.release_config)
        except RuntimeError as exc:
            self.queue.put(("update_check_error", (str(exc), silent)))
            return

        if is_newer_version(release.version, APP_VERSION):
            self.queue.put(("update_check_available", (release, silent)))
            return

        self.queue.put(("update_check_latest", silent))

    def begin_update_download(self, release: ReleaseInfo) -> None:
        if not getattr(sys, "frozen", False):
            if release.page_url:
                webbrowser.open(release.page_url)
            messagebox.showinfo(
                APP_NAME,
                "Self-install only works from the packaged .exe build. The release page has been opened instead.",
            )
            return

        self.latest_release = release
        self.set_action_running(True, f"Downloading version {release.version}...")
        self.summary_var.set(f"Downloading update v{release.version}...")
        threading.Thread(
            target=self.run_update_download,
            args=(release,),
            daemon=True,
        ).start()
        self.root.after(100, self.process_queue)

    def run_update_download(self, release: ReleaseInfo) -> None:
        temp_dir = Path(tempfile.gettempdir()) / "SystemCleanupUtility-update" / release.version
        destination = temp_dir / release.asset_name
        try:
            download_release_asset(release, destination)
        except RuntimeError as exc:
            self.queue.put(("update_download_error", str(exc)))
            return

        self.queue.put(("update_download_ready", destination))

    def process_queue(self) -> None:
        should_poll_again = False

        while True:
            try:
                item_type, payload = self.queue.get_nowait()
            except Empty:
                break

            if item_type == "status":
                self.status_var.set(str(payload))
            elif item_type == "log":
                self.append_log(str(payload))
            elif item_type == "target_result":
                if isinstance(payload, CleanupStats):
                    self.append_log(self.format_stats(payload))
            elif item_type == "cleanup_done":
                if isinstance(payload, CleanupStats):
                    self.finish_cleanup(payload)
            elif item_type == "update_check_available":
                release, silent = payload
                self.handle_update_available(release, silent)
            elif item_type == "update_check_latest":
                self.handle_update_latest(bool(payload))
            elif item_type == "update_check_error":
                message, silent = payload
                self.handle_update_check_error(str(message), bool(silent))
            elif item_type == "update_download_ready":
                if isinstance(payload, Path):
                    self.handle_update_download_ready(payload)
            elif item_type == "update_download_error":
                self.handle_update_download_error(str(payload))

        if self.action_running or self.update_check_running:
            worker_alive = bool(self.worker_thread and self.worker_thread.is_alive())
            should_poll_again = worker_alive or self.update_check_running or self.action_running

        if should_poll_again:
            self.root.after(100, self.process_queue)

    def format_stats(self, stats: CleanupStats) -> str:
        if stats.missing_root:
            return "Result: no matching files or folders were found."
        return (
            "Result: "
            f"{stats.deleted_files} files, "
            f"{stats.deleted_dirs} folders removed, "
            f"{stats.skipped_items} skipped, "
            f"{stats.failed_items} failed, "
            f"{format_bytes(stats.bytes_freed)} freed."
        )

    def finish_cleanup(self, total: CleanupStats) -> None:
        self.set_action_running(False, "Cleanup complete.")
        self.summary_var.set(
            "Completed: "
            f"{total.deleted_files} files and {total.deleted_dirs} folders removed, "
            f"{format_bytes(total.bytes_freed)} freed."
        )

        if total.failed_items:
            self.append_log(
                f"Finished with {total.failed_items} items that could not be removed. "
                "Close running apps and try again if you want a deeper cleanup."
            )

    def handle_update_available(self, release: ReleaseInfo, silent: bool) -> None:
        self.update_check_running = False
        self.latest_release = release
        self.update_var.set(f"Update available: v{release.version}")
        self.status_var.set(f"Version {release.version} is available.")
        self.refresh_controls()

        if self.action_running:
            return

        prompt = messagebox.askyesno(
            APP_NAME,
            f"Version {release.version} is available. Download and install it now?",
        )
        if prompt:
            self.begin_update_download(release)
        elif not silent:
            self.status_var.set("Update skipped.")

    def handle_update_latest(self, silent: bool) -> None:
        self.update_check_running = False
        self.update_var.set(f"You're on the latest version: v{APP_VERSION}")
        if not silent:
            self.status_var.set("No update found.")
            messagebox.showinfo(APP_NAME, f"You're already on version {APP_VERSION}.")
        self.refresh_controls()

    def handle_update_check_error(self, message: str, silent: bool) -> None:
        self.update_check_running = False
        self.update_var.set("Update check unavailable right now.")
        if not silent:
            self.status_var.set("Update check failed.")
            messagebox.showerror(APP_NAME, message)
        self.refresh_controls()

    def handle_update_download_ready(self, downloaded_path: Path) -> None:
        try:
            start_update_installer(Path(sys.executable), downloaded_path)
        except OSError as exc:
            self.handle_update_download_error(f"Could not start the updater helper: {exc}")
            return

        if self.latest_release:
            self.update_var.set(f"Installing v{self.latest_release.version}...")
        self.status_var.set("Applying update and restarting...")
        messagebox.showinfo(
            APP_NAME,
            "The update has been downloaded. The app will close and reopen to finish installing.",
        )
        self.root.after(150, self.root.destroy)

    def handle_update_download_error(self, message: str) -> None:
        self.set_action_running(False, "Update download failed.")
        if self.latest_release:
            self.update_var.set(f"Could not install v{self.latest_release.version}.")
        messagebox.showerror(APP_NAME, message)


def main() -> None:
    if os.name != "nt":
        raise SystemExit("This utility is intended for Windows.")

    if not is_admin() and not getattr(sys, "frozen", False):
        root = tk.Tk()
        root.withdraw()
        should_relaunch = messagebox.askyesno(
            APP_NAME,
            "Administrator access is recommended so Windows Temp and Prefetch can be cleaned. Relaunch as administrator?",
        )
        root.destroy()
        if should_relaunch and relaunch_as_admin():
            return

    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")

    CleanupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
