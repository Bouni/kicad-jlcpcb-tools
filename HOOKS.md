# Generation hooks

This plugin supports optional hook scripts that run around fabrication generation.

## Hook behavior

- `pre` hook runs before fabrication files are generated.
- If the `pre` hook exits nonzero (or times out), the plugin shows a dialog with **Continue** / **Cancel**.
  - **Cancel** stops generation.
  - **Continue** keeps going.
- `post` hook runs only after successful generation of Gerber ZIP, BOM, and CPL files.

## Configure hooks

Open **Settings** and set:

- **Pre-generate hook script**
- **Post-generate hook script**
- **Hook timeout (seconds)**

Leave script paths empty to disable hooks.

## Environment variables

Both hooks receive these environment variables:

- `JLCPCB_HOOK_STAGE` (`pre` or `post`)
- `JLCPCB_BOARD_PATH`
- `JLCPCB_PROJECT_DIR`
- `JLCPCB_OUTPUT_DIR`
- `JLCPCB_GERBER_DIR`
- `JLCPCB_GENERATION_COUNT`
- `JLCPCB_PLACEHOLDER_COUNT`
- `JLCPCB_ARTIFACT_GERBER_ZIP`
- `JLCPCB_ARTIFACT_BOM_CSV`
- `JLCPCB_ARTIFACT_CPL_CSV`

Notes:

- `JLCPCB_GENERATION_COUNT` is project-local and increments after each successful generation.
- For the `pre` hook, artifact variables point to the expected output paths.
- For the `post` hook, artifact variables point to files that were just generated.

## Script expectations

- Use an executable script file path (for example, `#!/bin/sh` with executable bit set on macOS/Linux).
- Hooks are run without a shell (`subprocess.run(..., shell=False)`).
- Any stdout/stderr is logged in the plugin log output.

## Example

```sh
#!/bin/sh
set -eu

echo "stage: $JLCPCB_HOOK_STAGE"
echo "board: $JLCPCB_BOARD_PATH"
echo "count: $JLCPCB_GENERATION_COUNT"

if [ "$JLCPCB_HOOK_STAGE" = "post" ]; then
  echo "zip: $JLCPCB_ARTIFACT_GERBER_ZIP"
fi
```

## Example: Git checkpoint post-hook

An executable example script is included at:

- `scripts/example_post_generate_git_checkpoint.sh`

It runs on `post` hooks and performs:

1. `git add -A .`
2. `git commit -m "Generation checkpoint: <project name> <YYYY>-<MM>-<DD>-<version>"`
3. create a lightweight tag for the same checkpoint
4. `git push origin HEAD`
5. `git push origin <tag>`
