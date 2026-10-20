# Track_any_call

**运行一次 Python 程序，再在 VS Code 中 Ctrl+点击，跳到本次实际执行的函数。**

Track_any_call 包含 **CallNav** 录制器与 VS Code 插件。目前安装包、命令行和编辑器菜单沿用 CallNav 名称。

[English](README.md) · [详细使用说明](docs/usage.md) · [实现原理](docs/architecture.md)

## 特征

- **追踪真实动态调用**：支持注册表、动态选择的子类、装饰器、`functools.partial` 和支持范围内的异步调用。
- **原生 VS Code 跳转**：Ctrl/Cmd+点击或“转到定义”，多个实际实现显示为候选；保留 Python/Pylance 功能。
- **线程、子进程与 Ray**：采集参与录制的 Python 进程，可选关联 Ray Actor 方法、构造器与 worker 的真实执行。
- **源码校验**：通过指纹检查，避免旧记录跳到已改变的代码。

## 安装

需要 **Python 3.11+**、**VS Code 1.85+**。先激活平时运行项目的 Python 环境。

```bash
git clone https://github.com/Zethan06/Track_any_call.git
cd Track_any_call
python -m pip install .
python vscode/scripts/package.py
```

在 VS Code 执行 **Extensions: Install from VSIX…**，选择生成的 `.vsix`。使用 Remote-SSH 时，安装到远程 workspace 所在主机。

## 录制与跳转

在自己的项目工作目录，包装原有启动脚本：

```bash
cd /path/to/your-project
callnav record --root "$PWD" -- bash ./launch.sh
```

在 VS Code 打开该项目，然后：

1. 执行 **CallNav: Load Latest Runtime Index**。
2. 执行 **CallNav: Enable Runtime Navigation**。
3. Ctrl/Cmd+点击执行过的调用。

Ray 项目和外部源码可这样录制：

```bash
callnav record --ray --root "$PWD" --include-root ../external-library -- bash ./launch.sh
```

想先体验无需 GPU 的例子，可在本仓库执行 `callnav record --root "$PWD" -- bash examples/mini_pipeline/launch.sh`，跳转位置见 [样例说明](examples/README.md)。

## 范围

只为执行过且支持定位的调用提供运行时目标。采集会增加开销；不记录 C/CUDA 内部或 HTTP 因果链。Ray worker 需要继承采集环境，并能访问共享路径。

记录保存在 `.callnav/`，使用当前源码路径。代码修改或迁移目录后应重新录制；将 `.callnav/` 加入 `.gitignore`，分享记录前检查源码路径与启动参数。

[详细配置与排障](docs/usage.md) · [参与开发与测试](CONTRIBUTING.md) · [MIT 许可](LICENSE)
