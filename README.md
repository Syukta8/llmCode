# FX505DD llama.cpp CUDA + HauhauCS Qwen3.5 + OpenCode V2

Tested for Bash syntax, **not executed against the user's laptop**.

Hardware: Ubuntu 24.04, NVIDIA GTX 1050 3 GiB, driver 580.196.07, GCC 13.3, system RAM 16 GiB.

## Run in order

```bash
chmod +x ./*.sh
./01-install-cuda.sh
./02-build-llama.sh
./03-download-models.sh 4b
./04-serve-model.sh 4b 8 8192
```

In a second terminal (install coding CLI of your choice):

```bash
./05-validate-api.sh 4b
./06-install-opencode-cli.sh   # OpenCode CLI (terminal)
./07-install-aider.sh          # Aider CLI (terminal)
```

### Launch via Desktop App (One-Click)

Launch **llmCode** directly from the Ubuntu Application Menu (press `Super` key and search `llmCode`), or run:

```bash
./bin/launch-app.sh
```

To stop the dashboard and llama-server at any time:

```bash
./bin/stop-app.sh
```

Or run via terminal:

```bash
cd src/LLMServer
./run.sh
```

Open browser at `http://127.0.0.1:7860`:
1. Pick downloaded model (4b or 9b).
2. Set target project directory.
3. Click **LAUNCH AIDER** or **LAUNCH OPENCODE**.
   - Dashboard auto-starts `llama-server` (if offline).
   - Spawns the chosen CLI in a terminal window connected to local `http://127.0.0.1:8080/v1`.

For 9B, stop 4B server first, run `./03-download-models.sh 9b`, then select 9B in dashboard with 4 GPU layers and 4096 context tokens.

## Safeguards

* The CUDA installer previews the APT transaction and requires confirmation. It uses a version-pinned toolkit package, not the `cuda` driver meta-package.
* The build script does not automatically update an existing llama.cpp checkout.
* The model server binds to localhost only.
* OpenCode project configuration asks before edits or shell actions, but these approvals are **not an OS-level sandbox**.
* Files in `.env` are denied by name. Arbitrary shell commands can still access sensitive data after approval.
* Before installing, read the scripts and check any existing system CUDA installations for conflicts.
