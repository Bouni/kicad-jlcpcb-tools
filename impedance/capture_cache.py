"""Bound immutable native PNG ownership independently of native board lifetimes."""

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
from typing import TYPE_CHECKING, Optional

from .model import BoardSnapshot, Section, ValidationError

if TYPE_CHECKING:
    from .service import CapturedImage

MAX_OWNED_CAPTURE_BYTES = 128 * 1024 * 1024
MAX_OWNED_CAPTURES = 2048


@dataclass(frozen=True)
class CaptureKey:
    """Identify pixels, never a viewed or approved state."""

    board_identity: str
    source_digest: str
    appearance: object
    output_variant: Optional[str]
    capture_policy: int
    spacing_policy: tuple[int, int]
    kind: str
    inputs: object
    dimensions: tuple[int, int]
    viewport_nm: Optional[tuple[float, float, float, float]]


@dataclass(frozen=True)
class _OwnedPNG:
    """Retain primitive immutable data without a renderer or native board."""

    data: bytes
    sha256: str
    dimensions: tuple[int, int]
    viewport_nm: Optional[tuple[float, float, float, float]]
    provenance_sha256: str
    original: Optional["_OwnedPNG"] = None

    @classmethod
    def capture(cls, image: "CapturedImage") -> "_OwnedPNG":
        """Copy exact ownership metadata for later integrity validation."""
        original = cls.capture(image.original) if image.original is not None else None
        dimensions = (image.width, image.height)
        return cls(
            image.data,
            image.sha256,
            dimensions,
            image.viewport_nm,
            cls.provenance(image.sha256, dimensions, image.viewport_nm, original),
            original,
        )

    @staticmethod
    def provenance(
        png_sha256: str,
        dimensions: tuple[int, int],
        viewport_nm: Optional[tuple[float, float, float, float]],
        original: Optional["_OwnedPNG"],
    ) -> str:
        """Seal the exact physical transform and comparison metadata with the PNG."""
        payload = json.dumps(
            (
                png_sha256,
                dimensions,
                viewport_nm,
                original.provenance_sha256 if original else None,
            ),
            allow_nan=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("ascii")).hexdigest()

    @property
    def byte_size(self) -> int:
        """Include the independently rendered HTML comparison in the limit."""
        return len(self.data) + (self.original.byte_size if self.original else 0)

    def load(self) -> "CapturedImage":
        """Recheck bytes, dimensions and transforms instead of trusting cache state."""
        from .service import CapturedImage

        if self.provenance_sha256 != self.provenance(
            self.sha256, self.dimensions, self.viewport_nm, self.original
        ):
            raise ValidationError(
                "The owned impedance PNG transform changed in memory."
            )
        image = CapturedImage.from_bytes(
            self.data,
            viewport_nm=self.viewport_nm,
            original=self.original.load() if self.original is not None else None,
        )
        if (
            image.sha256 != self.sha256
            or (image.width, image.height) != self.dimensions
        ):
            raise ValidationError("The owned impedance PNG changed in memory.")
        return image


class OwnedCaptureCache:
    """A bounded frame-owned pixel cache that cannot provide user consent."""

    def __init__(
        self,
        max_bytes: int = MAX_OWNED_CAPTURE_BYTES,
        max_entries: int = MAX_OWNED_CAPTURES,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_entries = max_entries
        self.byte_size = 0
        self._images: OrderedDict[CaptureKey, _OwnedPNG] = OrderedDict()
        self._image_sizes: dict[CaptureKey, int] = {}
        self._source: Optional[BoardSnapshot] = None
        self._source_digest = ""
        self.hits = 0
        self.misses = 0

    def source_digest(self, snapshot: BoardSnapshot) -> str:
        """Memoize pure source serialization without retaining native objects."""
        from .review_tracking import source_fingerprint

        if snapshot is not self._source and snapshot != self._source:
            digest = source_fingerprint(snapshot)
            self._source_digest = digest
        self._source = snapshot
        return self._source_digest

    def discard(self, key: Optional[CaptureKey]) -> None:
        """Explicit refresh removes the old pixels even if replacement fails."""
        if key is None:
            return
        previous = self._images.pop(key, None)
        if previous is not None:
            self.byte_size -= self._image_sizes.pop(key)

    def get(self, key: CaptureKey) -> Optional["CapturedImage"]:
        """Return current owned pixels; corrupt entries become cold misses."""
        owned = self._images.get(key)
        if owned is not None:
            try:
                from .spacing_review import SpacingReviewEntry

                if not (
                    key.kind == "section"
                    and isinstance(key.inputs, Section)
                    or key.kind == "spacing"
                    and isinstance(key.inputs, SpacingReviewEntry)
                ):
                    raise ValidationError("The owned PNG has unrelated image inputs.")
                image = owned.load()
                if (image.width, image.height) != key.dimensions:
                    raise ValidationError("The requested PNG dimensions changed.")
                if key.kind == "spacing":
                    from .service import validate_spacing_original

                    if image.viewport_nm is not None:
                        raise ValidationError("The spacing PNG provenance changed.")
                    if image.original is not None:
                        validate_spacing_original(image.original, key.inputs)
                elif image.viewport_nm != key.viewport_nm:
                    raise ValidationError("The section PNG viewport changed.")
            except (TypeError, ValueError, ValidationError):
                self.discard(key)
            else:
                self._images.move_to_end(key)
                self.hits += 1
                return image
        self.misses += 1
        return None

    def put(self, key: CaptureKey, image: "CapturedImage") -> None:
        """Own validated bytes only; oversized images remain a rendering fallback."""
        owned = _OwnedPNG.capture(image)
        # Detect malformed/corrupt objects before taking ownership.
        owned.load()
        self.discard(key)
        if owned.byte_size > self.max_bytes or self.max_entries < 1:
            return
        while self._images and (
            self.byte_size + owned.byte_size > self.max_bytes
            or len(self._images) >= self.max_entries
        ):
            oldest = next(iter(self._images))
            self.discard(oldest)
        self._images[key] = owned
        self._image_sizes[key] = owned.byte_size
        self.byte_size += owned.byte_size


def capture_key(
    cache: OwnedCaptureCache,
    board_identity: str,
    snapshot: BoardSnapshot,
    appearance: object,
    output_variant: Optional[str],
    item: object,
    *,
    dimensions: tuple[int, int] = (800, 420),
) -> CaptureKey:
    """Bind the complete immutable source and image inputs to current policies."""
    from .matching import CAPTURE_POLICY_REVISION
    from .spacing_checks import SPACING_CHECK_POLICY_REVISION
    from .spacing_review import SPACING_RENDER_POLICY_REVISION

    spacing = hasattr(item, "view_id")
    viewport = getattr(item, "viewport_nm", None)
    if not spacing:
        from .render import section_viewport

        frame = section_viewport(item, *dimensions, snapshot.board_bounds)
        viewport = (frame.left, frame.top, frame.width, frame.height)
    return CaptureKey(
        board_identity,
        cache.source_digest(snapshot),
        appearance,
        output_variant,
        CAPTURE_POLICY_REVISION,
        (SPACING_CHECK_POLICY_REVISION, SPACING_RENDER_POLICY_REVISION)
        if spacing
        else (0, 0),
        "spacing" if spacing else "section",
        item,
        dimensions,
        viewport,
    )
