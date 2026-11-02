# Track_any_call

Track_any_call 记录 Python 运行时的函数调用，用来确定动态绑定后实际执行的是哪个实现。遇到注册表加载的函数、抽象接口背后的子类方法等调用，可以在 VS Code 中根据本次运行的记录跳到实际执行的函数。

录制器、安装包和编辑器命令目前都叫 CallNav。

[English](README.md) · [详细用法](docs/usage.md) · [实现原理](docs/architecture.md)

## 安装

需要工作区主机上的 Python 3.11+ 和 VS Code 1.85+。从成功的 [Checks 构建](https://github.com/Zethan06/Track_any_call/actions/workflows/checks.yml)下载 `callnav-release` 产物，解压后在 VS Code 执行 **Extensions: Install from VSIX…**，选择其中的 `.vsix` 文件。

插件已内置 Python 记录器，插件捕获无需另外安装 Python 包或克隆源码。Remote-SSH 用户把插件安装到远程工作区主机。

版本标签会通过发布工作流生成固定文件名 `callnav.vsix`。首次发布后，可使用[一键下载链接](https://github.com/Zethan06/Track_any_call/releases/latest/download/callnav.vsix)；发布前使用上面的构建产物。

也可以从源码构建同一个安装包：

```bash
git clone https://github.com/Zethan06/Track_any_call.git
cd Track_any_call
python vscode/scripts/package.py
```

最后一条命令会输出插件文件的位置，也可用 `code --install-extension /path/to/callnav.vsix` 安装。

## 使用

安装 VSIX 后，打开一个**新的 VS Code 终端**，即可使用插件内置的 `callnav` 命令，无需 pip 安装。若命令尚不可用，在命令面板执行 **CallNav: Enable Terminal Command**，再新开终端。激活原来的环境，进入项目目录，在原启动命令前加上 `callnav`：

```bash
cd /path/to/your-project
callnav bash ./launch.sh
callnav python train.py --steps 1
```

无需给每个项目编写 JSON 配置。短前缀默认把当前目录作为源码根目录，并启用支持范围内的 Ray 采集。Conda 环境、GPU、模型、数据和脚本参数仍沿用原命令或 `.sh`；可执行程序之后的 `--help`、`--version` 等参数也原样交给它。

终端命令使用该终端当前环境中的 Python，要求 Python 3.11+。插件只给新建 VS Code 终端添加命令搜索路径，不修改 shell 启动文件。Remote-SSH 用户应把插件装在远程主机，并新开远程终端。

需要指定外部源码目录或关闭 Ray 时，把录制选项放在原命令前：

```bash
CUDA_VISIBLE_DEVICES=0,1 callnav --include-root ../external-library -- bash ./launch.sh
callnav --no-ray -- python train.py --steps 1
```

每次录制都会保存到独立历史目录，保留已有运行。录制后，在 VS Code 打开同一个项目，执行 **CallNav: Select Recording…**，根据日期、原命令、状态和边数选择一次已完成运行；插件会载入所选索引并启用跳转。按住 Ctrl 点击调用位置，macOS 使用 Cmd。也可用 **CallNav: Load Latest Runtime Index** 快速载入最新索引。

也可以直接使用插件内置记录器，无需安装 CLI：右键 `.sh` 或 `.py` 文件执行 **CallNav: Record Script…**，或使用 **CallNav: Record Command…** 输入启动命令。成功后自动载入本次索引；停止时使用 **CallNav: Stop Recording**。可选设置见[插件说明](vscode/README.md#commands-and-settings)。

原有的 `callnav record --ray --root "$PWD" -- bash ./launch.sh` 仍可用；它保留旧默认值，需要显式加 `--ray`。Ray worker 仍需继承录制环境，并能访问源码和记录目录，详见[使用说明](docs/usage.md#ray-and-framework-launchers)。同步 Slime adapter 不适用于 `train_async.py`。

在 VS Code 之外的普通 shell 或 SSH 终端中使用时，在原来的 Python 环境安装一次独立 CLI 即可：

```bash
python -m pip install /path/to/callnav-0.6.10-py3-none-any.whl
# 或从源码安装：
python -m pip install -e /path/to/Track_any_call
```

记录保存在项目的 `.callnav/` 下，CallNav 不会删除旧运行目录；`latest.json` 只是便于访问最新索引的副本。建议把 `.callnav/` 加入 `.gitignore`。改过源码或换过目录后，需要重新录制。分享记录前检查一下里面的路径和启动参数。

运行记录只包含这次执行并成功采集的调用。0.6.10 会在 Python 3.12/3.13 自动选择低开销采集方式，不需要修改命令或添加配置；Python 3.11 保留兼容方式。录制仍会增加运行开销，建议先跑较短的任务；C/CUDA 内部调用不在记录范围内。

支持直接 await、创建后跨线程传递的协程、同步装饰器返回的协程、普通及异步生成器的真实首次入口，以及 `super()`、动态 `getattr()`、字典分派、工厂返回对象的方法、Thread 目标和 Task 完成回调。生成器需要实际对象与创建位置的确证，例如函数参数、直接返回或经编译指令核验的字面量元组循环。还支持 Hydra 根工厂和 `registry.get(key)(*args, **kwargs)` 的实际执行目标。跳转以目标实际执行为依据；未执行分支不会生成目标。各类调用的适用条件见[采集边界](docs/usage.md#coverage-and-limitations)。

[开发与测试](CONTRIBUTING.md) · [MIT 许可](LICENSE)
