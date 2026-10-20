# Track_any_call

Track_any_call 记录 Python 程序运行时的函数调用，再根据记录在 VS Code 中跳到对应的实现。录制器、安装包和编辑器命令目前都叫 CallNav。

[English](README.md) · [详细用法](docs/usage.md) · [实现原理](docs/architecture.md)

## 安装

需要 Python 3.11+ 和 VS Code 1.85+。先激活平时运行项目的 Python 环境，再安装：

```bash
git clone https://github.com/Zethan06/Track_any_call.git
cd Track_any_call
python -m pip install .
python vscode/scripts/package.py
```

最后一条命令会输出插件文件的位置。在 VS Code 命令面板执行 `Extensions: Install from VSIX…`，选择生成的 `.vsix` 文件。使用 Remote-SSH 时，把插件安装到远程主机。

## 使用

进入自己项目的工作目录，在原来的启动命令前加上 `callnav record`：

```bash
cd /path/to/your-project
callnav record --root "$PWD" -- bash ./launch.sh
```

把原来的命令和参数放在 `--` 后面。录制结束后，在 VS Code 中打开同一个项目目录，然后：

1. 执行 `CallNav: Load Latest Runtime Index`。
2. 执行 `CallNav: Enable Runtime Navigation`。
3. 按住 Ctrl 点击调用位置；macOS 使用 Cmd。

录制 Ray Actor 调用时加上 `--ray`。如果还要查看项目目录之外的源码，用 `--include-root` 指定：

```bash
callnav record --ray --root "$PWD" --include-root ../external-library -- bash ./launch.sh
```

Ray worker 需要继承录制环境，并且能访问源码和记录目录。配置方法见[使用说明](docs/usage.md#ray-and-framework-launchers)。

记录保存在项目的 `.callnav/` 目录，建议加入 `.gitignore`。改过源码或换过目录后，需要重新录制。分享记录前检查一下里面的路径和启动参数。

运行记录只包含这次执行并成功采集的调用。录制会增加运行开销，建议先跑较短的任务；C/CUDA 内部调用不在记录范围内。

[开发与测试](CONTRIBUTING.md) · [MIT 许可](LICENSE)
