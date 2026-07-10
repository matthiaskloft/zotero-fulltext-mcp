# Debug-bridge token setup

The `find-pdf` and `link-pdf` CLI commands (and manual item-creation via debug-bridge JS, see
the MCP server's notes) drive Zotero through the **debug-bridge** plugin — the test fixture
plugin from the `zotero-better-bibtex` project:
<https://github.com/retorquere/zotero-better-bibtex/tree/master/test/fixtures/debug-bridge>.
It is not bundled with this repository; install it as its own Zotero plugin (XPI) from that
source.

The bridge requires a shared Bearer token, identical on two sides:

- **Zotero plugin side** — Config Editor pref `extensions.zotero.debug-bridge.token`
- **CLI side** — env var `ZOTERO_DEBUG_BRIDGE_TOKEN` (or the `--debug-bridge-token` flag, which
  overrides the env var)

The bridge listens only on `http://127.0.0.1:23119/debug-bridge/execute`, i.e. **localhost only**
— this token is a local shared secret you generate yourself, not a network credential issued by
anyone else.

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
   $python -m zotero_pdf_text find-pdf --key <ITEM_KEY> --config .\config.json --debug-bridge-token "REPLACE_WITH_YOUR_OWN_TOKEN"
   ```

If you run this pipeline from more than one machine, generate the token once and use the same
value in each machine's Zotero Config Editor and CLI environment.

## Verify

```powershell
$python -m zotero_pdf_text find-pdf --key <ITEM_KEY> --config .\config.json
```

A `token not configured` / `HTTP 500` result means the two sides don't match or Zotero wasn't
restarted after setting the pref. A JSON result with `"ok": true` (or a `found`/`attachment_key`
field) means the bridge is working.

## Scope of `find-pdf` (important)

`find-pdf` only triggers Zotero's **"Find Available PDF"**, which searches for an **openly
available** copy (OA repositories, publisher OA, Unpaywall-style sources). It does **not** use
institutional/paywall access.

- For **paywalled** items it will find nothing, and the debug-bridge call may **time out** (the CLI
  uses a 30s hard timeout) rather than returning a quick `found: false`. A timeout here is *not* a
  token/config problem — if you have progressed past `token not configured`, the bridge is working.
- **Paywalled papers must be attached manually**: drag the PDF onto the item, or right-click the
  item → *Add Attachment → Attach Stored Copy of File*.

## Notes

- If you ever rotate the token, change it in **both** places, and on every machine you run this
  pipeline from.
