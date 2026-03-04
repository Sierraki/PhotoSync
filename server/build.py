"""
PhotoSync 服务器打包脚本
使用 PyInstaller 打包成单个可执行文件
"""
import subprocess
import sys
import os

def main():
    # 确保在 server 目录下运行
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)

    # 安装 PyInstaller（如果没有）
    try:
        import PyInstaller
    except ImportError:
        print("正在安装 PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # PyInstaller 命令
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name=PhotoSync",
        "--onefile",                    # 打包成单个文件
        # 不用 --windowed，保留控制台窗口显示服务器状态
        "--icon=static/favicon.ico",    # 图标（如果有）
        "--add-data=templates;templates",  # 包含模板目录
        "--add-data=static;static",        # 包含静态文件目录
        "--hidden-import=uvicorn.logging",
        "--hidden-import=uvicorn.loops",
        "--hidden-import=uvicorn.loops.auto",
        "--hidden-import=uvicorn.protocols",
        "--hidden-import=uvicorn.protocols.http",
        "--hidden-import=uvicorn.protocols.http.auto",
        "--hidden-import=uvicorn.protocols.websockets",
        "--hidden-import=uvicorn.protocols.websockets.auto",
        "--hidden-import=uvicorn.lifespan",
        "--hidden-import=uvicorn.lifespan.on",
        "--hidden-import=email.mime.multipart",
        "--hidden-import=email.mime.text",
        "--collect-submodules=uvicorn",
        "--collect-submodules=fastapi",
        "--clean",
        "main.py"
    ]

    # 如果没有图标文件，移除图标参数
    if not os.path.exists("static/favicon.ico"):
        cmd = [c for c in cmd if not c.startswith("--icon=")]

    print("开始打包...")
    print(" ".join(cmd))
    subprocess.check_call(cmd)

    print("\n" + "=" * 50)
    print("打包完成！")
    print(f"可执行文件位置: {os.path.join(script_dir, 'dist', 'PhotoSync.exe')}")
    print("=" * 50)

if __name__ == "__main__":
    main()
