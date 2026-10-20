"""Two runtime-selectable implementations of the same reward interface."""


def length_reward(text):
    return float(len(text.split()))


def exact_reward(text):
    return float(text.endswith("answer"))
