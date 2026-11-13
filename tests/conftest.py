# NEW: Bug 17 - stop the test run from opening browser tabs.
# pytest loads this file before any test, so every test sees this setting.
import os

os.environ["ESCALATION_OPEN_BROWSER"] = "0"