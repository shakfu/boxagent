# Agent handlers

sanduk runs one agent CLI inside the container and reads its JSON stream. What
that CLI is, how it is driven, and how its output is parsed live in a handler.
Two ship: `claude` and `hax`. A third is a class you write, in your own package.

## What a handler answers

| | |
|-|-|
| `name` | the `--agent` value and the registry key |
| `image`, `containerfile` | which image carries the program |
| `protocols` | wire protocols the agent speaks, from `sanduk.providers` |
| `argv()` | flags appended after the image in the container command |
| `wire()` | the variables the agent reads its endpoint and credential from |
| `reader()` | a fresh `Reader` per run, which consumes the JSON stream |

The runner knows none of it. Adding an agent touches no file in `sanduk`.

## A minimal handler

```python
from sanduk import Agent, Outcome, Reader, Wiring
from sanduk.providers import OPENAI_CHAT


class MyReader(Reader):
    def __init__(self):
        self.result = None

    def event(self, record, quiet):
        if record.get("type") == "done":
            self.result = record
        elif not quiet and record.get("type") == "say":
            print(f"  . {record['text'][:160]}")

    def finish(self):
        if self.result is None:
            return None
        return Outcome(ok=True, text=self.result["text"], stats="1 turn")


class MyAgent(Agent):
    name = "mine"
    image = "my-agent:latest"
    containerfile = Path("/path/to/Containerfile.mine")
    protocols = frozenset({OPENAI_CHAT})

    def argv(self, args, provider, task):
        return ["--json", task]

    def wire(self, args, provider, root):
        return Wiring(
            key_env="MY_API_KEY",
            base_url_env="MY_BASE_URL",
            base_url=(root or f"{provider.scheme}://{provider.host}")
            + provider.api_prefix,
        )

    def reader(self):
        return MyReader()
```

Run it without packaging anything:

```sh
sanduk 'Review this.' --agent mypkg.handlers:MyAgent --provider openai --proxy
```

Or advertise it, and it appears in `--agent` by name:

```toml
[project.entry-points."sanduk.agents"]
mine = "mypkg.handlers:MyAgent"
```

A plugin that fails to import is reported and skipped, and one that claims a
shipped name is refused: replacing `claude` would change what runs in the
container without changing the command line.

## Three things that are easy to get wrong

**The base URL prefix is yours to add.** The relay forwards paths unchanged and
checks them against `Provider.routes`, so the base URL you hand the agent has to
end where those routes begin. `provider.api_prefix` is that segment: `/v1` for
most, `/api/v1` for OpenRouter. Claude Code is the exception that proves it —
it appends `/v1/messages` itself, so its handler passes the bare root.

**A handler is stateless; a reader is not.** The registry holds handler classes
and `launch` calls `reader()` once per run. Keep the token tally and the final
record on the reader, or one run's counts leak into the next.

**Token counts are not comparable across agents.** Claude Code reports cache
reads outside `input_tokens`; hax normalizes them inside it. `Outcome.stats` is
a formatted line, not a struct, because there is no shared meaning to normalize
to.

## Refusing a run early

`Agent.check()` runs before the image is built. The base implementation refuses
a provider whose protocols the agent does not speak. Override it to refuse a
flag your agent has no equivalent for — silently dropping `--allowed-tools`
would weaken a restriction the caller asked for.
