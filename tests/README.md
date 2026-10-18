# State tests

Fake Kick API + scripted scenarios (channel offline, game change, no progress, not linked,
priority changes, claim, manual mode, mining hours, expired token). Timings are sped up.

```bash
python tests/fake_kick.py &      # runs the real app on :8099 against a fake Kick
python tests/run_states.py
```
