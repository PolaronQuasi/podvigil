# Examples

All plans below are offline and create no paid resources. Read [the quickstart](QUICKSTART.md)
before changing `plan` to `launch`; launching creates billed resources in your Runpod account.

<!-- prettier-ignore -->
| Example | What it demonstrates | Offline command |
| --- | --- | --- |
| [Hello GPU](../examples/hello-gpu/job.json) | A short CUDA check with a metrics file | `podvigil plan examples/hello-gpu/job.json` |
| [Script and inputs](../examples/script-with-inputs/job.json) | Declared files, folders and pattern inputs | `podvigil plan examples/script-with-inputs/job.json` |
| [Checkpoints](../examples/checkpoints/job.json) | Periodic saving and bounded shutdown | `podvigil plan examples/checkpoints/job.json` |
| [Single candidate](../jobs/smoke.example.json) | One explicit GPU and data center | `podvigil plan jobs/smoke.example.json` |
| [Candidate list](../jobs/candidates.example.json) | Ordered Secure Cloud placement choices | `podvigil plan jobs/candidates.example.json` |

Job-file budgets are configured ceilings, not current price quotes. Inspect live capacity with
`podvigil survey`, choose a trusted image, and explicitly decide whether to launch. Save outputs
beneath `PODVIGIL_OUTPUT_DIR` and keep the job's ledger until resource absence and recovery are
verified.
