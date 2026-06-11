"""App Control (WDAC) policy engine — pure-logic core (Phase AC-2).

This package ports ``interceptors/app_control/cli/Add-WDACRule.ps1`` to Python and
adds the self-protect rule generator + inbox-manifest validators that the
in-orchestrator runtime (AC-3) and the ``dlp-ctl`` authoring workflow (AC-4) call.

Everything here is **pure logic with no OS side effects** except the single,
isolated, mockable ``hashing`` module (which shells out to ``New-CIPolicyRule``
for the rare no-PE-metadata hash fallback). The package ships ``base.xml`` as
package data; the installer copies ``orchestrator/`` wholesale (AC-5), so it
travels for free.

Module map:
  - ``policy_xml``   — load/serialize a SiPolicy XML; insert FileAttrib / FilePath /
                       Hash Allow+Deny rules into UMCI SigningScenario 12.
  - ``selfprotect``  — generate + validate the agent's own FilePath coverage.
  - ``manifest``     — inbox manifest schema + validator suite.
  - ``hashing``      — the one OS-touching seam (``New-CIPolicyRule -Level Hash``).
"""

from pathlib import Path

#: Absolute path to the packaged WDAC base policy (unsigned enforcement base,
#: PolicyID ``{9DBAA326-CB59-4B1D-ABAF-B28412229E4A}``). Canonical for the agent.
BASE_POLICY_PATH = Path(__file__).resolve().parent / "base.xml"

#: The sipolicy XML namespace every element lives in.
SIPOLICY_NS = "urn:schemas-microsoft-com:sipolicy"
