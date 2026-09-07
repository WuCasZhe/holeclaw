import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    from holeclaw_domain import CliError
except ModuleNotFoundError:
    from scripts.holeclaw_domain import CliError

SITE_URL = "https://treehole.pku.edu.cn/ch/web/pc/index"
CONFIG_KEY = "codex_pku_digest_config"
PATH_ARGUMENTS = {"--state", "--cache", "--source-cache", "--checkpoint", "--output",
                  "-s", "-C", "-S", "-k", "-o"}


def codex_base() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))


def running_on_windows() -> bool:
    return os.name == "nt"


def is_wsl() -> bool:
    return not running_on_windows() and bool(os.environ.get("WSL_DISTRO_NAME"))


def playwright_npx_path() -> str | None:
    override = os.environ.get("PLAYWRIGHT_CLI_NPX")
    if override:
        return override
    windows_npx = Path("/mnt/c/Program Files/nodejs/npx")
    if is_wsl() and not shutil.which("google-chrome") and windows_npx.is_file():
        return str(windows_npx)
    return shutil.which("npx")


def is_windows_mounted_path(path: str | None) -> bool:
    if not path:
        return False
    normalized = str(Path(path).expanduser()).replace("\\", "/")
    return normalized == "/mnt" or normalized.startswith("/mnt/")


