"""jev-stepwise-judge: a step-by-step judge for coding agents, powered by TypeSafe Jev.

Before every tool call it compares the agent's *state* (what it knows, what it
has changed in the workspace, and where it is in its goal list) with the step
it is about to take, and asks Jev a batch of narrow typed questions about it.
"""

__version__ = "0.1.0"
