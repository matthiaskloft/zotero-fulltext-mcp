# Debug-bridge token setup

The `find-pdf`, `link-pdf`, and `import-doi` CLI commands drive Zotero through the
**debug-bridge** development plugin from the Better BibTeX project.
Download its XPI from the [debug-bridge release](https://github.com/retorquere/zotero-better-bibtex/releases/tag/debug-bridge)
and install it in Zotero through Tools → Plugins → gear menu → Install Plugin From File.
It is separate from the regular Better BibTeX plugin and is not bundled with this repository.
The [upstream instructions](https://github.com/retorquere/zotero-better-bibtex/blob/master/test/fixtures/debug-bridge/README.md)
describe its token authentication. `import-doi` and `check-pdf` also use it, read-only, for their live duplicate/PDF
checks; without it they fall back to a verified copy of `zotero.sqlite` and label the answer as
not live.

The bridge requires a shared Bearer token, identical on two sides:

- **Zotero plugin side** — Config Editor pref `extensions.zotero.debug-bridge.token`
- **CLI side** — env var `ZOTERO_DEBUG_BRIDGE_TOKEN` (or the `--debug-bridge-token` flag, which
  overrides the env var)

The default endpoint is `http://127.0.0.1:23119/debug-bridge/execute` on localhost.
The token is a shared secret you generate yourself. It authorizes JavaScript execution
inside Zotero, so keep it private and do not expose the bridge to the network.

## Generate your own token

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use the same generated value in both places below. Do not reuse the example value shown here —
`REPLACE_WITH_YOUR_OWN_TOKEN` is a placeholder, not a real token.

## Install steps

1. **Zotero plugin side.** Zotero → Settings → Advanced → **Config Editor**. Search
   `extensions.zotero.debug-bridge.token`. If it exists, set its value to your generated token;
   if it does not exist, create it as a **String** pref with that name and value. Restart Zotero.
   (Ensure the debug-bridge XPI is actually installed — Tools → Plugins.)

2. **CLI side.** Make the same value visible to the CLI. Either persist it as a user env var:

   ```powershell
   setx ZOTERO_DEBUG_BRIDGE_TOKEN "REPLACE_WITH_YOUR_OWN_TOKEN"
   # open a new shell afterwards so the value is loaded
   ```

   or pass it per-command:

   ```powershell
   & $python -m zotero_pdf_text check-pdf --key "<ITEM_KEY>" --config $config --debug-bridge-token "REPLACE_WITH_YOUR_OWN_TOKEN"
   ```

Here `$python` and `$config` are the interpreter and absolute config path set in the
[README](../README.md). In bash, use `export ZOTERO_DEBUG_BRIDGE_TOKEN='REPLACE_WITH_YOUR_OWN_TOKEN'`
and invoke commands with `"$python" -m zotero_pdf_text ... --config "$config"`.
On each machine, the token in that machine's Zotero preference and CLI environment must match.

## Verify

```powershell
& $python -m zotero_pdf_text check-pdf --key "<ITEM_KEY>" --config $config --json
```

Replace `<ITEM_KEY>` with an existing parent item's eight-character Zotero key. This check
does not add or change attachments. A response with `"live": true` confirms the bridge
answered; `found` reports whether the item already has a PDF. A response with `"live": false`
used the SQLite-copy fallback and does not verify the bridge. Inspect `bridge_error`, confirm
the plugin is installed and the tokens match, and restart Zotero after changing its preference.

## Scope of `find-pdf` (important)

`find-pdf` triggers Zotero's **"Find Available PDF"**. Zotero tries the item's DOI/URL
and open-access sources. It can also retrieve publisher PDFs available through direct or
VPN-based institutional access, but it does not inherit a browser's web-proxy login.
See [Zotero's explanation](https://forums.zotero.org/discussion/96563/find-pdf).

- When Zotero cannot access a PDF, it may return `not_found`; a slow lookup may also
  **time out** (the CLI allows `find-pdf` 90 s). The CLI then reports
  `outcome: "unknown"`: check the item in Zotero (or `check-pdf`) before rerunning. A timeout here is
  not proof of a token/config problem; verify the bridge with the read-only check above.
- If Zotero cannot retrieve a paper you can access in your browser, save it with the Zotero
  Connector or attach a downloaded PDF manually: drag it onto the item, or right-click the
  item → *Add Attachment → Attach Stored Copy of File*. Let ZotMoov finish moving it before indexing.
- When you know a direct, openly accessible PDF link that Zotero's resolvers missed, use
  `link-pdf --key <ITEM_KEY> --url <direct-pdf-url>` (or `import-doi --with-pdf --pdf-url ...`)
  instead of writing your own `importFromURL` script through the bridge. It refuses non-public
  hosts and non-PDF responses, and it reports the attachment key as it stands after a ZotMoov
  auto-move.

## Notes

- If you ever rotate the token, change it in **both** places, and on every machine you run this
  pipeline from.

## Adding papers and attaching PDFs

`import-doi`, `find-pdf`, and `link-pdf` change your Zotero library through Zotero's APIs.
`link-pdf --file` also asks ZotMoov to relocate the linked file into its configured destination.
Use `--config` with your absolute config path in every command below.

Run these commands with the Python of the environment the server is installed in
(`<venv python> -m zotero_pdf_text <command>`, or the `zotero-pdf-text` console script next to
it); `uv run` works only from a checkout with `uv` on `PATH`. A typical add-by-DOI flow is
`import-doi --doi <DOI>` (prints the new item's `key`, its `item_type` name and `key_source`;
`key_source: "ambiguous"` with `key: null` when the translator created several items, all listed
in `keys`), then `check-pdf --key <key>`, then `find-pdf --key <key>`. Before it imports,
`import-doi` looks for an existing item with that DOI, and `check-pdf` looks for an existing PDF.
Both read live from Zotero through debug-bridge when it answers; otherwise they read a
hash-verified copy of `zotero.sqlite` (so `import-doi` still works without plugins) and say so:
`duplicate_check` in the `import-doi` output, and `source`/`live` (plus `bridge_error`) in the
`check-pdf` output, with a note on stderr / in the text. If neither can be read, `import-doi`
imports nothing and exits 1, and `check-pdf` prints `found: null` and exits 1: an unchecked
answer is never reported as "no duplicate" or "no PDF". A write that lands between the check and
the import is still a race. Without a working bridge each run copies and hashes `zotero.sqlite`
and its sidecars, and `import-doi` and `check-pdf` refuse (fail closed: exit 1, `found: null` for `check-pdf`) if Zotero is
writing during the copy;
re-run when it is idle. `find-pdf` reports
`outcome`: `attached` (with the attachment key as it stands after any ZotMoov auto-move -- the
only outcome whose `attachment_key` is usable), `not_found` (Zotero's resolvers found nothing;
attach an open-access copy with `link-pdf --key <key> --url <direct-pdf-url>`, or
`--file <pdf>` for a local copy), `unsettled` (a file
was added but its final key was not observed in time), or `unknown` (the bridge timed out). A
debug-bridge timeout never means "failed": `import-doi` does not fall back to the connector and
`zotero-write apply` reports `auto_run_timed_out`, because Zotero may already have made the
change. After `unsettled`, `unknown` or a timeout, check Zotero; do not rerun.

`link-pdf --key <key> --url <direct-pdf-url>` downloads the PDF itself and stores it in Zotero
as a copy (`Zotero.Attachments.importFromFile`); with ZotMoov's auto-move on, it waits for the
move exactly like `find-pdf` and reports the same outcomes (`attached`, `unsettled`, `unknown`,
`error`, plus `refused_existing_pdf`). The URL must be `https` without a user name or password,
and every redirect hop (at most 5) must resolve to public addresses only, so a link from paper
text cannot reach the Zotero connector, debug-bridge or your local network. The download is capped
(`--max-mb`, default 200) and must start with `%PDF-`, so a publisher landing page is refused. It
is staged in a temporary folder that is always removed; nothing is written to `output_root`. The
item's attachments are checked live first, and an item that already has a PDF is refused unless
`--allow-additional` is given. The output adds `source_url`, `final_url`, `size`, `sha256` and
`verified_hash` (the attached file's SHA-256 matches the download). `import-doi --with-pdf
[--pdf-url <direct-pdf-url>]` chains the steps: it skips an item that already has a PDF, runs
`find-pdf`, and on `not_found` runs `link-pdf --url` with the given link. It runs for a new item
(`key_source` `created_item` or `connector_lookup`) or one already in the library, stops at
`unsettled`, `unknown` or an error, and adds `steps` and `pdf_outcome` to the output (unchanged
without `--with-pdf`). MCP clients
discover these commands through `guide("writes")` and propose the exact command for you to
approve; the server itself never writes to Zotero.
