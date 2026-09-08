"""Execution: dispatches collected tests and reports each one's outcome.

`run.py` holds the concurrent asyncio dispatch loop, the per-test setup -> call -> teardown
envelope, and the outcome/exit-code types the rest of voci reads back.
"""
