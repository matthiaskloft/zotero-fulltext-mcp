# Live Zotero test

`import-doi`, `find-pdf` and `link-pdf` run JavaScript inside Zotero through debug-bridge. The
normal suite checks that code offline: recorded bridge payloads go through the parsers, and the
generated `find-pdf` script runs in Node against a fake `Zotero` seeded from a recorded
environment (`tests/test_zotero_bridge_fixtures.py`, fixtures in `tests/fixtures/zotero_bridge/`).
A fake cannot tell you that real Zotero still behaves the way it was recorded. The opt-in live
test does that: it runs the real CLI against a running Zotero.

**The live test writes to Zotero.** It imports a paper by DOI and attaches its PDF. It never
deletes anything. Run it only against a separate Zotero profile with its own data directory,
never against your everyday library. The test refuses to write when it can tell that it would
touch your everyday library (see [Guards](#guards)).

## One-time setup: a separate Zotero profile

1. Close Zotero. Only one Zotero can listen on the local port that debug-bridge and the connector
   use (23119), so the live test must never run while your everyday Zotero is open.
2. Start the profile manager with `zotero -P` (on Windows,
   `"C:\Program Files\Zotero\zotero.exe" -P`). Create a profile, for example `zotero-live-test`,
   and start it.
3. In that profile, set a separate data directory: Settings → Advanced → Files and Folders →
   Data Directory Location → Custom, for example `C:\Users\you\ZoteroLiveTest\data`. Restart
   when Zotero asks you to.
4. Set the Linked Attachment Base Directory (same settings pane) to a test folder, for example
   `C:\Users\you\ZoteroLiveTest\linked`.
5. Install the plugins in this profile:
   - **debug-bridge**, with the token pref set as described in
     [debug-bridge-setup.md](debug-bridge-setup.md). The same `ZOTERO_DEBUG_BRIDGE_TOKEN` works
     for both profiles.
   - **ZotMoov**, with its destination directory set to the linked folder from step 4, automatic
     moving on, and file behavior "move". This is the setup whose auto-move `find-pdf` waits for.
6. Write a test config, outside the repository, whose `zotero_data_directory` is the test
   profile's data directory. For example, `C:\Users\you\ZoteroLiveTest\config.live-test.json`:

   ```json
   {
     "zotero_root": "C:\\Users\\you\\ZoteroLiveTest",
     "zotero_data_directory": "C:\\Users\\you\\ZoteroLiveTest\\data",
     "linked_attachments": "C:\\Users\\you\\ZoteroLiveTest\\linked",
     "output_root": "C:\\Users\\you\\ZoteroLiveTest\\converted_text"
   }
   ```

   Every path must exist (`check-setup --config <test config>` confirms that).

## Run it

Start Zotero with the test profile (`zotero -P zotero-live-test`), then run the test from the
repository root:

```bash
ZOTERO_LIVE_TEST=1 ZOTERO_LIVE_TEST_CONFIG=<test config> .venv/Scripts/python.exe -m pytest -m live_zotero -s
```

```powershell
$env:ZOTERO_LIVE_TEST = "1"
$env:ZOTERO_LIVE_TEST_CONFIG = "C:\Users\you\ZoteroLiveTest\config.live-test.json"
.\.venv\Scripts\python.exe -m pytest -m live_zotero -s
Remove-Item Env:ZOTERO_LIVE_TEST, Env:ZOTERO_LIVE_TEST_CONFIG
```

`-s` shows the keys the test created. Without `ZOTERO_LIVE_TEST=1` and a test config that exists,
the test is skipped, and it is skipped when debug-bridge does not answer. That is why it never
runs in CI.

The test imports the first open-access arXiv DOI from a short list that is not yet in the test
library, as `import-doi`'s own duplicate check reports it. It then runs `check-pdf`, `find-pdf`
and `check-pdf` again. It asserts that `item_type` is a type name, that `key_source` is
`created_item`, and that `find-pdf` reports `attached`. It also asserts that the final attachment
key is the single PDF that `check-pdf` sees afterwards. When every DOI on the list is already
present, the test is skipped with "reset the test profile". To reset, empty the test library
(select all items, move them to the trash, then empty the trash) or create a new test profile.

## Guards

Before its first write, the test asks the running Zotero for its data directory
(`Zotero.DataDirectory.dir`, a read-only probe) and fails without writing when:

- that directory is not the test config's `zotero_data_directory`, which means the Zotero
  answering on the port is some other profile, for example your everyday library;
- that directory is the one your normal config uses (`resolve_config_path()`: the
  `ZOTERO_PDF_TEXT_CONFIG` env var, or `config.<hostname>.json`/`config.json` in the current
  directory). The test config is also refused when it *is* your normal config.

## Recording fixtures

Add `ZOTERO_LIVE_RECORD=1` (`$env:ZOTERO_LIVE_RECORD = "1"`) to save the raw bridge payloads of
the run into `tests/fixtures/zotero_bridge/`. The recorder replaces the reconstructed fixtures
with raw captures. It runs the real CLI through `tests/live_zotero_recorder.py`, which wraps
`execute_javascript`. Before anything is written, the payloads are sanitized: Zotero keys become
placeholders (`AAAA1111`, `BBBB2222`, ...), titles become `<title withheld>`, and paths are
removed (a linked path keeps only its `attachments:` prefix). A key the sanitizer missed makes
the recording fail. Review the diff before committing. Each fixture's `_provenance` field records
how it was captured.

Record again whenever the bridge code in `src/zotero_pdf_text/bibtex.py` changes, and after a
Zotero or ZotMoov upgrade.
