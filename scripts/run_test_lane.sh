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
    options=(--init --tmpfs /tmp:exec --user=0)
    if [[ -z ${KICAD_VALIDATION_RUN_ID:-} ]]; then
        options+=(--rm)
    fi
    trap 'status=$?
    if [[ -s $receipt ]]; then
        docker stop --time 15 "$(cat "$receipt")" >/dev/null 2>&1 || true
        if [[ -z ${KICAD_VALIDATION_RUN_ID:-} || $status == 0 ]]; then
            docker rm -f "$(cat "$receipt")" >/dev/null 2>&1 || true
        fi
    fi
    exit "$status"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    docker run "${options[@]}" --name "$name" --cidfile "$receipt" --platform "linux/$architecture" \
        --label "org.kicad-jlcpcb-tools.validation-run=$owner" \
        --label "org.kicad-jlcpcb-tools.validation-lane=$lane" \
        --env KICAD_CLI --env KICAD_CAPTURE_PNG --env KICAD_IMPEDANCE_NATIVE_BUDGET_PROFILE \
        --env NATIVE_GENERATE_BENCHMARK --env NATIVE_SMOKE_BENCHMARK \
        --env NATIVE_SOURCE_SNAPSHOT_PROBE --env NATIVE_GENERATE_RUNS \
        "${mounts[@]}" --workdir "$PWD" "$image" \
        bash scripts/run_test_lane.sh --inside "$lane" "$architecture"
    exit
fi
lane=$2 architecture=$3
if [[ $(id -u) == 0 ]]; then
    chown -R runner:runner "$PWD"
    # Forward container signals directly to the shell that retains native evidence.
    exec setpriv --reuid=runner --regid=runner --init-groups \
        env HOME=/home/runner USER=runner LOGNAME=runner SHELL=/bin/bash \
        bash "$0" --inside "$lane" "$architecture"
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
        cancel_wx() {
            trap '' INT TERM
            kill -TERM "$active" 2>/dev/null || true
            wait "$active" 2>/dev/null || true
            exit "$1"
        }
        timeout 900s .venv/bin/python -m scripts.run_wx_tests --artifacts "$artifacts" &
        active=$!
        trap 'cancel_wx 130' INT
        trap 'cancel_wx 143' TERM
        wait "$active" ;;
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
        if [[ $major != 10 ]]; then
            timeout 900s xvfb-run -a -s '-screen 0 1920x1080x24 -noreset' "${pytest[@]}" \
                --require-native=kicad --kicad-major="$major" -m 'native_kicad and not kicad10_impedance' \
                --basetemp="$artifacts/pytest-work" 2>&1 | tee "$artifacts/pytest.log"
        else
            generate=${NATIVE_GENERATE_BENCHMARK:-0}
            smoke=${NATIVE_SMOKE_BENCHMARK:-0}
            probe=${NATIVE_SOURCE_SNAPSHOT_PROBE:-0}
            if (( generate + smoke + probe > 1 )); then
                printf 'Select only one manual native profile\n' >&2
                exit 2
            fi
            # The smoke rejects checkout paths; retain its complete external output.
            scratch=$(mktemp -d /tmp/native-impedance.XXXXXXXX)
            active= logger=
            retain_native_artifacts() {
                status=$?
                trap '' INT TERM
                if [[ -n $active ]]; then
                    kill -TERM "$active" 2>/dev/null || true
                    wait "$active" 2>/dev/null || true
                fi
                exec 3>&-
                if [[ -n $logger ]]; then
                    wait "$logger" || true
                fi
                rm -f "$scratch/native-output.pipe"
                cp -a "$scratch/." "$artifacts/" || status=1
                rm -rf "$scratch"
                exit "$status"
            }
            run_logged() {
                local log=$1 command_status=0 logger_status=0
                shift
                mkfifo "$scratch/native-output.pipe"
                # Hold the pipe open until the native writer exits, then drain tee.
                exec 3<> "$scratch/native-output.pipe"
                tee "$log" < "$scratch/native-output.pipe" 3>&- &
                logger=$!
                "$@" >&3 2>&1 3>&- &
                active=$!
                wait "$active" || command_status=$?
                active=
                exec 3>&-
                wait "$logger" || logger_status=$?
                logger=
                rm "$scratch/native-output.pipe"
                if (( logger_status != 0 )); then
                    return "$logger_status"
                fi
                return "$command_status"
            }
            trap retain_native_artifacts EXIT
            trap 'exit 130' INT
            trap 'exit 143' TERM
            if (( probe )); then
                proof_status=0
                run_logged "$scratch/source-pytest.log" timeout 300s xvfb-run -a -s '-screen 0 1920x1080x24 -noreset' \
                    .venv/bin/python -m pytest -rs --require-native=kicad --kicad-major=10 -m native_kicad \
                    tests/test_impedance_source_evidence_native.py --basetemp="$scratch/source-pytest-work" \
                    --junitxml="$scratch/source-pytest.xml" || proof_status=$?
                probe_status=0
                run_logged "$scratch/source-probe.log" timeout 300s xvfb-run -a -s '-screen 0 1920x1080x24 -noreset' \
                    .venv/bin/python scripts/native_impedance_source_probe.py \
                    --output "$scratch/source-probe" --samples 65 || probe_status=$?
                (( proof_status == 0 && probe_status == 0 ))
            elif (( generate || smoke )); then
                arguments=(.venv/bin/python scripts/linux_native_impedance_smoke.py
                    --output "$scratch/impedance" --timeout 1800)
                if (( generate )); then
                    arguments+=(--generate-only --generate-runs "${NATIVE_GENERATE_RUNS:-1}" --generate-budget-seconds 60)
                fi
                run_logged "$scratch/impedance.log" timeout 3000s xvfb-run -a -s '-screen 0 1920x1080x24 -noreset' bash -eu -c '
                    artifacts=$1
                    shift
                    openbox >"$artifacts/openbox.log" 2>&1 &
                    manager=$!
                    trap "kill $manager 2>/dev/null || true" EXIT
                    for attempt in {1..50}; do
                        kill -0 "$manager"
                        if xprop -root _NET_SUPPORTING_WM_CHECK | grep -q "window id #"; then
                            "$@"
                            exit $?
                        fi
                        sleep 0.1
                    done
                    printf "Openbox did not become ready\\n" >&2
                    exit 1
                ' native-impedance "$scratch" "${arguments[@]}"
            else
                run_logged "$scratch/native-runner.log" timeout 3000s \
                    .venv/bin/python scripts/native_kicad_runner.py --artifacts "$scratch"
            fi
        fi ;;
    *) printf 'Unknown test lane: %s\n' "$lane" >&2; exit 2 ;;
esac
