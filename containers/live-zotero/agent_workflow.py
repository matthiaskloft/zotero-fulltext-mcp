"""Manual agent acquisition task: captured tools and independent state verification.

Run inside the isolated Zotero container. The pure assess() function is tested on the host.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pymupdf

from zotero_pdf_text.bibtex import execute_javascript
from zotero_pdf_text.artifacts import ManagedIndexMissingError, resolve_reader_db_path
from zotero_pdf_text.config import load_config
from zotero_pdf_text.fts import chunk_sha256, get_fulltext, search_fts
from zotero_pdf_text.pre_write_checks import check_doi_duplicate, check_existing_pdf

ROOT = Path("/work/agent-task")
CONFIG = Path("/work/config.live-test.json")
ARTICLE = {
    "doi": "10.1371/journal.pone.0000308",
    "title": "Sharing Detailed Research Data Is Associated with Increased Citation Rate",
    "pdf_url": "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0000308&type=printable",
    "pdf_sha256": "b52ca44dfd09d240543b80315f0cd43fe9ba7946f2b5650d5580213ed5d9186c",
}


def save(name, value):
    ROOT.mkdir(parents=True, exist_ok=True)
    temporary = ROOT / (name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(ROOT / name)


def read(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def guarded_config():
    config = load_config(CONFIG)
    for path in (config.zotero_root, config.zotero_data_directory, config.linked_attachments, config.output_root):
        if not path.resolve().is_relative_to(Path("/work")):
            raise RuntimeError("Only the isolated /work test workspace is allowed")
    os.environ["ZOTERO_DEBUG_BRIDGE_TOKEN"] = Path("/work/bridge-token.txt").read_text().strip()
    probe = execute_javascript("return Zotero.DataDirectory.dir;")
    if not probe.ok or Path(probe.result).resolve() != config.zotero_data_directory.resolve():
        raise RuntimeError("Live Zotero is not using the isolated test data directory")
    return config


def snapshot(article):
    config = guarded_config()
    script = """
const search = new Zotero.Search();
search.libraryID = Zotero.Libraries.userLibraryID;
search.addCondition('DOI', 'is', DOI);
const items = await Zotero.Items.getAsync(await search.search());
return items.filter(i => !i.deleted).map(i => ({key:i.key, doi:i.getField('DOI'), title:i.getField('title')}));
""".replace("DOI);", json.dumps(article["doi"]) + ");")
    result = execute_javascript(script)
    if not result.ok:
        raise RuntimeError(result.error)
    items = result.result
    for item in items:
        pdfs = check_existing_pdf(item["key"], config.zotero_sqlite)
        if pdfs.get("source") != "debug_bridge":
            raise RuntimeError("Snapshot must read committed live Zotero state")
        item["attachments"] = []
        for attachment in pdfs.get("attachments", []):
            path = str(attachment.get("path", ""))
            linked = path.startswith("attachments:")
            file = config.linked_attachments / path.removeprefix("attachments:") if linked else Path(path)
            safe = file.resolve().is_relative_to(config.linked_attachments.resolve())
            observation = {"key": attachment["key"], "linked": linked and safe, "exists": safe and file.is_file()}
            if observation["exists"]:
                observation["sha256"] = hashlib.sha256(file.read_bytes()).hexdigest()
                with pymupdf.open(file) as pdf:
                    observation["pages"] = len(pdf)
            item["attachments"].append(observation)
    db = config.output_root / "index" / "zotero_text_index.sqlite"
    try:
        published = resolve_reader_db_path(db)
    except ManagedIndexMissingError:
        indexed = []
    else:
        indexed = [hit.to_dict() for hit in search_fts(published, title=article["title"])]
    return {"items": items, "indexed": indexed}


def record(interface, name, arguments, result, ok, **observations):
    trace = read("trace.json")
    trace.append({"sequence": len(trace), "at": datetime.now(timezone.utc).isoformat(),
                  "phase": read("phase.json"), "interface": interface, "name": name,
                  "arguments": arguments, "result": result, "ok": ok, **observations})
    save("trace.json", trace)


def cli_arguments(arguments):
    """Normalize argparse's equivalent --option=value spelling for trace grading."""
    parts = [part for value in arguments for part in
             (value.split("=", 1) if value.startswith("--") and "=" in value else [value])]
    return [next((name for name in ("--key", "--doi") if part.startswith("--") and name.startswith(part)), part)
            for part in parts]


