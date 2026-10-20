"""An async implementation selected by import path, like a rollout plugin."""

import asyncio


async def generate(prompt, state):
    # Suspension makes it possible to check that resumes are not extra calls.
    await asyncio.sleep(0)
    return f"{prompt} weight={state['weight']} answer"
