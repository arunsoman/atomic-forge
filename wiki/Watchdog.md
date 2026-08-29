`watchdog.py`'s `WatchdogLoop` closes the loop past your local working
copy: detect a live failure, repair it with the exact evidence-based
localization + agentic sampling the [[Repair-Loop]] uses, land the fix as
a canary, and promote or roll back on a real health check — no local test
suite required, since the canary's own health check is the pass/fail
oracle.

```bash
atomic-forge watch --project-dir ./out --log-file /var/log/app.log \
    --deploy-cmd "python app.py {port}" --canary-percent 10
```

- **Detect** — `LogFailureDetector` tails a log file for Python
  tracebacks, reuses `repair_agent.extract_signals` to parse them (no
  second parser to keep in sync), and dedupes by fingerprint so a
  steadily-repeating crash surfaces once, not once per poll.
- **Repair** — the traceback is localized and patched the same way a
  failing local test would be, then committed.
- **Canary** — `LocalProcessCanaryDeployer` runs the pre-patch and
  post-patch code as two real subprocesses on two real ports, splits real
  HTTP traffic between them through a small stdlib reverse proxy, and
  health-checks the canary *directly* (not through the proxy, so a bad
  canary at 10% traffic still fails its own check immediately).
- **Promote / rollback** — N consecutive healthy checks promote the canary
  (stable process torn down); any unhealthy check rolls back (canary torn
  down, the patch reverted and committed).

## Bring-your-own infra

Both `FailureDetector` and `DeployTarget` are protocols with one real
reference implementation each. Bring your own (a real error tracker, a
real load balancer / Kubernetes) by implementing the same protocol —
nothing else about `WatchdogLoop` changes. This is the boundary of
"production infrastructure" forge draws deliberately: see
[[Persistent-Sandbox]] and [[Design-Notes]].