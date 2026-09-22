import os
import sys
import time
import socket
import subprocess
import threading
from pathlib import Path

# Universal DNS fallback resolver
try:
    from backend.utils.dns_resolver import install_resilient_dns
    install_resilient_dns()
except Exception:
    pass

# Suppress stdout/stderr errors under pythonw.exe
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Path resolution for script or PyInstaller frozen executable
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

sys.path.insert(0, str(BASE_DIR))

APP_DATA_DIR = Path(os.path.expandvars(r"%LocalAppData%\EmailAutomater"))
APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
WEBVIEW_CACHE_DIR = APP_DATA_DIR / "webview_cache"
WEBVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)

SERVER_INSTANCE = None
SERVER_ERROR_MESSAGE = None
_MUTEX_HANDLE = None


# ----------------- System & Single Instance Controls ----------------- #

def set_high_dpi_awareness():
    """Configures high-DPI scaling on Windows to prevent blurry UI rendering."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # Per-Monitor V2
        except Exception:
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    import ctypes
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass


def acquire_single_instance_lock() -> bool:
    """Uses a Windows Named Mutex to guarantee only one application instance runs."""
    global _MUTEX_HANDLE
    if sys.platform == "win32":
        try:
            import ctypes
            mutex_name = "Global\\EMA_UniversalAssistant_SingleInstance_Mutex"
            kernel32 = ctypes.windll.kernel32
            _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, mutex_name)
            last_error = kernel32.GetLastError()
            ERROR_ALREADY_EXISTS = 183

            if last_error == ERROR_ALREADY_EXISTS:
                # Existing instance running -> bring it to front and exit
                try:
                    ps_script = (
                        '$wshell = New-Object -ComObject WScript.Shell; '
                        '$wshell.AppActivate("EMA"); '
                        '$wshell.AppActivate("Smart Universal Email Assistant"); '
                        '$wshell.AppActivate("EmailAutomater")'
                    )
                    subprocess.Popen(
                        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                    )
                except Exception:
                    pass
                return False
        except Exception:
            pass
    return True


def is_port_in_use(host: str = "127.0.0.1", port: int = 8000) -> bool:
    """Fast socket check to see if port 8000 is occupied."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


def free_port(port: int = 8000):
    """Safely frees the port if occupied by an orphaned instance, ignoring the current process."""
    if not is_port_in_use("127.0.0.1", port):
        return

    current_pid = str(os.getpid())
    if sys.platform == "win32":
        try:
            cmd = f'netstat -ano | findstr /R /C:":{port} " | findstr "LISTENING"'
            output = subprocess.check_output(cmd, shell=True, creationflags=CREATE_NO_WINDOW).decode()
            for line in output.strip().splitlines():
                parts = line.strip().split()
                if len(parts) >= 5:
                    pid = parts[-1]
                    if pid != current_pid and pid != "0":
                        subprocess.run(
                            f"taskkill /F /PID {pid}",
                            shell=True,
                            capture_output=True,
                            creationflags=CREATE_NO_WINDOW,
                        )
            time.sleep(0.3)
        except Exception:
            pass


# ----------------- Uvicorn Server Management ----------------- #

def wait_for_server(host: str = "127.0.0.1", port: int = 8000, timeout: float = 25.0) -> bool:
    """Polls until the FastAPI server is accepting HTTP connections."""
    start_time = time.time()
    while time.time() - start_time < timeout:
        if SERVER_ERROR_MESSAGE:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.15)
    return False


def run_uvicorn_server():
    """Runs FastAPI backend using embedded Uvicorn server in a dedicated thread."""
    global SERVER_INSTANCE, SERVER_ERROR_MESSAGE
    try:
        import uvicorn
        from backend.main import app

        uv_config = uvicorn.Config(
            app=app,
            host="127.0.0.1",
            port=8000,
            log_level="error",
            access_log=False,
            loop="asyncio",
            timeout_keep_alive=15,
        )
        SERVER_INSTANCE = uvicorn.Server(uv_config)
        SERVER_INSTANCE.install_signal_handlers = lambda: None
        SERVER_INSTANCE.run()
    except Exception:
        import traceback
        SERVER_ERROR_MESSAGE = traceback.format_exc()
        try:
            with open(BASE_DIR / "backend_crash.log", "w", encoding="utf-8") as f:
                f.write(SERVER_ERROR_MESSAGE)
        except Exception:
            pass


def shutdown_server():
    """Gracefully terminates Uvicorn and enforces process termination to prevent zombie threads."""
    global SERVER_INSTANCE
    if SERVER_INSTANCE:
        SERVER_INSTANCE.should_exit = True

    def watchdog_force_exit():
        time.sleep(1.2)
        os._exit(0)

    # Clean exit thread ensures UI doesn't hang waiting for SSE generator loops
    threading.Thread(target=watchdog_force_exit, daemon=True).start()


# ----------------- Window Runners ----------------- #

def launch_native_app_window():
    """Fallback desktop runner using Edge/Chrome application mode if PyWebView is unavailable."""
    paths = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    browser_exe = next((p for p in paths if os.path.exists(p)), None)
    profile_dir = APP_DATA_DIR / "browser_app_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    if browser_exe:
        cmd = [
            browser_exe,
            "--app=http://127.0.0.1:8000",
            f"--user-data-dir={profile_dir}",
            "--start-maximized",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ]
        proc = subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
        proc.wait()
        shutdown_server()
    else:
        import webbrowser
        webbrowser.open("http://127.0.0.1:8000")
        try:
            while True:
                time.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            shutdown_server()


# ----------------- Main Entrypoint ----------------- #

def main():
    set_high_dpi_awareness()

    # 1. Enforce single-instance lock
    if not acquire_single_instance_lock():
        sys.exit(0)

    # 2. Free stale port bindings
    free_port(8000)

    # 3. Start backend server in dedicated daemon thread
    server_thread = threading.Thread(target=run_uvicorn_server, daemon=True)
    server_thread.start()

    # 4. Wait for backend to accept connections
    if not wait_for_server("127.0.0.1", 8000, timeout=25.0):
        import ctypes
        err = SERVER_ERROR_MESSAGE or "Backend server failed to bind to port 8000 within 25 seconds."
        ctypes.windll.user32.MessageBoxW(
            0,
            f"Startup Error:\n{err[:400]}\n\nReview backend_crash.log for complete diagnostics.",
            "EMA Assistant Error",
            0x10,
        )
        shutdown_server()
        sys.exit(1)

    # 5. Launch native desktop window via PyWebView
    launched = False
    try:
        import webview

        def on_closed():
            shutdown_server()

        def maximize_window(win):
            time.sleep(0.4)
            try:
                win.maximize()
            except Exception:
                pass

        window = webview.create_window(
            title="Smart Universal Email Assistant",
            url="http://127.0.0.1:8000",
            width=1280,
            height=820,
            min_size=(960, 620),
            resizable=True,
            background_color="#060913",
        )
        window.events.closed += on_closed
        threading.Thread(target=maximize_window, args=(window,), daemon=True).start()

        # Run WebView with EdgeChromium backend and persistent user data directory
        webview.start(
            gui="edgechromium",
            debug=False,
            storage_path=str(WEBVIEW_CACHE_DIR),
            private_mode=False,
        )
        shutdown_server()
        launched = True
    except Exception:
        pass

    # 6. Fallback to standalone app mode if PyWebView encountered initialization errors
    if not launched:
        launch_native_app_window()


if __name__ == "__main__":
    main()