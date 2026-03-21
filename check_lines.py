import sys

# Check strategy_c.py DCA block context (lines 905-920)
f = open("strategy_c.py", encoding="utf-8")
lines = f.readlines()
f.close()
print("=== strategy_c.py DCA block (905-920) ===")
for i in range(904, 920):
    sys.stdout.write(f"{i + 1}: {lines[i]}")

print("\n=== strategy_c.py _force_open_balance_slot start (619-622) ===")
for i in range(618, 623):
    sys.stdout.write(f"{i + 1}: {lines[i]}")

print("\n=== strategy_c.py _execute_hedge_pipeline start (1048-1068) ===")
for i in range(1047, 1068):
    sys.stdout.write(f"{i + 1}: {lines[i]}")

# Check main.py exec_price block (352-395)
f2 = open("main.py", encoding="utf-8")
lines2 = f2.readlines()
f2.close()
print("\n=== main.py exec_price block (352-395) ===")
for i in range(351, 395):
    sys.stdout.write(f"{i + 1}: {lines2[i]}")
