"""Octopus 桌面版启动器 —— PyInstaller 打包入口。

双击 Octopus.exe → 起本地后端(127.0.0.1:8000) → 后端服务前端 dist → 自动开浏览器。

默认 local 模式：agent、出图、出视频都在本机调度，用户在设置里填自己的 key。

数据落 %LOCALAPPDATA%\\Octopus\\（DB、output），不写安装目录（避免 Program Files 权限坑）。
"""
import os
import sys
import time
import threading
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("OCTOPUS_PORT", "8000"))
URL = f"http://{HOST}:{PORT}"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _bundle_base() -> Path:
    """打包后 = PyInstaller 解包目录(_MEIPASS)；源码运行 = 仓库 octopus/ 根。"""
    if _frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


def _user_data_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Octopus"
    (root / "output" / "images").mkdir(parents=True, exist_ok=True)
    (root / "output" / "videos").mkdir(parents=True, exist_ok=True)
    return root


def _setup_env() -> None:
    base = _bundle_base()
    data = _user_data_dir()

    if _frozen():
        frontend = base / "frontend_dist"
        ffmpeg_dir = base / "ffmpeg"
    else:
        # 源码直接跑 launcher.py（打包前本地验证用）
        frontend = base / "frontend" / "dist"
        ffmpeg_dir = base / "ffmpeg"
        sys.path.insert(0, str(base / "backend"))

    os.environ.setdefault("OCTOPUS_FRONTEND_DIST", str(frontend))
    os.environ.setdefault("OCTOPUS_OUTPUT_DIR", str(data / "output"))
    # 桌面版默认云端优先（用户可在设置页「运行模式」切 local 自用）
    os.environ.setdefault("OCTOPUS_DEPLOYMENT_MODE", "local")

    # ffmpeg：把 bundled 目录塞进 PATH，各处 subprocess(["ffmpeg", ...]) 直接可用
    if ffmpeg_dir.is_dir():
        os.environ["PATH"] = str(ffmpeg_dir) + os.pathsep + os.environ.get("PATH", "")

    # windowed(console=False) 打包时 sys.stdout/stderr 为 None，uvicorn 一写日志就崩 →
    # 重定向到用户数据目录下的日志文件（也方便用户报错时把日志发来）；文件建不了则退到 devnull
    if sys.stdout is None or sys.stderr is None:
        _target = None
        try:
            logdir = data / "logs"
            logdir.mkdir(parents=True, exist_ok=True)
            _target = open(logdir / "octopus.log", "a", encoding="utf-8", buffering=1)
        except Exception:
            try:
                _target = open(os.devnull, "w")
            except Exception:
                _target = None
        if _target is not None:
            if sys.stdout is None:
                sys.stdout = _target
            if sys.stderr is None:
                sys.stderr = _target

    # DB(./octopus.db) 及一切相对路径产物落用户数据目录，而非安装目录
    os.chdir(str(data))


def _free_port(port: int) -> None:
    """启动前清掉占用本端口的残留进程（上次没退干净的实例、抢端口的旧进程等），
    避免端口冲突导致起不来。只针对监听在本端口上的 TCP 进程；
    设 OCTOPUS_NO_PORT_KILL=1 可关闭此行为。"""
    import subprocess

    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    my_pid = str(os.getpid())
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10, creationflags=no_window,
        ).stdout
    except Exception:
        return

    targets = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or not parts[0].upper().startswith("TCP"):
            continue
        local, foreign, pid = parts[1], parts[2], parts[-1]
        # 监听态：foreign 为通配 0.0.0.0:0 / [::]:0（locale 无关）；本地端口匹配
        if (pid.isdigit() and pid not in (my_pid, "0")
                and local.endswith(f":{port}")
                and foreign in ("0.0.0.0:0", "[::]:0", "*:*")):
            targets.add(pid)

    if not targets:
        return
    for pid in targets:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F", "/T"],
                           capture_output=True, timeout=10, creationflags=no_window)
        except Exception:
            pass

    # 等端口真正释放（taskkill 后可能还有短暂占用）
    import socket
    for _ in range(15):  # 最多 ~3s
        try:
            with socket.create_connection((HOST, port), timeout=0.3):
                time.sleep(0.2)  # 还有人 listen，继续等
        except OSError:
            return  # 连不上 = 端口已空


def _open_browser_when_ready() -> None:
    """等 /health 通了再开浏览器，避免用户看到连接失败。"""
    import urllib.request

    for _ in range(120):  # 最多等 ~60s
        try:
            with urllib.request.urlopen(f"{URL}/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.5)
    try:
        webbrowser.open(URL)
    except Exception:
        pass


def main() -> None:
    _setup_env()

    # 启动前清端口：干掉占用 8000 的残留进程，避免上次没退干净导致冲突起不来
    if not os.environ.get("OCTOPUS_NO_PORT_KILL"):
        _free_port(PORT)

    import uvicorn
    from app.main import app  # 必须在 _setup_env() 之后 import（env 要先就位）

    if not os.environ.get("OCTOPUS_NO_BROWSER"):
        threading.Thread(target=_open_browser_when_ready, daemon=True).start()
        print(f"Octopus 正在启动，浏览器将自动打开 {URL} ……")
    else:
        print(f"Octopus 正在启动（NO_BROWSER）：{URL}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
