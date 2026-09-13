"""Strict modal-loop and caller-owned destruction for headless dialog tests."""

from collections.abc import Callable
from typing import Optional


class ModalDialog:
    """Keep EndModal, native exit callbacks, and destruction as distinct steps."""

    instances: list["ModalDialog"]
    quit_dialog: Callable[[], None]
    error: Optional[RuntimeError] = None

    def __enter__(self) -> "ModalDialog":
        """Start a fresh caller-owned lifetime for the constructed dialog."""
        self.inside_native_loop = False
        self.destroyed = False
        self.result: Optional[int] = None
        self.lifecycle: list[str] = []
        self.instances.append(self)
        return self

    def __exit__(self, *_error: object) -> None:
        """Destroy only after the modal call returns or raises."""
        self.Destroy()

    def ShowModal(self) -> int:
        """Run the production close handler before simulating native exit hooks."""
        assert not self.destroyed
        self.inside_native_loop = True
        self.lifecycle.append("show")
        try:
            self.quit_dialog()
            assert not self.destroyed, "Native modal exit still needs the dialog"
            assert self.result is not None, "The close handler must call EndModal"
            self.lifecycle.append("exit hook")
            if self.error:
                raise self.error
            return self.result
        finally:
            self.inside_native_loop = False

    def EndModal(self, result: int) -> None:
        """Record modal completion while the native loop still owns the window."""
        assert not self.destroyed and self.inside_native_loop
        assert self.result is None, "EndModal must run exactly once"
        self.result = result
        self.lifecycle.append("end modal")

    def Destroy(self) -> None:
        """Reject premature or repeated destruction."""
        assert not self.inside_native_loop, "Destroy must follow ShowModal return"
        assert not self.destroyed, "The caller must destroy its child exactly once"
        self.destroyed = True
        self.lifecycle.append("destroy")
