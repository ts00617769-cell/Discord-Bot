import re

with open("cogs/exp_tracker.py", "r") as f:
    code = f.read()

# Let's fix the validation ordering in toggle_alerts as suggested by the reviewer.
# Find the toggle_alerts block and edit it safely using regular string replacement.
