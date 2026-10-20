"""A complete tiny rollout/reward/train/sync lifecycle for navigation tests.

This is a CPU fixture, not a Slime training replacement. Python training runs
in a child process; rollout runs in a thread. Both inherit opt-in collection.
"""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys

from registry import load_function


async def generate_sample(state):
    generate_function = load_function("generation.generate")
    # Only this inline await is attributed to its actual async implementation.
    return await generate_function("example prompt", state)


def score(text, function_path):
    reward_function = load_function(function_path)
    # Ctrl+click reward_function below: this callsite sees TWO implementations.
    return reward_function(text)


def rollout(state):
    generated = asyncio.run(generate_sample(state))
    return [score(generated, path) for path in ("rewards.length_reward", "rewards.exact_reward")]


def train_in_subprocess(state, rewards):
    command = [sys.executable, str(Path(__file__).with_name("train_worker.py"))]
    command.append(json.dumps({"state": state, "rewards": rewards}))
    return json.loads(subprocess.check_output(command, text=True))


def update_weights(state):
    print(f"weight_sync weight={state['weight']}", flush=True)


def dispose():
    print("dispose", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1)
    args = parser.parse_args()
    state = {"weight": 0.0}
    with ThreadPoolExecutor(max_workers=1) as executor:
        try:
            for iteration in range(args.iterations):
                print(f"rollout {iteration}", flush=True)
                rewards = executor.submit(rollout, state).result()
                print(f"train {iteration}", flush=True)
                state = train_in_subprocess(state, rewards)
                update_weights(state)
        finally:
            dispose()


if __name__ == "__main__":
    main()
