# shipd-agent

Playwright automation for the [Shipd.ai](https://shipd.ai) Olympus/Mars review workflow.
It signs you in, clocks in, reserves a submission, clones it, runs an autonomous
code review against `shipd-rubric.md`, and optionally submits feedback on Shipd.

## Unattended runs

Long batch runs should be started in the background so a closed terminal or accidental
keypress does not stop them:

```bash
nohup ./run.sh > run.log 2>&1 &
```

On macOS, prevent the machine from sleeping while a batch is running:

```bash
caffeinate -is ./run.sh
```

`Ctrl+C` still stops the run gracefully. `Ctrl+Z` (SIGTSTP) is ignored by the batch
runner so an accidental suspend cannot freeze an unattended session.
 