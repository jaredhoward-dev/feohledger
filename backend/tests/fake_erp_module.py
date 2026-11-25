"""tools/fake-erp's app module, importable in-process, so an adapter test can
use it as its only transport (``httpx.ASGITransport``) without a container."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_FAKE_ERP = Path(__file__).resolve().parents[2] / "tools" / "fake-erp"


def load_fake_erp():
    """fake-erp's app module, loaded once. It imports its sibling modules by
    bare name, so its directory goes on the path; ``defusedxml`` (used only by
    its Intacct / SYSPRO routes, never reached here) is not a backend
    dependency, so a stand-in is registered when it is absent."""
    if "fake_erp_app" in sys.modules:
        return sys.modules["fake_erp_app"]
    if str(_FAKE_ERP) not in sys.path:
        sys.path.insert(0, str(_FAKE_ERP))
    try:
        import defusedxml  # noqa: F401
    except ImportError:
        stub = types.ModuleType("defusedxml")
        stub.DefusedXmlException = type("DefusedXmlException", (ValueError,), {})
        stub.ElementTree = types.ModuleType("defusedxml.ElementTree")
        sys.modules["defusedxml"] = stub
        sys.modules["defusedxml.ElementTree"] = stub.ElementTree
    spec = importlib.util.spec_from_file_location("fake_erp_app", _FAKE_ERP / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
