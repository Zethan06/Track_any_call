"""A separate Python process, without any ML dependencies."""

import json
import sys


def sgd_update(state, rewards):
    state["weight"] += sum(rewards) / len(rewards)
    return state


def train(state, rewards):
    return sgd_update(state, rewards)


if __name__ == "__main__":
    request = json.loads(sys.argv[1])
    print(json.dumps(train(request["state"], request["rewards"])))
