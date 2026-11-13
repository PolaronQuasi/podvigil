"""Fit a straight line to the points in data/, with settings from configs/.

This example shows the three kinds of inputs a job file can list (one file, a folder, a pattern)
and where they appear in the Pod: below /workspace/inputs, the folder that PODVIGIL_INPUT_DIR names.
That folder is read-only. Results go to PODVIGIL_OUTPUT_DIR, which is also the working directory.

The job file starts it as `python3 -u -m train`: the input folder is on PYTHONPATH, so this finds the
script both in the Pod and with `podvigil run-local`, which runs the job on your own computer first.
Without a GPU it uses the CPU; it needs torch either way.
"""

import csv
import json
import os
import pathlib

import torch

inputs = pathlib.Path(os.environ["PODVIGIL_INPUT_DIR"])
output = pathlib.Path(os.environ["PODVIGIL_OUTPUT_DIR"])

settings = {}
for path in sorted((inputs / "configs").glob("*.json")):  # the pattern input
    settings.update(json.loads(path.read_text(encoding="utf-8")))

xs, ys = [], []
files = sorted((inputs / "data").rglob("*.csv"))  # the folder input, with its subfolders
for path in files:
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            xs.append(float(row["x"]))
            ys.append(float(row["y"]))
print(f"read {len(xs)} points from {len(files)} files; settings {settings}", flush=True)

device = "cuda" if torch.cuda.is_available() else "cpu"
x = torch.tensor(xs, device=device).unsqueeze(1)
y = torch.tensor(ys, device=device).unsqueeze(1)
weight = torch.zeros(1, 1, device=device, requires_grad=True)
bias = torch.zeros(1, device=device, requires_grad=True)
optimizer = torch.optim.SGD([weight, bias], lr=float(settings.get("learning_rate", 0.01)))
steps = int(settings.get("steps", 2000))
for step in range(steps):
    optimizer.zero_grad()
    loss = ((x @ weight + bias - y) ** 2).mean()
    loss.backward()
    optimizer.step()
    if step % 500 == 0:
        print(f"step {step}: loss {loss.item():.6f}", flush=True)

result = {
    "device": device,
    "points": len(xs),
    "steps": steps,
    "loss": loss.item(),
    "slope": weight.item(),
    "intercept": bias.item(),
    "run_name": settings.get("run_name"),
}
(output / "metrics.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print("finished: " + json.dumps(result), flush=True)
