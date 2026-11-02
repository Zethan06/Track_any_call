"""Measure recorder overhead and compare complete call graphs without extra dependencies."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]


def positive(value):
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return result


def graph(index):
    return sorted(json.dumps({key: value for key, value in edge.items() if key != 'process_ids'},
                             sort_keys=True) for edge in index['edges'])


def benchmark(work, script, source, repeats):
    samples = []
    observed = None
    for number in range(repeats):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('CALLNAV_')}
        environment['PYTHONDONTWRITEBYTECODE'] = '1'
        command = [sys.executable, str(script)]
        output = work.parent / f'{source.name if source else "plain"}-{number}-{time.time_ns()}'
        if source is not None:
            environment['PYTHONPATH'] = str(source)
            command = [sys.executable, '-m', 'callnav', '--root', str(work),
                       '--output', str(output), '--', *command]
        started = time.perf_counter()
        result = subprocess.run(command, cwd=work, env=environment,
                                text=True, capture_output=True, timeout=120)
        elapsed = time.perf_counter() - started
        if result.returncode or result.stdout != 'BENCHMARK_OK\n':
            raise RuntimeError(f'Benchmark failed: {result.stdout}\n{result.stderr}')
        if source is not None:
            index = json.loads((output / 'index.json').read_text())
            if any(process.get('errors') for process in index['processes']):
                raise RuntimeError('Recorder reported errors')
            current = graph(index)
            if observed is not None and current != observed:
                raise RuntimeError('Recorded graph changed between identical benchmark runs')
            observed = current
        samples.append(elapsed)
    return {'seconds': samples, 'median_seconds': statistics.median(samples)}, observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source', type=Path,
                        help='optional older src directory containing the callnav package')
    parser.add_argument('--repeats', type=positive, default=3)
    parser.add_argument('--functions', type=positive, default=200)
    parser.add_argument('--iterations', type=positive, default=20)
    parser.add_argument('--workload', choices=['calls', 'module', 'imports', 'opcodes'], default='calls',
                        help='repeat calls, a large module body, excluded library imports, or opcode-heavy loops')
    parser.add_argument('--output', type=Path, help='optional JSON results file')
    args = parser.parse_args()
    sources = {'current': ROOT / 'src'}
    if args.baseline_source:
        sources['baseline'] = args.baseline_source.resolve()
    for source in sources.values():
        if not (source / 'callnav' / '__main__.py').is_file():
            parser.error(f'Not a CallNav source directory: {source}')
    with tempfile.TemporaryDirectory(prefix='callnav-benchmark-') as temporary:
        work = Path(temporary) / 'workload'
        work.mkdir()
        body = '    return abs(x) + 1\n'
        if args.workload == 'opcodes':
            body = '    total = 0\n    for index in range(200):\n        total += index\n' + body
        (work / 'dense.py').write_text(
            ''.join(f'def f{number}(x):\n' + body
                    for number in range(args.functions)) + '\nfunctions = [' +
            ','.join(f'f{number}' for number in range(args.functions)) + ']\n', encoding='utf-8')
        loop = (f'for _ in range({args.iterations}):\n'
                '    for fn in dense.functions:\n        assert fn(-1) == 2\n')
        if args.workload == 'module':
            path = work / 'dense.py'
            path.write_text(path.read_text() + loop.replace('dense.functions', 'functions'),
                            encoding='utf-8')
            loop = ''
        imports = ''
        if args.workload == 'imports':
            external = work.parent / 'external'
            external.mkdir()
            (external / 'library.py').write_text(
                'def library_call(value):\n    return abs(value)\n' +
                f'for item in range({args.functions * args.iterations * 50}):\n'
                '    library_call(-item)\n', encoding='utf-8')
            imports = f'import sys\nsys.path.insert(0, {str(external)!r})\nimport library\n'
        script = work / 'run.py'
        script.write_text(imports + 'import dense\n' + loop + 'print("BENCHMARK_OK")\n',
                          encoding='utf-8')
        result = {'python': sys.version.split()[0], 'repeats': args.repeats,
                  'functions': args.functions, 'iterations': args.iterations,
                  'workload': args.workload}
        result['plain'], _ = benchmark(work, script, None, args.repeats)
        expected = None
        for name, source in sources.items():
            result[name], observed = benchmark(work, script, source, args.repeats)
            if len(observed) != args.functions:
                raise RuntimeError('Recorder missed a benchmark target')
            if any(json.loads(edge)['count'] != args.iterations for edge in observed):
                raise RuntimeError('Recorder missed or duplicated benchmark calls')
            if expected is not None and observed != expected:
                raise RuntimeError('Baseline and current call graphs differ')
            expected = observed
        result['graphs_equal'] = True
        result['edges'] = len(expected)
        result['calls'] = args.functions * args.iterations
        result['graph_sha256'] = hashlib.sha256('\n'.join(expected).encode()).hexdigest()
        content = json.dumps(result, indent=2) + '\n'
        if args.output:
            args.output.write_text(content, encoding='utf-8')
        print(content, end='')


if __name__ == '__main__':
    main()
