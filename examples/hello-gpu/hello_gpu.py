"""The smallest useful GPU job: check that CUDA works, multiply two matrices, save the result.

The runner starts this script inside the Pod as an unprivileged user. It reads no inputs and
writes one file, metrics.json, into the folder that PODVIGIL_OUTPUT_DIR names. The job file lists
metrics.json under required_artifacts, so a run that does not write it does not count as a success.
Everything the script prints goes to train.log, which you get back with the results.
"""

import json
import os
import pathlib
import time

import torch

output = pathlib.Path(os.environ["PODVIGIL_OUTPUT_DIR"])
if not torch.cuda.is_available():
    raise SystemExit("CUDA is not available in this container")

started = time.perf_counter()
x = torch.randn(4096, 4096, device="cuda")
y = x @ x
torch.cuda.synchronize()
metrics = {
    "gpu": torch.cuda.get_device_name(0),
    "torch": torch.__version__,
    "matrix_size": 4096,
    "mean": y.mean().item(),
    "seconds": round(time.perf_counter() - started, 3),
}
(output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
print("hello-gpu finished: " + json.dumps(metrics), flush=True)
