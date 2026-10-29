#!/usr/bin/env bash
# Run the same seven lanes on hosted runners and act's isolated workspace volumes.
set -euo pipefail
if [[ ${1:-} != --inside ]]; then
    image=$1 lane=$2 architecture=$3
    mkdir -p .cache/native-tests/"$lane" .cache/native-tests/pip
    mounts=(--mount "type=bind,source=$PWD,target=$PWD")
    if [[ ${ACT:-} == true ]]; then
        # With host networking, hostname identifies the Docker VM, not this job.
        carrier=$(awk '$5 == "/etc/hostname" {n = split($4, p, "/"); print p[n - 1]}' /proc/self/mountinfo)
        mounts=(--volumes-from "$carrier")
    fi
    owner=${KICAD_VALIDATION_RUN_ID:-$(cat /proc/sys/kernel/random/uuid)}
    name="kicad-tests-$owner-$lane"
    receipt=".cache/native-tests/$lane/container-id"
    trap 'if [[ -s $receipt ]]; then docker rm -f "$(cat "$receipt")" >/dev/null 2>&1 || true; fi' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    docker run --rm --init --tmpfs /tmp:exec --user=0 --name "$name" --cidfile "$receipt" --platform "linux/$architecture" \
        --label "org.kicad-jlcpcb-tools.validation-run=$owner" \
        "${mounts[@]}" --workdir "$PWD" "$image" \
        bash scripts/run_test_lane.sh --inside "$lane" "$architecture"
    exit
fi
lane=$2 architecture=$3
if [[ $(id -u) == 0 ]]; then
    chown -R runner:runner "$PWD"
    exec runuser -u runner -- bash "$0" --inside "$lane" "$architecture"
fi
artifacts="$PWD/.cache/native-tests/$lane"
export PIP_CACHE_DIR="$PWD/.cache/native-tests/pip"
actual=$(dpkg --print-architecture)
printf 'lane=%s uid=%s architecture=%s expected=%s\n' "$lane" "$(id -u)" "$actual" "$architecture" | tee "$artifacts/setup.log"
[[ $(id -u) != 0 && $actual == "$architecture" ]]
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install '.[dev]'
pytest=(.venv/bin/python -m pytest -rs --junitxml="$artifacts/pytest.xml")
case "$lane" in
    ordinary-tests)
        "${pytest[@]}" -m 'not native_wx and not native_kicad' ;;
    native-wx-0)
        timeout 360s "${pytest[@]}" -n auto --maxprocesses=4 --dist=loadfile --max-worker-restart=0 \
            --require-native=wx -m 'not native_kicad and not os_input' ;;
    native-wx-1)
        timeout 360s xvfb-run -a -s '-screen 0 1920x1080x24 -noreset' bash -c '
            openbox >/dev/null 2>&1 &
            manager=$!
            trap "kill $manager 2>/dev/null || true" EXIT
            "$@"
        ' bash "${pytest[@]}" --require-native=wx -m os_input ;;
    native-kicad-*)
        major=${lane##*-}
        export KICAD_CONFIG_HOME="$artifacts/config"
        mkdir -p "$KICAD_CONFIG_HOME/$major.0"
        printf '%s\n' '{"appearance":{"color_theme":"_builtin_default"}}' > "$KICAD_CONFIG_HOME/$major.0/pcbnew.json"
        timeout 300s xvfb-run -a -s '-screen 0 1920x1080x24 -noreset' "${pytest[@]}" \
            --require-native=kicad --kicad-major="$major" -m native_kicad \
            --basetemp="$artifacts/pytest-work" 2>&1 | tee "$artifacts/pytest.log" ;;
    *) printf 'Unknown test lane: %s\n' "$lane" >&2; exit 2 ;;
esac
