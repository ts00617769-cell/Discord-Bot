with open("cogs/exp_tracker.py", "r") as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if "self.ALERT_CHANNEL_IDS =" in line:
        lines[i] = '        self.ALERT_CHANNEL_IDS = [int(x.strip()) for x in os.getenv("EXP_ALERT_CHANNEL_ID", "").split(",") if x.strip() and x.strip().isdigit()]\n'
    elif "self.TRANSFER_ALERT_CHANNEL_IDS =" in line:
        lines[i] = '        self.TRANSFER_ALERT_CHANNEL_IDS = [int(x.strip()) for x in os.getenv("TRANSFER_ALERT_CHANNEL_ID", "").split(",") if x.strip() and x.strip().isdigit()]\n'

with open("cogs/exp_tracker.py", "w") as f:
    f.writelines(lines)
