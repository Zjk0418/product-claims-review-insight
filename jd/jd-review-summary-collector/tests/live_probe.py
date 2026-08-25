"""Read-only live probe for a debug-enabled Chrome; not run by unit tests."""

import asyncio
import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "jd_review_summary.py"
SPEC = importlib.util.spec_from_file_location("jd_review_summary_live", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


async def main() -> None:
    ws_url = MODULE._select_or_create_jd_target("100047005216")
    async with MODULE.CDPClient(ws_url) as cdp:
        await cdp.wait_jd_ready("100047005216")
        print("page", await cdp.eval("JSON.stringify({href:location.href,title:document.title,signer:!!window.PSign,sha:!!window.SHA256,token:!!window.getJsToken})"))
        batch = await cdp.batch_summaries(["100047005216", "100142563319"])
        print("batch diagnostic", cdp.last_diagnostic)
        signed = await cdp.signed_summary("100047005216")
        print("signed diagnostic", cdp.last_diagnostic)
    print("batch", batch)
    print("signed", signed)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())
