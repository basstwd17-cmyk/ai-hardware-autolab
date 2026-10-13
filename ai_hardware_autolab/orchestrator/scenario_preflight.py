"""Validate scenario input without contracts, campaign gates or device matching."""
import hashlib
class PreflightError(ValueError): pass
class ScenarioPreflight:
    def verify_scenario(self, scenario, digest):
        if not isinstance(scenario, str) or not scenario.strip(): raise PreflightError("a scenario is required")
        if digest != hashlib.sha256(scenario.strip().encode()).hexdigest(): raise PreflightError("scenario digest does not match")
    def _execution_mode(self, scenario, _unused=None): return "offline"
