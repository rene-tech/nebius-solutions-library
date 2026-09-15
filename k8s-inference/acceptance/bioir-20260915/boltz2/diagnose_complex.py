"""Preserve suppressed upstream stdout for the failed exact complex request."""
import json
import server

original = server.subprocess.run

def traced(command, **kwargs):
    completed = original(command, **kwargs)
    print(completed.stdout, flush=True)
    print(completed.stderr, flush=True)
    return completed

server.subprocess.run = traced
with open("/models/fixtures/7sfy.json") as file:
    server._predict(server.PredictRequest(**json.load(file)))