def validate_cli_arguments(arguments):
    allowed = {"import-doi", "check-pdf", "find-pdf", "link-pdf", "convert-new"}
    protected = {"--config", "--db", "--debug-bridge-token", "--debug-bridge-endpoint", "--connector-endpoint"}
    if not arguments or arguments[0] not in allowed:
        raise ValueError("Use an allowed CLI command")
    for value in arguments[1:]:
        option = value.split("=", 1)[0]
        if option.startswith("--") and any(name.startswith(option) for name in protected):
            raise ValueError("Cannot override the isolated config or endpoints, including abbreviated options")


def assess(article, before, after, repeat, trace):
    """Grade facts and trace together; never accept the agent's own success claim."""
    checks = []

    def check(step, state, observed, evidence):
        status = "fail" if not state else "pass" if observed else "insufficient_evidence"
        checks.append({"step": step, "status": status, "evidence": evidence})

    trace = [dict(e, arguments=cli_arguments(e["arguments"])) if isinstance(e["arguments"], list) else e for e in trace]
    initial = [e for e in trace if e["phase"] == "initial" and e["ok"]]
    repeated = [e for e in trace if e["phase"] == "repeat" and e["ok"]]
    lookup = next((e for e in initial if e["name"] == "lookup-doi" and e["arguments"].get("doi") == article["doi"]
                   and e["result"].get("key") is None and e["result"].get("live") is True), None)
    imports = [e for e in initial if e["name"] == "import-doi" and article["doi"] in e["arguments"]]
    check("identify_absence", not before["items"], bool(lookup and imports and lookup["sequence"] < imports[0]["sequence"]),
          {"before_items": len(before["items"]), "lookup_sequence": lookup["sequence"] if lookup else None})
    items = after["items"]
    correct = len(items) == 1 and items[0]["doi"].lower() == article["doi"].lower() and items[0]["title"] == article["title"]
    check("add_article", correct, any(e["result"].get("key") == items[0]["key"] for e in imports) if correct else False, {"items": items})
    attachments = items[0]["attachments"] if len(items) == 1 else []
    pdf = attachments[0] if len(attachments) == 1 else {}
    attach_calls = [e for e in initial if e["name"] in {"link-pdf", "find-pdf"} or
                    (e["name"] == "import-doi" and ("--with-pdf" in e["arguments"] or
                     any(i.get("attachments") for i in e.get("state_after", {}).get("items", []))))]
    attached_to_target = any((e["name"] == "import-doi" and e["result"].get("key") == items[0]["key"]) or
                            ("--key" in e["arguments"] and items[0]["key"] in e["arguments"]) for e in attach_calls) if correct else False
    check("attach_correct_pdf", bool(pdf.get("exists") and pdf.get("pages", 0) > 0 and pdf.get("sha256") == article["pdf_sha256"]),
          attached_to_target, pdf)
    check("link_pdf", bool(pdf.get("linked")), attached_to_target, pdf)
    indexed = [hit for hit in after["indexed"] if hit.get("zotero_attachment_key") == pdf.get("key") and
               hit.get("zotero_parent_key") == (items[0]["key"] if len(items) == 1 else None) and
               hit.get("doi", "").lower() == article["doi"].lower()]
    check("convert_and_index", bool(indexed), any(e["name"] == "convert-new" for e in initial), {"indexed_records": len(indexed)})
    retrievals = [e for e in initial if e["name"] == "get_fulltext_chunk" and e["arguments"].get("attachment_key") == pdf.get("key")]
    valid = []
    for event in retrievals:
        text = event["result"].get("text", "")
        if (event.get("verified_current") and text and
                chunk_sha256(text) == event["arguments"].get("chunk_sha256")):
            valid.append(event)
    searched = any(e["name"] in {"search_fulltext", "search_within_fulltext"} and e["sequence"] < r["sequence"]
                   and any(h.get("attachment_key") == pdf.get("key") and h.get("source_locator", {}).get("chunk_sha256") == r["arguments"].get("chunk_sha256")
                           and h.get("source_locator", {}).get("chunk_index") == r["arguments"].get("chunk_index")
                           for h in e["result"].get("results", [])) for e in initial for r in valid)
    check("retrieve_verified_passage", bool(valid) if retrievals else True, bool(valid and searched), {"valid_passages": len(valid)})
    def identity(state):
        return sorted((item["key"], tuple(sorted(a["key"] for a in item["attachments"]))) for item in state["items"])
    check("avoid_duplicates", repeat is not None and identity(after) == identity(repeat) and correct,
          any((e["name"] == "import-doi" and article["doi"] in e["arguments"] and
               e["result"].get("status") == "already_in_library" and correct and
               e["result"].get("key") == items[0]["key"]) or
              (e["name"] == "lookup-doi" and e["arguments"].get("doi") == article["doi"] and
               e["result"].get("live") is True and correct and e["result"].get("key") == items[0]["key"])
              for e in repeated),
          {"repeat_recorded": repeat is not None})
    return {"verifier_version": 3, "passed": all(c["status"] == "pass" for c in checks), "checks": checks}


