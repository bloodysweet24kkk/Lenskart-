#!/usr/bin/env python3
"""
Entry point for Render Free Web Service.

1. Starts the keep-alive HTTP server on $PORT (Render requirement) + self-pinger.
2. Runs the Telegram bot with auto-restart loop.
"""

import time
import os
import importlib.util

# Start keep-alive server + self-pinger first
try:
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("app", os.path.join(here, "app.py"))
    app_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app_mod)
    app_mod.start_keepalive_server()
except Exception as e:
    print(f"Warning: keep-alive server failed: {e}", flush=True)

time.sleep(1)

# Load and run the bot
here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("lenskart_bot", os.path.join(here, "lenskart_bot.py"))
bot_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot_module)

print("Starting Telegram bot...", flush=True)

while True:
    try:
        bot_module.main()
    except KeyboardInterrupt:
        print("\nBot stopped by user.", flush=True)
        break
    except Exception as e:
        print(f"\nBot crashed: {e}", flush=True)
        print("Restarting in 5 seconds...", flush=True)
        time.sleep(5)
