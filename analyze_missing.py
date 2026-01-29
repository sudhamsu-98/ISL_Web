import csv
from collections import Counter
from pathlib import Path

MAPPING = Path("/home/antpc/Downloads/ISL_web/mapping.csv")

# These were "skipped no ref", so we need to rescan collected videos the same way you did.
# Easiest: modify build script to also write missing.csv
# But since we don't have missing saved, here's a better approach:
# Re-run build script with a tiny edit OR use this approach if you saved logs.

print("If you want the missing list, re-run build script with missing.csv output.")
