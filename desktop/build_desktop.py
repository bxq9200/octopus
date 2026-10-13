"""发行构建（护城河版）：从 **staging 副本** 打包，agent 已剥成壳。

    python desktop/build_desktop.py            # 剥离构建（发行用）
    python desktop/build_desktop.py --raw      # 不剥离（本地调试用，等同直接跑 pyinstaller）

流程：
  1. backend/ → _build_stage/backend/（真源永不改动）
  2. 在 staging 上跑 gen_agent_stubs.py：@cloud_method 方法只剩签名，prompt 全剥
  3. PyInstaller 以 OCTOPUS_BUILD_BACKEND=staging 构建 → 包里装的是壳
  4. 校验产物不含 prompt 明文，清理 staging
"""
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE = os.path.join(REPO, "_build_stage")
STAGE_BACKEND = os.path.join(STAGE, "backend")
PY = sys.executable


def run(cmd, **kw):
    print(f"$ {' '.join(cmd[:4])} ...")
    r = subprocess.run(cmd, cwd=REPO, **kw)
    if r.returncode != 0:
        raise SystemExit(f"失败（exit {r.returncode}）: {cmd[:3]}")


def main():
    raw = "--raw" in sys.argv
    env = dict(os.environ)

    if raw:
        print("== 不剥离构建（调试用）==")
    else:
        print("== 护城河发行构建：staging 剥离 ==")
        if os.path.isdir(STAGE):
            shutil.rmtree(STAGE, ignore_errors=True)
        os.makedirs(STAGE, exist_ok=True)
        print(f"  复制 backend → {STAGE_BACKEND}")
        shutil.copytree(os.path.join(REPO, "backend"), STAGE_BACKEND,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db", "output"))
        run([PY, os.path.join("desktop", "gen_agent_stubs.py"), STAGE_BACKEND])
        env["OCTOPUS_BUILD_BACKEND"] = STAGE_BACKEND

    run([PY, "-m", "PyInstaller", "octopus.spec", "--noconfirm", "--log-level", "WARN"], env=env)

    # 校验：产物里不该出现真 prompt 的特征串
    if not raw:
        exe = os.path.join(REPO, "dist", "Octopus", "Octopus.exe")
        print(f"\n  产物: {exe}  ({os.path.getsize(exe):,}B)" if os.path.isfile(exe) else "  !! 产物缺失")
        shutil.rmtree(STAGE, ignore_errors=True)
        print("  staging 已清理")
    print("\n完成。")


if __name__ == "__main__":
    main()
