# Direct calls (registrar-less mode)

Placing and receiving SIP calls between two baresipy instances without any SIP registrar, proxy,
or account credentials.

## How it works

If `BareSIP()` is constructed with `user`/`pwd`/`gateway` all left unset, there's nothing to
register with. baresipy instead creates a local, non-registering user agent as soon as baresip
signals it's ready:

```
/uanew sip:<user or "baresipy">@0.0.0.0;regint=0
```

`regint=0` tells baresip not to attempt SIP REGISTER at all — the UA exists purely so baresip can
place and accept calls locally. `user` is still accepted in this mode (it becomes the local
account's SIP username) even though `pwd`/`gateway` are not required.

Because there's no registrar to resolve short numbers against, `call()` in registrar-less mode
requires a full SIP URI:

```python
b = BareSIP(user="alice")          # no gateway -> registrar-less
b.call("sip:bob@192.168.1.42:5060")
```

Calling `call()` with a bare number and no `gateway` configured raises `ValueError` — there's
nowhere to resolve it against.

## Receiving direct calls

**Important caveat**: baresip only accepts an incoming call when the request-URI matches its
local account:

- the request-URI **host** must be one of the machine's own local IP addresses (not a hostname,
  and not `0.0.0.0`), and
- the request-URI **user** part must match the local account's user.

In practice this means the caller must dial the callee using the callee's actual reachable IP
and the exact user the callee registered locally, eg.:

```python
# callee, on host 192.168.1.42
callee = BareSIP(user="bob", headless=True)

# caller, elsewhere
caller = BareSIP(user="alice", headless=True)
caller.call("sip:bob@192.168.1.42:5060")
```

Dialing `bob@some-hostname` or `bob@0.0.0.0` from the caller side will not be accepted by the
callee, even on the same machine/network — resolve the callee's IP first and dial that.

By default, `BareSIP.handle_incoming_call()` rejects every inbound call. To answer, subclass and
override it, calling `accept_call()`:

```python
class Callee(BareSIP):
    def handle_incoming_call(self, number):
        self.accept_call()
```

## Working reference: the docker e2e rig

`docker-compose.e2e.yml` and `test/e2e/` are a complete, working example of two registrar-less,
headless baresip instances calling each other directly by IP over a private docker network — a
`callee` service that auto-accepts and echoes DTMF, and a `caller` service that dials it, sends
DTMF and a test tone, and asserts on the exchange. Both services are assigned static IPs in the
compose network specifically because of the IP-matching requirement described above (the caller
dials `sip:callee@172.31.99.10:5060`, the callee's static address, not a hostname).

See [docs/docker.md](docker.md) for how to run it, and `test/e2e/callee.py` / `test/e2e/caller.py`
for the full implementation (event handling, DTMF, recording the rx audio leg, and writing
results for `test/e2e/test_call.py` to assert on).
