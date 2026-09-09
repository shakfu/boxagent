# Surviving a host restart

Status: investigated, 2026-09-09. Target: none. Recommendation is to do the
cheap half and write the rest down.

Scope: what it would take for a wakeup in flight to survive the death of the
process that started it, the way nanoclaw's `adoptRunningSessions()` does.
Nothing here is implemented.

## What dies today, and what does not

A run is a `container run` child whose stdout this process reads, plus a relay
thread in this process holding the real key. Kill the process and:

| | outcome |
| --- | --- |
| the container | keeps running. Measured: a `kill -9` left both it and the network holder alive |
| the agent's work in `/work` | survives, up to whatever it had written |
| the relay | gone, so the agent's next call fails |
| the JSON stream | gone; nothing was reading it |
| the run record | survives, and the next run reaps the container (`runs.py`) |
| an assistant's wakeup row | stays open, and `serve` treats the interrupt as no failure |

So the loss is not the container and not the files: it is the relay and the
stream. Adoption means restoring both.

## The four things adoption needs

1. **A container whose output can be re-read.** Today `launch` reads the
   child's stdout, which no second process can attach to. Apple's engine can
   replay a detached container's output: `container logs <name>` prints what
   has been written and `container logs --follow` streams from there. Measured
   against a detached container printing a JSON line every two seconds: both
   worked. Docker has the same two, plus `--since`.
2. **A reader that can start from the beginning.** `logs --follow` replays the
   whole log, and Apple's engine has `-n` but no `--since`. That is not a
   problem: a `Reader` is fresh per run and tallies from zero, so replaying the
   log from the start produces the same totals. Adoption re-reads rather than
   resumes.
3. **A relay at the same address.** The container's base URL is fixed in its
   environment when it starts, so a new relay has to bind the same
   `gateway:port`. The gateway is deterministic per network and `--proxy-port`
   already pins the port; today it defaults to an ephemeral one.
4. **The run token.** The relay checks a `secrets.token_urlsafe(24)` that
   exists only in this process's memory. A new relay cannot invent it: the
   container is already holding the old one. It would have to be written to
   the run record.

Point 4 is the one with a cost. The token is worthless off-host -- the relay
binds the bridge gateway, which nothing outside that network can reach -- and
it dies with the run. But "in memory only" becomes "in a file under
`$XDG_STATE_HOME`, mode 0600", and that is a real change to what an attacker
with read access to the state directory gets.

nanoclaw does not have this problem because its credential gateway is a
separate service resolving auth per request: there is no per-session secret to
restore. Its comment says exactly that -- an adopted session's egress keeps
working "without any per-process state to rebuild".

## Two designs

| | adopt | re-run |
| --- | --- | --- |
| what happens on restart | find the live container, re-bind the relay, replay its log, finish the run | reap the container as an orphan, run the wakeup again |
| work lost | none | the whole wakeup |
| new state on disk | the run token | none |
| new code | detached launch, log-follow reader, fixed port, token in the record, adoption in `tick`/`serve` | none: this is what happens now |
| rough size | 250-350 lines plus tests | 0 |

Re-run is already the behaviour, and it is not obviously wrong: `--timeout`
defaults to 900s, so the most a restart costs is fifteen minutes of one
wakeup, and an assistant's messages are not consumed by a wakeup that did not
finish. Adoption earns its keep when wakeups are long and the host is
restarted often -- a laptop that sleeps, a `serve` under a supervisor that
restarts it, a wakeup measured in hours rather than minutes.

## What I would do

Nothing yet, except the part that costs nothing:

- **Pin the relay port for assistants.** `--proxy-port` exists; an assistant
  that always uses the same port is a prerequisite for adoption and harmless
  without it. One line in `run_argv`, and it makes the later change additive.
- **Record why a wakeup ended.** The `runs` table has an exit code; an
  interrupted wakeup and a failed one are already distinguished in
  `schedule_next` but not in the row. Storing it makes "how often does a
  restart cost us a wakeup" answerable, which is the number that decides
  whether adoption is worth building.

Then look at the number. If restarts are costing real work, build adoption in
the order above: detached launch first, since it is the only piece that
changes how every run is started.