async def mcp_call(name, arguments, config):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if name == "list":
        from zotero_pdf_text.mcp_contract import create_server
        server = create_server(config.output_root / "index" / "zotero_text_index.sqlite", config=config)
        return {"tools": [tool.model_dump(mode="json") for tool in await server.list_tools()]}, True
    params = StdioServerParameters(command=sys.executable, args=["-m", "zotero_pdf_text.mcp_server", "--db",
        str(config.output_root / "index" / "zotero_text_index.sqlite"), "--config", str(CONFIG)], env=dict(os.environ))
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            response = await session.call_tool(name, arguments)
            return response.structuredContent or {}, not response.isError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "lookup-doi", "cli", "mcp", "checkpoint", "verify"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    config = guarded_config()
    if args.action == "prepare":
        if (ROOT / "before.json").exists():
            raise RuntimeError("This task already has a baseline; use a fresh test container")
        before = snapshot(ARTICLE)
        if before["items"]:
            raise RuntimeError("Target article is already present; use a fresh test container")
        if before["indexed"]:
            raise RuntimeError("Target article has stale index data; use a fresh test container")
        save("article.json", ARTICLE)
        save("before.json", before)
        save("trace.json", [])
        save("phase.json", "initial")
        print(f"Find {ARTICLE['title']} (DOI {ARTICLE['doi']}) in my library. If missing, add it and attach its PDF. "
              f"A direct PDF URL is {ARTICLE['pdf_url']}. Make it searchable and retrieve a passage supporting its main finding about data sharing and citations. "
              "Use the lookup-doi, cli and mcp tool commands provided by your operator.")
        return 0
    article = read("article.json")
    if args.action == "lookup-doi":
        result = check_doi_duplicate(article["doi"], config.zotero_sqlite)
        record("library", "lookup-doi", {"doi": article["doi"]}, result, result.get("live") is True)
    elif args.action == "cli":
        try:
            validate_cli_arguments(args.arguments)
        except ValueError as exc:
            parser.error(str(exc))
        process = subprocess.run([sys.executable, "-m", "zotero_pdf_text", *args.arguments, "--config", str(CONFIG)],
                                 capture_output=True, text=True, timeout=900)
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError:
            result = {"stdout": process.stdout, "stderr": process.stderr}
        observations = {"state_after": snapshot(article)} if process.returncode == 0 and args.arguments[0] in {
            "import-doi", "find-pdf", "link-pdf",
        } else {}
        record("cli", args.arguments[0], args.arguments[1:], result, process.returncode == 0, **observations)
        print(json.dumps(result, indent=2))
        return process.returncode
    elif args.action == "mcp":
        name = args.arguments[0]
        arguments = json.loads(args.arguments[1]) if len(args.arguments) > 1 else {}
        result, ok = asyncio.run(asyncio.wait_for(mcp_call(name, arguments, config), timeout=90))
        record("mcp", name, arguments, result, ok)
        print(json.dumps(result, indent=2))
        return 0 if ok else 1
    elif args.action == "checkpoint":
        if read("phase.json") != "initial":
            raise RuntimeError("Initial checkpoint already recorded")
        save("after.json", snapshot(article))
        save("phase.json", "repeat")
        print("Repeat the same article task, ensuring it is available with its PDF and a supporting passage.")
        return 0
    else:
        if read("phase.json") != "repeat":
            raise RuntimeError("Record an initial checkpoint and perform the repeat task first")
        trace = read("trace.json")
        for event in trace:
            if event["name"] == "get_fulltext_chunk" and event["ok"]:
                arguments = event["arguments"]
                try:
                    current = get_fulltext(resolve_reader_db_path(config.output_root / "index" / "zotero_text_index.sqlite"),
                        attachment_key=arguments["attachment_key"], chunk_index=arguments["chunk_index"],
                        expected_chunk_sha256=arguments["chunk_sha256"])
                    event["verified_current"] = current.text == event["result"].get("text")
                except (LookupError, ValueError, OSError, ManagedIndexMissingError):
                    event["verified_current"] = False
        result = assess(article, read("before.json"), read("after.json"), snapshot(article), trace)
        save("verdict.json", result)
        for check in result["checks"]:
            print(f"{check['status'].upper():22} {check['step']}")
        return 0 if result["passed"] else 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
