"""Natural-language -> Tableau workbook, with a validate-and-repair loop.

    python3 author.py --csv examples/sales.csv --request "How are sales trending by region?" -o out/answer.twbx
    python3 author.py --csv examples/sales.csv --plan plan.json -o out/answer.twbx   # offline, no model call
    python3 author.py --csv orders.csv --csv customers.csv --plan plan.json -o out/answer.twbx  # two tables

Pipeline: profile the CSV -> Claude writes a ChartSpec plan (JSON-schema
structured output) -> chartspec.py places fields (our "Show Me") ->
spec_to_twb.py writes the workbook -> desktop_check.py validates it the way
Tableau Desktop does. Any error goes back to the model verbatim, appended to
the same conversation, for up to --max-repairs turns.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # sibling modules, also under `python -I`
import desktop_check  # noqa: E402
import integrity  # noqa: E402
from chartspec import PLAN_SCHEMA, plan_to_vizspec  # noqa: E402
from profile import profile  # noqa: E402
from spec_to_twb import SpecError, compile_spec  # noqa: E402

MODEL = "claude-opus-5-5"
SYSTEM_PROMPT = (HERE / "prompts" / "author.md").read_text()


class ModelError(RuntimeError):
    pass


def build_and_check(plan: dict, csv: Path | list[Path], out: Path) -> tuple[list[str], dict]:
    """Compile a plan to `out` (.twb or .twbx). Returns (errors, vizspec)."""
    csvs = [csv] if isinstance(csv, Path) else list(csv)
    spec, errors = plan_to_vizspec(plan, csvs, [str(c.resolve()) for c in csvs])
    if errors:
        return errors, spec
    with tempfile.TemporaryDirectory() as tmp:
        spec_path = Path(tmp) / "spec.json"
        spec_path.write_text(json.dumps(spec))
        twb = Path(tmp) / "check.twb"
        try:
            compile_spec(spec_path, twb)
        except SpecError as e:
            return [f"compiler: {e}"], spec
        issues = integrity.check(ET.parse(twb).getroot())
        if issues:
            return [f"integrity: {i}" for i in issues[:15]], spec
        has_schema = (desktop_check.CACHE / f"{desktop_check.SCHEMAS['twb']}.xsd").exists()
        if not has_schema:
            print("warning: Tableau Desktop's schema isn't cached; skipping the Desktop check "
                  "(run desktop_check.py --extract-from <Tableau .app>)", file=sys.stderr)
        if has_schema:
            ok, issues, _ = desktop_check.check(twb)
            if not ok:
                return [f"tableau-desktop-schema: {i}" for i in issues[:15]], spec
        compile_spec(spec_path, out)
    return [], spec


class Author:
    def __init__(self, model: str, effort: str):
        import anthropic  # imported lazily so --plan mode needs no SDK

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model, self.effort = model, effort
        self.messages: list[dict] = []

    def ask(self, content: str) -> dict:
        self.messages.append({"role": "user", "content": content})
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",  # re-run server-side on a recommended model if this one declines
                cache_control={"type": "ephemeral"},
                system=SYSTEM_PROMPT,
                output_config={"effort": self.effort,
                               "format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
                messages=self.messages,
            )
        except self.anthropic.RateLimitError as e:
            raise ModelError(f"rate limited: {e.message}") from e
        except self.anthropic.APIStatusError as e:
            raise ModelError(f"API error {e.status_code}: {e.message}") from e
        except self.anthropic.APIConnectionError as e:
            raise ModelError(f"network error: {e}") from e
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise ModelError(f"model declined the request (category: {category})")
        if response.stop_reason == "max_tokens":
            raise ModelError("response hit max_tokens before the plan was complete")
        # Append the full content (thinking blocks included) so the conversation stays append-only.
        self.messages.append({"role": "assistant", "content": response.content})
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise ModelError("no JSON plan in the response")
        u = response.usage
        print(f"  model: {response.model}  in={u.input_tokens} cache_read={u.cache_read_input_tokens} "
              f"out={u.output_tokens}", file=sys.stderr)
        return json.loads(text)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--csv", required=True, type=Path, action="append",
                   help="a CSV; repeat for several tables of one data model")
    p.add_argument("--request", help="what the user wants to see")
    p.add_argument("--plan", type=Path, help="offline: use this ChartSpec plan instead of calling the model")
    p.add_argument("-o", "--out", required=True, type=Path, help=".twb or .twbx")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-repairs", type=int, default=3)
    a = p.parse_args(argv)
    if not (a.request or a.plan):
        p.error("give --request (model) or --plan (offline)")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    log = {"request": a.request, "attempts": []}
    t0 = time.time()

    if a.plan:
        plan = json.loads(a.plan.read_text())
        errors, spec = build_and_check(plan, a.csv, a.out)
        log["attempts"].append({"plan": plan, "errors": errors})
    else:
        author = Author(a.model, a.effort)
        prof = profile(a.csv)
        prompt = f"PROFILE:\n{json.dumps(prof, indent=1)}\n\nREQUEST:\n{a.request}"
        try:
            plan = author.ask(prompt)
            for attempt in range(a.max_repairs + 1):
                errors, spec = build_and_check(plan, a.csv, a.out)
                log["attempts"].append({"plan": plan, "errors": errors})
                print(f"attempt {attempt + 1}: {'OK' if not errors else f'{len(errors)} error(s)'}", file=sys.stderr)
                if not errors or attempt == a.max_repairs:
                    break
                plan = author.ask("The plan could not be built. Fix every error and return the full corrected plan.\n"
                                  + "\n".join(f"- {e}" for e in errors))
        except ModelError as e:
            print(f"model error: {e}", file=sys.stderr)
            return 3

    sidecar = a.out.with_suffix(".author.json")
    log.update(vizspec=spec, seconds=round(time.time() - t0, 1))
    sidecar.write_text(json.dumps(log, indent=1))
    if errors:
        print("FAILED:\n" + "\n".join(f"  - {e}" for e in errors), file=sys.stderr)
        return 1
    print(a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
