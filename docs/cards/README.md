# Dashboard cards

Example cards for the integration's sensors. They are optional: copy one into a dashboard
(edit the dashboard, **Add card**, **Manual**, and paste the YAML).

## Bill card

[`bill-card.yaml`](bill-card.yaml): the current billing cycle and the previous bill, from
the bill estimate's sensors.

<!-- TODO(screenshot): the bill card on a dashboard, both parts. The maintainer to supply
     as ../images/bill-card.png. -->
*(Screenshot to follow.)*

**Needs:**
- [button-card](https://github.com/custom-cards/button-card), installed through HACS
  (**HACS > Frontend**, search "button-card");
- the integration's **Bill estimate** turned on (site settings, **Bill estimate**: a
  billing day and the daily charges).

**What it shows:**
- **This bill so far:** **Bill to date**, with "incl. today (est.)" (the attribute
  `including_today_estimated`, shown once yesterday is imported and Today so far is set
  up), the **Projected bill**, the cycle's dates, the day of the cycle and the day the
  data runs to.
- **Previous bill (estimate):** the **Previous bill estimate** sensor: the last complete
  cycle's total, its dates, each line (usage, export credit, the network's export charge
  with the export allowance, and each daily charge, GST included), and whether every day
  of that cycle was imported (`complete`). Until then it says how many days it covers.

  <!-- TODO(screenshot): the previous bill part close up, with its lines. The maintainer
       to supply as ../images/bill-card-previous.png. -->
  *(Screenshot to follow.)*

Tapping either part opens the sensor, with all its attributes.

**Another site:** the entity IDs in the file are an account's first site's. For another
site, replace `amber_energy_dashboard_` with the site device's name as a slug, for example
`beach_house_` for a site named "Beach House" (check the IDs on the site's device page).

**The estimate and your bill:** both totals are estimates from Amber's billed usage and
the charges you entered. They leave out one-off charges (such as card payment fees), and
a cycle's total is complete once its last day has been imported (normally the morning
after the cycle ends). If you used a template package of your own to show the last
cycle's total, the **Previous bill estimate** sensor replaces it.
