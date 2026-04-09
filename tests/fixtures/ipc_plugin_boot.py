"""Standalone IPC plugin bootstrap simulator fixture.

This fixture script is intentionally dependency-light and can be used to model
how the IPC plugin entrypoint is expected to initialize in tests:

1. Confirm `KICAD_API_SOCKET` is present.
2. Construct an IPC client and verify availability.
3. Ask provider for IPC-backed adapters.
4. Launch a UI entrypoint with those adapters.
"""


def run_bootstrap_simulation(env, client_factory, provider_factory, window_factory):
    """Simulate IPC plugin startup flow and return execution details."""
    socket_path = env.get("KICAD_API_SOCKET")
    if not socket_path:
        return {"exit_code": 1, "reason": "missing_socket"}

    client = client_factory(socket_path=socket_path)
    if not client.is_available():
        return {"exit_code": 1, "reason": "ipc_unavailable"}

    provider = provider_factory()
    adapter_set = provider.create_adapter_set(
        launch_context="ipc",
        ipc_client=client,
    )

    window = window_factory(None, adapter_set=adapter_set)
    window.Center()
    window.Show()

    return {
        "exit_code": 0,
        "socket_path": socket_path,
        "adapter_set": adapter_set,
    }
