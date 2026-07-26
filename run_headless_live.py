"""Run apartment_bot.py with --headless --live, for IDE Run button (no launch config needed)."""
import runpy
import sys

sys.argv = [sys.argv[0], "--headless", "--live"]
runpy.run_path("apartment_bot.py", run_name="__main__")
