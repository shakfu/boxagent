"""PrimeIntellect's `prime-agent`, which is pi's CLI in another build.

The release tarball declares `bin: prime-agent` and depends on
`@earendil-works/pi-agent-core`, `-ai` and `-tui`, so the flags, the JSON
stream and the `models.json` provider block are pi's. What differs is the
command, the configuration directory, and where the build comes from: a
checksummed release tarball rather than an npm package.

A subclass rather than a copy: the day the two forks diverge in the stream,
one reader has to change and this file says which handler owns the difference.
"""

from __future__ import annotations

from sanduk.agent import RESOURCES
from sanduk.agents.pi import Pi


class Prime(Pi):
    name = "prime"
    image = "sanduk-prime:latest"
    containerfile = RESOURCES / "Containerfile.prime"
    key_env = "PRIME_RELAY_KEY"
    base_url_env = "PRIME_RELAY_BASE_URL"
    # 0.9.4 has no --no-approve, so a `.prime/agent/settings.json` in the
    # mounted directory is read. That steers the run; it does not widen the
    # box, which is the container and the relay either way.
    trust_flags = ()

    def key_reference(self) -> str:
        """A bare variable name, where pi takes `$NAME`.

        Measured against 0.9.4 with a stub upstream: `$PRIME_RELAY_KEY` arrived
        as the literal string in the Authorization header, `PRIME_RELAY_KEY`
        arrived as the variable's value, and omitting the field sent the
        request to api.openai.com under `OPENAI_API_KEY` instead of to the
        relay.
        """
        return self.key_env