def windows_native_path(value: str) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        return value
    completed = subprocess.run(
        ["wslpath", "-w", str(path.resolve())],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def windows_cli_arguments(arguments: list[str]) -> list[str]:
    converted = []
    expecting_path = False
    for argument in arguments:
        if expecting_path:
            converted.append(windows_native_path(argument))
            expecting_path = False
            continue
        matched_option = next(
            (option for option in PATH_ARGUMENTS if argument.startswith(option + "=")),
            None,
        )
        if matched_option:
            _option, value = argument.split("=", 1)
            converted.append(f"{matched_option}={windows_native_path(value)}")
            continue
        # argparse also accepts attached short values, e.g. -o/tmp/report.json.
        short_option = next((option for option in PATH_ARGUMENTS
                             if len(option) == 2 and argument.startswith(option)
                             and len(argument) > 2), None)
        if short_option:
            converted.extend([short_option, windows_native_path(argument[2:])])
            continue
        converted.append(argument)
        expecting_path = argument in PATH_ARGUMENTS
    return converted


def windows_python_command() -> list[str] | None:
    python = shutil.which("python.exe")
    if python:
        return [python]
    launcher = shutil.which("py.exe")
    return [launcher, "-3"] if launcher else None


def maybe_reexec_windows_runtime(arguments: list[str] | None = None) -> int | None:
    if not is_wsl() or not is_windows_mounted_path(playwright_npx_path()):
        return None
    python_command = windows_python_command()
    if not python_command:
        raise CliError(
            "Playwright 将使用 Windows Node，但未找到 Windows Python。"
            "请安装 Windows Python，或在 WSL 内安装原生 Node.js 和浏览器。"
        )
    child_environment = os.environ.copy()
    child_environment["PYTHONUTF8"] = "1"
    child_environment.pop("PWCLI", None)
    child_environment.pop("PLAYWRIGHT_CLI_NPX", None)
    codex_home = child_environment.get("CODEX_HOME")
    if codex_home and not is_windows_mounted_path(codex_home):
        child_environment.pop("CODEX_HOME", None)
    runtime_root = child_environment.get("HOLECLAW_RUNTIME_DIR")
    if runtime_root:
        native_runtime = windows_native_path(runtime_root)
        if native_runtime.startswith("\\\\wsl"):
            raise CliError(
                "Windows 采集运行时不能把 SQLite 放在 WSL 文件系统。"
                "请取消 HOLECLAW_RUNTIME_DIR，或将其设置到 /mnt/c 下。"
            )
        child_environment["HOLECLAW_RUNTIME_DIR"] = native_runtime
    child_arguments = windows_cli_arguments(
        list(sys.argv[1:] if arguments is None else arguments)
    )
    command = [
        *python_command,
        windows_native_path(str(Path(__file__).with_name("run_digest.py").resolve())),
        *child_arguments,
    ]
    print(
        "检测到 Windows Playwright 依赖；切换到 Windows Python，"
        "确保浏览器、本地回调和 SQLite 位于同一运行环境。",
        flush=True,
    )
    return subprocess.run(command, env=child_environment).returncode


def find_pwcli() -> Path:
    override = os.environ.get("PWCLI")
    wrapper_name = (
        "playwright_cli.cmd" if running_on_windows() else "playwright_cli.sh"
    )
    local_wrapper = Path(__file__).with_name(wrapper_name)
    codex_wrapper = codex_base() / "skills/playwright/scripts" / wrapper_name
    candidates = [Path(override)] if override else [local_wrapper, codex_wrapper]
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    if path is None:
        checked = ", ".join(str(candidate) for candidate in candidates)
        raise CliError(f"Playwright wrapper not found. Checked: {checked}")
    npx = (
        shutil.which("npx.cmd") or shutil.which("npx")
        if running_on_windows()
        else playwright_npx_path()
    )
    if not npx:
        raise CliError("npx is required. Install Node.js/npm first.")
    return path


def native_path(path: Path) -> str:
    # Windows-backed WSL launches are re-executed before reaching BrowserCli.
    return str(path.resolve())


class BrowserCli:
    def __init__(self, session: str, headed: bool = True):
        self.pwcli = find_pwcli()
        self.session = session
        self.headed = headed

    def run(self, *args: str, check: bool = True, raw: bool = False) -> subprocess.CompletedProcess:
        command = [str(self.pwcli), f"-s={self.session}"]
        if raw:
            command.append("--raw")
        command.extend(args)
        completed = subprocess.run(
            command,
            text=True,
            encoding="utf-8",
            capture_output=True,
            errors="replace",
        )
        if check and completed.returncode != 0:
            message = (completed.stdout + "\n" + completed.stderr).strip()
            raise CliError(message[-3000:])
        return completed

    def ensure_session(self) -> None:
        listing = subprocess.run(
            [str(self.pwcli), "list"],
            text=True,
            encoding="utf-8",
            capture_output=True,
            errors="replace",
        )
        if self.session not in listing.stdout:
            arguments = ["open", "about:blank"]
            if self.headed:
                arguments.append("--headed")
            self.run(*arguments)


def ensure_gitignore_entry(entry: str) -> None:
    ignore = Path.cwd() / ".gitignore"
    current = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if entry not in current.splitlines():
        suffix = "" if not current or current.endswith("\n") else "\n"
        ignore.write_text(current + suffix + entry + "\n", encoding="utf-8")


def ensure_path_ignored(path: Path, root: Path, entry: str) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    ensure_gitignore_entry(entry)
    return True


def ensure_auth_ignored(state: Path) -> None:
    ensure_path_ignored(state, Path.cwd() / ".auth", ".auth/")


def ensure_runtime_ignored(*paths: Path) -> None:
    runtime_root = (Path.cwd() / "output/playwright").resolve()
    for path in paths:
        if ensure_path_ignored(path, runtime_root, "output/playwright/"):
            return


def authenticated(snapshot: str) -> bool:
    return "Page Title: 北大树洞" in snapshot and "treehole.pku.edu.cn/ch/web/pc/index" in snapshot

def login_open(args: argparse.Namespace) -> None:
    browser = BrowserCli(args.session)
    browser.ensure_session()
    browser.run("goto", SITE_URL)
    print("可视浏览器已打开。请亲自完成北大统一身份认证，进入树洞首页后再保存登录状态。")


def login_save(args: argparse.Namespace) -> None:
    state = args.state.resolve()
    browser = BrowserCli(args.session)
    browser.ensure_session()
    save_login_state(browser, state)


def save_login_state(browser: BrowserCli, state: Path) -> None:
    snapshot = browser.run("snapshot").stdout
    if not authenticated(snapshot):
        raise CliError("当前页面仍未进入北大树洞首页，请先完成登录。")
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    browser.run("state-save", native_path(state))
    state.chmod(0o600)
    ensure_auth_ignored(state)
    print(f"登录状态已保存：{state}")


def load_authenticated_state(browser: BrowserCli, state: Path) -> bool:
    browser.run("goto", "about:blank")
    browser.run("state-load", native_path(state))
    browser.run("goto", SITE_URL)
    return authenticated(browser.run("snapshot").stdout)


def ensure_standalone_login(args: argparse.Namespace) -> BrowserCli:
    state = args.state.resolve()
    if not state.is_file() and args.non_interactive:
        raise CliError(
            "未找到本地登录状态；--non-interactive 模式不会等待人工登录。"
            "请先不带该参数运行一次 standalone。"
        )

    browser = BrowserCli(args.session, headed=not args.non_interactive)
    browser.ensure_session()
    if state.is_file():
        try:
            if load_authenticated_state(browser, state):
                print("已加载有效登录状态，继续自动采集。", flush=True)
                return browser
            reason = "已保存的登录状态已失效"
        except CliError:
            reason = "已保存的登录状态无法加载"
            if not args.non_interactive:
                browser.run("goto", SITE_URL)
    else:
        browser.run("goto", SITE_URL)
        reason = "未找到本地登录状态"

    if args.non_interactive:
        raise CliError(
            f"{reason}；--non-interactive 模式不会等待人工登录。"
            "请先不带该参数运行一次 standalone。"
        )

    print(
        f"{reason}。可视浏览器已打开，请亲自完成北大统一身份认证。\n"
        "进入北大树洞首页后，回到此终端按 Enter 继续。",
        flush=True,
    )
    try:
        input()
    except EOFError as error:
        raise CliError("需要交互式终端完成首次登录。") from error
    save_login_state(browser, state)
    return browser
