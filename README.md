# Amber Energy Usage Dashboard

Gets your real Amber Electric grid import/export usage updated
automatically every day, (plus an optional one-time historical backfill)
into Home Assistant's built-in Energy Dashboard, with accurate cost
tracking against Amber's live dynamic pricing.

**The core purpose of this kit is the daily piece**: once set up, your
Energy Dashboard keeps itself updated with real usage data (one day
behind, matching Amber's own settlement delay) with no ongoing effort
from you. The historical backfill script is a separate, optional bonus
for populating history before you started — the daily automation works
fully on its own without it.

## Why this exists

Home Assistant's official Amber Electric integration provides live price
sensors only — it does **not** expose your actual usage/consumption data
(an older version did, but it was removed as a "duplicate" of the price
sensor). In the absence of any local power consumption monitoring, there is
no other automatic source for real grid import/export figures. This kit
fills that gap using Amber's own usage API, which has your real historical
data (the same data their app and website show you).

## What you get

- A one-time script that backfills up to ~90 days of your usage history
- A daily automation that keeps it updated going forward
- Accurate cost/compensation tracking, using Amber's live price sensors
- None of it touches the built-in Amber integration — this runs alongside it

## Related projects

This isn't the only community effort to get real usage/pricing data into
Home Assistant beyond the official integration:
- [`hass-amber-electric`](https://github.com/madpilot/hass-amber-electric) —
  the original Amber custom component, now merged into HA core (its
  energy/usage sensor was removed in that process — the gap this kit fills)
- [`Home-Assistant-custom-components-Tauron-AMIplus`](https://github.com/PiotrMachowski/Home-Assistant-custom-components-Tauron-AMIplus) —
  a different (Polish) energy retailer integration using the same core
  approach as this kit: injecting historical data directly as statistics
  rather than through live entity states, to get proper hourly history
  into the Energy dashboard

## Status

Tested end-to-end on two separate Home Assistant instances: one behind a
DuckDNS/reverse-proxy HTTPS setup, one plain HTTP on a LAN with no
certificate. Both required different `HA_BASE_URL` handling (see
"Troubleshooting" below) — if you hit a connection issue this kit hasn't
seen before, it's most likely a variant of that.

## Privacy note

`amber_backfill_cache.json` (created when you run the backfill script)
contains your real historical household energy usage. Don't commit it,
attach it to a bug report, or otherwise share it — the `.gitignore`
included here excludes it and `secrets.yaml` by default.

## Prerequisites

- [ ] Home Assistant with the official **Amber Electric** integration
      already added (for live price sensors) — Settings > Devices & Services
- [ ] An Amber API key — generate one in the Amber app: Settings > enable
      Developer Mode > Generate API Key
- [ ] Your Amber **site ID** — not the same as your account login. Find it with:
      ```
      curl -H "Authorization: Bearer YOUR_API_KEY" https://api.amber.com.au/v1/sites
      ```
- [ ] [HACS](https://hacs.xyz/) installed
- [ ] The **Import Statistics** integration installed via HACS (search
      "Import statistics" — by klausj1). After installing via
      HACS, you must **also** add it as an integration: Settings > Devices
      & Services > Add Integration > "Import Statistics"
- [ ] A Home Assistant **Long-Lived Access Token** (Settings > your profile
      > Security tab) — only needed for running the one-time backfill script
- [ ] A machine that can run Python 3 with the `requests` library, and can
      reach your Home Assistant instance (a laptop, a WSL box, anything)
- [ ] **Know whether your Home Assistant serves HTTP or HTTPS on its API
      port.** See the troubleshooting section below — getting this wrong
      is the single most confusing failure mode in this whole setup, and
      it fails silently with no useful error message.

## Setup

1. **Merge `configuration_snippet.yaml`** into your `configuration.yaml`.
   Two values need filling in, in **two different places**:
   - Your **Amber site ID** goes directly into the `rest:` block, replacing
     `YOUR_SITE_ID` in the URL (the snippet has a comment marking exactly
     where).
   - Your **Amber API key** goes into `secrets.yaml` (a separate file, in
     the same folder as `configuration.yaml`), not into the snippet itself:
     ```yaml
     amber_api_key: "Bearer psk_your_key_here"
     ```

   Pasting the whole snippet at the **end** of your existing
   `configuration.yaml` is fine — order doesn't matter. The one thing to
   check first: if you already have a top-level `rest:`, `input_number:`,
   `template:`, or `recorder:` key anywhere else in the file, merge this
   snippet's entries into your existing one instead of pasting a second
   copy — YAML doesn't allow the same top-level key twice, and Home
   Assistant will only use one of them.

   Then **Settings > Developer Tools > YAML > Check Configuration**, and
   once that passes, **Settings > System > Restart > Restart Home
   Assistant** — a full restart, not a quick reload. New entities like the
   ones this snippet creates don't reliably appear after a partial/quick
   reload; only a full restart is guaranteed to pick them up.

2. **Confirm the entities exist.** Developer Tools > States, search
   "amber" — you should see `sensor.amber_energy_import`,
   `sensor.amber_energy_export` (both reading `0`), and
   `input_number.amber_energy_import_running_total` /
   `..._export_running_total` (both reading `0`). If they're not there,
   go back and confirm you did a full restart, not just a config check.

3. **Edit `amber_backfill.py`** — fill in your API key, site ID, HA base
   URL (**with the correct scheme, see troubleshooting**), HA token, and
   how many days you want (`DAYS`, default 90).

4. **Run it**:
   ```
   sudo apt update && sudo apt install python3-requests -y
   python3 amber_backfill.py
   ```
   On a stock Debian/Ubuntu system (including WSL), `pip` often isn't
   installed at all — the `apt` package above is the simplest route and
   needs no extra steps. If you'd rather use `pip` (e.g. you're on a
   system that already has it, or a virtual environment), that also works:
   ```
   pip install requests --break-system-packages
   ```
   The script caches each day's fetch to `amber_backfill_cache.json` as it
   goes — safe to re-run if interrupted (e.g. by an Amber rate limit); it
   will skip days already fetched, and retries rate limits automatically.

5. **Verify before trusting it.** Developer Tools > Actions. Search for
   `import_statistics: export_statistics`, then switch to YAML mode (a
   small toggle in the top-right corner of the action card) and paste:
   ```yaml
   action: import_statistics.export_statistics
   data:
     statistic_id: sensor.amber_energy_import
     start_time: "<90 days ago>"
     end_time: "<today>"
     filename: "verify.csv"
     decimal: "."
   ```
   Then check the exported file (find it with
   `find / -name verify.csv 2>/dev/null`, likely in `/homeassistant/`)
   shows one row per day with sensibly increasing totals — no single
   huge jump, no resets to zero partway through.

6. **Add to the Energy Dashboard.** Settings > Dashboards > Energy > edit
   the grid connection: set "Energy imported from grid" to
   `sensor.amber_energy_import`, "Energy exported to grid" to
   `sensor.amber_energy_export`. For cost tracking, choose "Use an entity
   with current price" / "current rate" and select the Amber integration's
   General Price / Feed In Price sensors.

7. **Install the daily automation.** Settings > Automations & Scenes >
   Create Automation > Create new automation > three-dot menu (top right)
   > Edit in YAML, then replace the placeholder content with everything
   in `daily_automation.yaml` and save. Test it once manually before
   waiting for 6am: open the saved automation, three-dot menu > Run, then
   re-run the verification step above to confirm a new day landed cleanly.

## Why this design (read this before changing anything)

The dashboard-facing sensors (`sensor.amber_energy_import/export`) are
**permanently frozen at state `"0"` and never change.** All real data
reaches them only through `import_statistics.import_from_json` calls,
which write directly into Home Assistant's long-term statistics table.

This is deliberate, and it fixes a real bug we hit building this: Home
Assistant automatically compiles long-term statistics from any entity
with `state_class: total_increasing` based on its **live state changes** —
completely independent of anything manually imported. If you feed data
into such a sensor by actually changing its state (e.g. via an
`input_number` mirrored into it), the automatic compiler and your manual
import both end up writing to the same statistics, and they will
conflict — producing exactly the kind of corruption this design avoids
(a giant phantom single-hour spike, in our case, from a single seeding
step being read as a huge real jump).

The `input_number` helpers in this kit have **no `state_class`** — that's
what keeps them invisible to the compiler. They exist purely as the daily
automation's own private memory of "the running total so far." Never give
them a `state_class`, `device_class`, or `unit_of_measurement` that would
make Home Assistant treat them as a real sensor.

### Why the `recorder: exclude` block is not optional

Freezing the dashboard sensors' state at a constant `"0"` is **not enough
on its own**. Home Assistant's automatic statistics compiler runs on its
own schedule (roughly hourly) for *any* entity with a `state_class` —
completely independent of whether you've also manually imported data for
it. Since the sensor's real, actual state genuinely is `"0"` forever, the
compiler will eventually run, see that true value, and record it as a
real statistics point. For a `total_increasing` sensor, a value lower
than the current total is read as a meter reset — silently wiping out
the entire imported history back to zero.

This isn't a rare edge case: it happened during this kit's own testing,
several hours after an otherwise perfectly clean backfill, with nothing
else changed in between.

The fix is `recorder: exclude:` — excluding these entities from state
recording entirely. With no recorded states, the automatic compiler has
nothing to compile from and leaves the entity alone. The Energy Dashboard
is unaffected, since it reads from long-term statistics (populated
entirely by our `import_statistics` calls), not from state history.
You'll see a "this entity is no longer being recorded" note in Developer
Tools > Statistics after adding this — that's the expected, correct
outcome, not an error.

**Do not skip this block.** If you're extending this kit to track
additional entities the same way (see "Known limitations" below), add
each new dashboard-facing sensor to this exclude list too.

### Why `sum`/`state` values, not `delta`

The Import Statistics integration also supports a `delta` mode (supply
each day's plain increase, let the tool compute the running total). We
initially used this for the backfill and hit two problems:
1. `delta` mode requires at least one existing statistics row to compute
   against — a brand new entity has nothing to anchor to.
2. Under load (many rapid API calls), we observed roughly 90kWh of data
   silently go missing from a 90-day backfill, likely a race condition
   between chunked calls each independently reading "the current total so
   far" before the previous call's write had fully committed.

Using absolute `sum`/`state` values (computed in Python, sent in one
single call) avoids both problems entirely, and is what this kit uses
throughout.

## Troubleshooting

**Every API call to Home Assistant returns "Empty reply from server" or a
connection reset, with nothing in the logs.**
This means you're using `http://` when your Home Assistant instance is
actually serving `https://` on that port — very common if you use a
DuckDNS (or similar) hostname with a real certificate for remote access,
even for local/LAN calls. Try `https://` instead. You will likely also
need `verify=False` in the Python script (or `-k` with curl) since
self-issued/DuckDNS certificate chains are often incomplete for strict
verification — this is a mild security trade-off, acceptable for calls
originating from your own trusted network.

**`import_statistics.import_from_json` returns a 500 error mentioning
"Entity does not exist".**
The template sensor hasn't been created yet, or `configuration.yaml`
wasn't restarted after adding it. Check Developer Tools > States for the
exact entity_id first.

**Re-running the backfill script doesn't fix wrong historical data.**
This integration does not appear to overwrite already-imported
timestamps — re-sending the same day is silently ignored. If you need to
correct bad history, your only clean options are: import into a **new**
entity name and repoint the dashboard (accepting the old, wrong data sits
unused in the background — this is what we did), or direct database
surgery (not covered here, genuinely risky, take a full backup first).

**A chunked/multi-call import loses data.**
Send it as a single call instead. We could not find official guidance on
a safe request size limit, but a full 90-day/2-entity payload (~180 data
points) succeeded reliably as one call once the HTTP/HTTPS issue above
was fixed — the chunking was a mitigation for a problem that turned out
to be something else entirely.

**Amber's API returns 429 (rate limited).**
The script retries automatically, respecting `Retry-After` if present. If
it keeps failing, wait a few minutes before re-running — it will resume
from the cache rather than re-fetching everything.

**`export_statistics` requires a `filename` and `decimal` field.**
Both are required even though it might seem like it should just return
data directly. Use `decimal: "."` for a standard `.`-separated CSV.

**Connection refused when the backfill script tries to reach Home
Assistant.**
Check the *exact* URL (including port) that loads your dashboard
successfully in a browser. Don't assume port 8123 — depending on your
installation method, HA may be served on a different port (or no
explicit port at all, i.e. plain port 80) with no redirect from 8123.
Whatever works in your browser is what belongs in `HA_BASE_URL`.

**History looks fine right after backfill, but shows a reset to zero
hours or days later.**
This is the `recorder: exclude` issue described above under "Why this
design" — if you skipped that block, or it didn't apply correctly, add
it and restart. This does not undo already-corrupted history (see the
"re-running doesn't fix wrong historical data" point above), but it
stops it from happening again going forward.

**How to actually confirm the `recorder: exclude` fix is working, rather
than assuming it is.**
⚠️ **This test permanently and visibly pollutes one hour of the target
entity's history with an obviously fake number, and that cannot be
undone or cleaned up afterward** (see "re-running doesn't fix wrong
historical data" above) — do this on a disposable test sensor if you
want to keep your real history clean, not on your live
`sensor.amber_energy_import`/`export` unless you're comfortable with a
permanent artificial spike sitting in that hour.

The corruption this kit works around took *hours* to appear during its
own testing — a clean-looking result immediately after setup is not
sufficient proof the fix is working. To confirm properly:
- Manually import one obviously fake, distinctive value (e.g. `99999`)
  at the current hour, via `import_statistics.import_from_json`
- Wait at least an hour
- Export and check that timestamp again — if `99999` is still there
  unchanged, the exclusion is genuinely working. If it has been reset or
  overwritten, something is wrong with your `recorder: exclude`
  configuration (check for typos in the entity names, and confirm you
  restarted after adding it) and you should resolve that before relying
  on this for real data.

**Config Check passes and the file looks right, but the new entities
still don't show up after restarting.**
Occasionally Home Assistant's YAML parsing seems to get into a state
where a genuinely correct file doesn't take effect. If a full restart
(not a partial reload) still doesn't produce the entities, try: delete
the pasted snippet from `configuration.yaml` entirely, save, Check
Configuration, restart — then paste the snippet back in fresh, save,
Check Configuration, restart again. This "clear it out and re-add it"
sequence has resolved this for at least one tester when simply
restarting again did not.

## Known limitations

- **Cost accuracy is daily-granularity, not per-interval.** The Energy
  dashboard's "current price" cost tracking multiplies each day's total
  usage by whatever Amber's price happens to be *at the moment the
  automation runs* — not the true weighted price across that day's
  30-minute intervals (which can swing from negative to 30+ c/kWh within
  hours). Amber's own API returns a real `cost` figure per interval; a
  future enhancement would track that directly the same way this kit
  tracks kWh, rather than relying on HA's live-price approximation.
- **No automatic historical price backfill.** Only usage (kWh) is
  backfilled; historical cost figures before you install this kit will
  show as $0 on the dashboard.
- **Not tested with a Controlled Load channel.** Only General and Feed In
  channels are handled. If your Amber account has a Controlled Load
  tariff, you'll need to extend the script's channel filtering yourself.
