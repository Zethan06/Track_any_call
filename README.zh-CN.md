# CallNav

**运行一次 Python 程序，再在 VS Code 源码里 Ctrl+点击，跳到本次真实执行的实现。**

CallNav 适合分析注册表、动态选择的子类、装饰器、`functools.partial` 和支持范围内的 Ray Actor 调用。直接使用 VS Code 原来的 `.py` 编辑器与定义跳转界面。

[English](README.md) · [详细使用说明](docs/usage.md) · [实现原理](docs/architecture.md) · [参与开发](CONTRIBUTING.md)

这是早期的开源项目，包含无需额外运行依赖的 Python 采集器和无需 npm 依赖的 VS Code 扩展。记录的是执行过的 Python 调用关系，不是函数值或完整执行回放。

## 安装并跑通样例

采集器及所有需要采集的 Python 进程均要求 Python **3.11+**，扩展要求 VS Code **1.85+**。自己的项目仍使用原有依赖；内置样例不需要 GPU、Ray 或 ML 库。以下命令使用 POSIX shell。

取得源码后，在 CallNav 仓库根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

callnav --version
callnav record --root "$PWD" -- bash examples/mini_pipeline/launch.sh
python vscode/scripts/package.py
```

最后一条命令输出 VSIX 文件路径。在 VS Code 命令面板执行 **Extensions: Install from VSIX…**，选中该文件。安装了 `code` 命令时，也可以：

```bash
callnav_vsix="$(python vscode/scripts/package.py)"
code --install-extension "$callnav_vsix"
```

以上从本地源码安装，不依赖 PyPI 或 Marketplace 已发布的包。

在 VS Code 打开 CallNav 仓库：

1. 命令面板执行 **CallNav: Load Latest Runtime Index**。
2. 执行 **CallNav: Enable Runtime Navigation**。
3. 打开 [examples/mini_pipeline/train.py](examples/mini_pipeline/train.py)，Ctrl+点击 `return reward_function(text)` 的 `reward_function`，可以选择本次执行过的两个 reward 实现。
4. 点击 `await generate_function(...)` 的 `generate_function`，跳到实际选用的异步插件。

macOS 使用 Cmd+点击，具体修饰键以 VS Code 设置为准；也支持“转到定义”。样例在 CPU 上跑通 rollout → reward → 子进程训练 → 权重同步 → 清理资源。

## 记录自己的启动脚本

先激活你平时运行项目的环境，从 CallNav 源码目录安装采集器，再到自己项目的工作目录执行：

```bash
# 替换为你的 CallNav 源码目录。
python -m pip install /path/to/callnav

cd /path/to/your-project
callnav record --root "$PWD" -- bash ./launch.sh --your-option value
```

`--` 后面完整保留原启动命令与参数。CallNav 不改变当前工作目录，保留原命令的终端输出与退出状态。也可以直接录制 Python 入口：

```bash
callnav record --root "$PWD" -- python main.py --config config.yaml
callnav record --root "$PWD" -- python -m your_package
```

用框架原有参数控制较小的代表性运行，覆盖需要分析的初始化、rollout、train 等阶段。任意框架没有统一的“一步训练”边界，CallNav 不会自动替你缩短任务。

在 VS Code 打开**自己的项目目录**，执行前面的两个 CallNav 命令即可。无需配置我们服务器上的路径；默认索引就是你指定 `--root` 目录内的 `.callnav/latest.json`。

使用 Remote-SSH 时，在远程机器录制，通过 Remote-SSH 打开同一份源码，并把 VSIX 安装到远程 workspace。

## 外部依赖与 Ray

调用方与目标都需要位于配置的源码目录中。要跳入外部依赖，可重复添加 `--include-root`：

```bash
callnav record --root "$PWD" \
  --include-root ../external-library \
  -- bash ./launch.sh
```

需要关联 Ray Actor 的实际方法与构造器时，加 `--ray`：

```bash
callnav record --ray --root "$PWD" -- python ray_main.py
```

例如，`runner.run.remote(config)` 可以跳到 worker 本次实际执行的 `run`。对于通过 PATH 调用、等待任务完成的 `ray job submit [options] -- command`，`--ray` 会把采集环境合并进 job 的 JSON runtime environment。提交方与 worker 都必须被采集，源码与输出目录须以相同路径可访问。支持的启动形式和框架步数适配见 [使用说明](docs/usage.md#ray-and-framework-launchers)。

## 使用边界

- 只有本次执行过、且支持定位的调用点才有运行时目标；观察到多个实现就显示多个候选。
- 文件保持普通 **Python** 模式，Pylance 等静态服务继续工作。VS Code 可能把静态定义与运行时目标一起列出来。
- 源码改动或未保存时，CallNav 会拒绝相应的运行时跳转；恢复为记录时的源码或重新录制。
- 记录使用你自己 checkout 的路径。暂不支持把旧记录搬到另一个路径或机器后直接使用，应在新位置重新录制。
- 采集会增加运行开销；C/CUDA/JIT 内部、HTTP 因果关系和未被采集的进程不在调用图内。详见 [覆盖范围](docs/usage.md#coverage-and-limitations)。
- 记录包含源码路径与启动/进程参数。分享前请检查，命令参数可能包含凭据。

每次运行生成独立的 `.callnav/<run-id>/` 目录。通过 **CallNav: Load Runtime Index…** 选择历史 `index.json`。建议把 `.callnav/` 加入自己项目的 `.gitignore`。

## 开发

```bash
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -v
node --test vscode/test/unit.test.js
python vscode/scripts/package.py
```

开发流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，扩展宿主测试见 [vscode/README.md](vscode/README.md)。项目采用 [MIT 许可](LICENSE)。
