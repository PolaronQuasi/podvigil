"""A training loop that saves checkpoints, stops cleanly when told to, and can resume.

What it shows:
- Checkpoints go into PODVIGIL_OUTPUT_DIR. Each one is written to a temporary name and then renamed,
  so a half-written file never replaces a good one.
- The runner does not tell the workload its deadline. This script gets its own time budget,
  TRAIN_SECONDS, from workload_env in the job file, and stops by itself when it is used up.
- When the job's time is up, when it is cancelled (podvigil cancel), or when the Pod's guard stops
  it, the runner sends SIGTERM to the workload and SIGKILL 10 seconds later. On SIGTERM this script
  saves a last checkpoint and exits; the runner then copies the outputs to the volume, within the
  time it has left for that.
- Resume: when the inputs hold resume/checkpoint.pt, training continues from it.

Outputs reach the network volume only when the job ends. A Pod that dies hard loses all of them,
checkpoints included. So keep each job short and chain jobs: give the last checkpoint of one job
to the next one as an input (see docs/WORKLOAD-CONTRACT.md).
"""

import json
import os
import pathlib
import signal
import time

import torch

inputs = pathlib.Path(os.environ["PODVIGIL_INPUT_DIR"])
output = pathlib.Path(os.environ["PODVIGIL_OUTPUT_DIR"])
budget = float(os.environ.get("TRAIN_SECONDS", "600"))
every = float(os.environ.get("CHECKPOINT_SECONDS", "60"))
total_steps = int(os.environ.get("TOTAL_STEPS", "200000"))

stopping = False


def on_sigterm(signum, frame):
    global stopping
    stopping = True  # finish the current step, save, exit


signal.signal(signal.SIGTERM, on_sigterm)

device = "cuda" if torch.cuda.is_available() else "cpu"
torch.manual_seed(0)
model = torch.nn.Sequential(torch.nn.Linear(64, 256), torch.nn.ReLU(), torch.nn.Linear(256, 1)).to(
    device
)
optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
target = torch.randn(64, 1, device=device)
step = 0

resume = inputs / "resume" / "checkpoint.pt"
if resume.is_file():
    state = torch.load(resume, map_location=device)
    model.load_state_dict(state["model"])
    optimizer.load_state_dict(state["optimizer"])
    step = state["step"]
    print(f"resumed from step {step}", flush=True)


def save(reason, loss):
    folder = output / "checkpoints"
    folder.mkdir(exist_ok=True)
    temporary = folder / "latest.pt.tmp"
    torch.save(
        {"model": model.state_dict(), "optimizer": optimizer.state_dict(), "step": step}, temporary
    )
    os.replace(temporary, folder / "latest.pt")  # the rename is the commit
    progress = {"step": step, "loss": loss, "reason": reason, "device": device}
    (output / "progress.json").write_text(json.dumps(progress, indent=2) + "\n", encoding="utf-8")
    print(f"checkpoint at step {step} ({reason}), loss {loss:.6f}", flush=True)


started = last_save = time.monotonic()
loss = float("nan")
while step < total_steps and not stopping and time.monotonic() - started < budget:
    batch = torch.randn(512, 64, device=device)
    optimizer.zero_grad()
    error = ((model(batch) - batch @ target) ** 2).mean()
    error.backward()
    optimizer.step()
    step += 1
    loss = error.item()
    if time.monotonic() - last_save >= every:
        save("periodic", loss)
        last_save = time.monotonic()

save("stopped by SIGTERM" if stopping else "finished", loss)
