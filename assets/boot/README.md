# Camol boot assets

`camol-camel-ascii.png` is the canonical camel artwork supplied by the project owner.
It should appear right-adjusted beside `camol-wordmark.txt` on wide terminal boot
screens.

`camol-wordmark.txt` is the raw geometric `CAMOL` wordmark. It is stored as plaintext
so Markdown cannot reinterpret its slashes and underscores. Preserve its whitespace
and render it from the upper-left; do not center it.

The distribution will eventually contain wide, medium, and compact plaintext
derivatives for terminals without an image protocol. Those files should be derived
from the canonical artwork and visually reviewed rather than generated independently.

The terminal renderer may choose a compact wordmark below the wide-layout breakpoint,
but the source asset remains the visual reference.
