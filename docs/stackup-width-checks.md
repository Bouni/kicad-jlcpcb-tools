# Stackup selection and saved width results

This iteration adds board-level JLCPCB stackup selection and offline display of
saved nominal trace-width results to controlled-impedance review. It does not
change the PCB, net-class assignments, trace geometry, or the vendor workbook's
actual-width field.
Targeted automated checks exercise transport adapters, persistence and simulated
dialog workflows. [Native Linux checks](linux-impedance-testing.md) separately
exercise real dialogs and report output. Live catalog compatibility requires
provider probes; neither source review nor mocked tests establish native behavior.

## Workflow and persistence

Select the target stackup in the main **Controlled impedance** dialog. Candidates
are restricted to the PCB's complete enabled copper layer count, not the toolbar's
fabrication-layer preference or only the layers carrying matched traces. Thickness
and copper-weight filters narrow that catalog. Inspect the construction and make
an explicit selection; catalog updates never silently replace the saved construction.
The selector links to JLCPCB's [stackups](https://jlcpcb.com/impedance) and
[impedance calculator](https://jlcpcb.com/pcb-impedance-calculator).

The construction pane is a schematic cross-section, not a paragraph or a
thickness-scale drawing. Each physical copper, core, prepreg or other dielectric
row remains visible in vendor order. Copper uses canonical KiCad names and the
user's saved PCB palette; separate columns show thickness in **mm** and **Dk**.
Adjacent dielectric rows are not merged, and missing values show **—**, not
guessed material properties. Readable minimum row heights keep thin copper
visible; tall constructions scroll vertically. Labels and status do not wrap.
Long names, material codes and values remain available in tooltips.

**Copy definition** below the cross-section copies the selected stackup as plain
text: construction name/IDs, board dimensions and copper weights, separate
preferred and surcharge categories, and every physical layer with its full
material, thickness in mm and Dk. The copied text uses the exact displayed
snapshot, including a retained saved selection, rather than looking up a newer
catalog entry by name. Values are not ellipsized and missing fields remain
explicit. Copy is disabled without a visible selection. It does not accept the
stackup, close the selector, alter board settings or write another file; clipboard
failures are shown inline.

The header shows the selected construction, finished thickness and compact
preferred/saved status. IDs, retrieval timestamps and duplicated pricing prose
are removed from this pane, not from stored provenance. Repeated catalog status
updates retain the same diagram and scroll position when its displayed data has
not changed. A saved construction remains the saved snapshot on catalog refresh.
An empty selection clears the drawing; absent construction has an explicit
unavailable state.

The cross-section is drawn in memory with native controls and vector bands;
there are no generated image files, external render services, or PCB mutations.
Default, classic and partial custom themes follow KiCad's saved copper-color
settings. Invalid/unavailable colors produce explicitly labeled neutral bands;
this fallback does not affect the separate PCB capture renderer's strict palette
handling. Both use shared theme lookup and color decoding in `palette.py`, but
stackup colors do not depend on a valid capture background or dimming setting.
Capture backgrounds require exact opacity before alpha rounding; copper bands
retain their alpha. Theme changes accepted in Preferences are read when the
selector is opened again.

The selected stackup is a frozen snapshot in the board's existing `project.db`
configuration. A separate project-level cache stores public catalog snapshots by
layer count, with a separate UTC timestamp for the latest successful complete
catalog check. There is no **Refresh catalog** button. On opening the main
**Controlled impedance** dialog, an automatic background check runs if there are
no compatible cached stackups,
or if the last successful check was more than 24 hours ago. Missing, invalid or
future check times are treated as due. Cached choices remain usable while checking;
typing filters does not cause network requests. A dedicated status line in the
main dialog shows progress or errors independently of section-review messages.
The selector joins the same check, including when opened before the queued
main-dialog startup callback runs. Already-open selectors receive new choices
when the check completes; opening another selector does not start another request.
The main dialog is the sole request owner. The picker requires its shared catalog
controller and only subscribes/unsubscribes; there is no separate standalone
fetch/cache mode in the picker.

A successful check updates the shared rows and check timestamp together, even
when the catalog is unchanged or empty. An empty result is checked again on the
next main-dialog opening, but never loops within the same dialog. A saved board selection
alone does not count as a cached catalog, and filters hiding all rows do not make
a nonempty catalog due. There is no continuous polling while the main dialog stays
open. Elapsed UTC time is used, not calendar-day boundaries or per-stackup
retrieval dates.

Failed requests preserve the previous cache and check time, report the failure
inline, and retry on the next main-dialog opening if still due. Closing the
picker only detaches it: the parent-owned check continues and can save its result.
Closing the main dialog cancels pending work; late callbacks cannot replace the
cache. Observing a changed PCB copper-layer count replaces the old check with one
for the new count. A completed successful
check remains cached even if the selection dialog is subsequently canceled; it
does not change board settings or their revision. Cache-write failures are shown
explicitly and leave the previous persistent cache intact. Selection,
specification edits, results and audit records remain
private until accepted through the parent configuration save. No configuration,
catalog, result or image sidecars are introduced.

Catalog payload version 2 adds the check timestamp inside the existing database
table. Version 1 rows load unchanged with unknown freshness and are checked on
opening; reading them performs no migration write. This is separate from the
board configuration's version and approval fingerprints.

Specifications select complete net classes. Actual widths, including neckdowns,
are discovered from routing. Enter the intended target, reference layers and
applicable pair/ground gaps. Existing saved width results report target width,
actual width and signed dimensional difference in mm/mil. Unavailable,
unsupported, failed and outdated results are shown as such, never converted into
a successful match.

Changing meaningful stackup or specification inputs invalidates final section
approval and makes earlier layer approvals historical. Workbook rows repopulate
automatically after those changes; use **Approve N workbook rows** below the list
to approve the new report selection. **Needs approval** makes the pending action
explicit, and a green check with **N workbook rows approved** identifies an
approved selection. Reopening a specification requires a fresh layer review and
records a new layer approval only when explicitly approved. Saved result model,
numeric-input fingerprints and assumptions participate in approval currentness.
Numeric fingerprints omit retrieval timestamps and transient request identifiers,
so audit metadata alone does not alter physical intent or reset final report
approval. Accepting an unchanged specification likewise retains final report
approval. A previously unseen PCB filename starts with empty impedance settings;
moving the entire project and database together preserves saved settings. Save
conflicts and a changed live board identity reject stale saves.

**Save and close** in the main controlled-impedance dialog saves settings
independently of final report approval. You can select a stackup, save it without
adding specifications, and return later; the selection remains in the existing
board database. **Needs review** does not disable saving, but an enabled report
still blocks fabrication export until approved. Changing a stackup never grants
approval. **Cancel** discards the main dialog's unsaved selection; a failed save
keeps that dialog open with its edits intact.

Workbook rows also populate when the main dialog opens and after accepted
specification additions, edits or removals. An unchanged approved configuration
restores its saved checked rows and approval on reopening. The first relevant
preview is selected automatically, preferring the specification just added or
edited; harmless updates preserve the current row. There is no manual
**Rescan / reset review** button. A population failure shows an inline explanation
and contextual **Retry**, while a configuration without specifications has an
explicit empty state. Population and retry never grant approval or themselves
create image-view timestamps; successfully displaying the selected preview is
still a separate tracked event. A detected board change refreshes rows before
requiring another explicit approval. Unapproved checkbox exclusions remain
session-local until the final reviewed selection is saved.

## Preferred does not mean a price quote

The ordering API supplies enabled orderable stackups. The calculator catalog
supplies its frontend's preferred classification: the display label contains
`Standard` or `推荐`. These records are joined by exact construction code and
physical settings, not a similar display name. The “No requirement” alias is not
an explicit controlled-impedance stackup.

A fire icon mirrors that source classification, with a textual preferred status
in the construction header. The icon has its own narrow cell, separate from text.
The previous **Preference** and **Pricing** columns are replaced by one **Price**
column: **Normal**, **Additional**, or **—** when surcharge information is
unavailable. These labels use the existing provider surcharge evidence, not the
preferred boolean, and are price categories rather than currency quotes. Absence
of the fire does not establish that a stackup costs more. Missing calculator
metadata means preference is unknown, not “non-preferred.”
Quoted fabrication prices remain JLCPCB's responsibility.

The icon is **Fire, Font Awesome Free 6.7.2**, ©2024 Fonticons, Inc., licensed
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Its geometry is unchanged;
the fill is orange and display dimensions are explicit. The packaged
`impedance/resources/preferred-fire-LICENSE.txt`, SVG comment, picker attribution
and offline HTML attribution preserve its source and license. It is not copied
JLCPCB artwork. [Original SVG](https://github.com/FortAwesome/Font-Awesome/blob/6.7.2/svgs/solid/fire.svg).

## Catalog transport and saved results

HTTP requests honor configured proxies and Requests CA-bundle overrides while
explicitly suppressing implicit `.netrc` origin authentication. Redirects remain
disabled and certificate verification is never bypassed. Failures distinguish
DNS, proxy, certificate/TLS, timeout, and interrupted-response categories without
printing connection details or credentials. Connect/read timeouts and cooperative
elapsed-time checks prevent accepting late results; they cannot forcibly interrupt
an operating-system DNS lookup already in progress. Cancellation never waits for
the HTTP worker on the GUI thread.

The catalog normalizes JLCPCB's type-3 “Bare board” entry as one dielectric, with
the frontend's summed top/dielectric/bottom thickness and supplied dielectric
constant. It does not invent copper layers for this unnamed entry. This permits
complete six-layer catalogs to load.

Saved results retain nominal width, status, calculation time, provider/model,
assumptions, and declared/effective-input fingerprints. Raw provider responses,
numeric request archives, and full solver configuration dumps are not retained in
board settings or shown as HTML forensic details. The HTML shows the compact
model/assumption summary; it does not imply that nominal catalog copper thickness
is always the finished-copper model input. Effective-input fingerprints still participate in approval even
when the rounded nominal width is unchanged.

Configuration payload version 5 writes these compact results. An ordinary
version-4 net-class record is converted without a write, retaining design intent
and historical review timestamps but clearing old report approval. The
first explicit upgraded save atomically replaces the previous configuration
without saving a history. Versions 1–3 and version-4 records with retired
width/net filters require explicit reset and recreation; there is no
inline legacy-specification conversion. See
[Project storage and board identity](controlled-impedance.md#project-storage-and-board-identity).

Comparisons are **nominal width checks**, not impedance verification. They do not
measure pair/ground gaps, prove uninterrupted reference planes, model transitions
and connectors, or establish manufacturing tolerance. No percent-width tolerance
is presented as a percent-impedance tolerance. Actual routed width remains in
the XLSX; the HTML adds the frozen stackup, nominal comparison and provenance.
Generation does not fetch the network or invent new review/calculation timestamps.
Saved results remain part of review currentness; “nominal” does not mean that
changing their meaningful inputs or results bypasses approval.

## Catalog diagnostic evidence

The complete six-layer production download reproduced a parser failure on
JLCPCB's type-3 construction entries before the fix. With the mapping corrected,
the same `fetch_stackups(6)` path returned **157 selectable stackups, including
23 preferred**, in approximately **15 seconds** during this development check.
Those counts and timing are observations, not fixed catalog expectations.
The original generic native connection failure was not captured directly;
proxy/CA preservation and more realistic read timeouts address source-level
transport defects, and specific error messages make remaining failures diagnosable.
Regression tests in `tests/test_jlcpcb_catalog_transport.py` pass for bare-board
geometry, Requests environment/auth behavior and sanitized errors using mocked
HTTP responses. Live service and native UI behavior require the separate checks
described below.

## Acceptance checklist

Before release, execute—not merely inspect—the following scenarios:

- Reopen new, draft and approved configurations and verify automatic row
  population, an explanatory no-specification state, restored approved selections,
  and a visible relevant preview. Add, edit and remove specifications, select a
  different stackup: no manual scan should be needed. Check selected-row retention
  on harmless updates, no-op edits and metadata-only changes preserving approval, contextual **Retry** after population
  failure, and absence of stale previews after removal or failure. Confirm the
  count-bearing approval button and pending/green-approved state match the checked
  rows. A board change discovered at approval must refresh without approving in
  the same click. Population alone must not create audit timestamps, and draft
  save/reopen must not imply unapproved checkbox exclusions were persisted.
- Exercise **Copy definition** with an ordinary/current row, a frozen saved row
  whose catalog entry changed, and missing construction/material/Dk metadata.
  Paste into a plain-text editor and confirm full values and layer order.
  Filter to zero rows or clear selection: copy must disable and must never copy
  stale construction. Clipboard-busy, failed SetData, unsupported persistence,
  and platform clipboard ownership must not produce false success, close the
  dialog, or alter board/cache data. Native copy/paste remains deferred.
- Review the native construction pane at its minimum/default size, multiple
  display scalings and light/dark UI themes: no wrapped or overlapping labels,
  flame and Price separated, normal/additional/unknown surcharge categories,
  and readable row values without shrinking fonts. Exercise 2/6/8/16/32-layer
  construction, very thin copper, consecutive dielectrics, missing Dk, absent
  construction, long material codes and long decimal values. Verify all physical
  rows remain accessible by vertical scrolling, and tooltips expose full text.
- Check saved/default/classic/custom palettes, partial overrides, transparency,
  missing/invalid theme files and a theme switch followed by selector reopening.
  Missing colors must be labeled neutral rather than presented as KiCad colors.
  Saved/current selection, filtering to no rows, repeated catalog notices,
  refreshed metadata and monitor changes must not show stale construction,
  lose meaningful review position, alter board intent, or touch stored provenance.
- Open and filter 2/4/6/8+ layer catalogs; confirm full PCB layer count, retained
  saved snapshots, ordering-only entries, preferred/fee separation, empty results,
  stale cache visibility, malformed responses, failed checks and cancellation.
- Check the automatic-catalog transitions: missing/empty cache, old version-1
  rows, missing/invalid/future time, just under/exactly/just over 24 hours, UTC
  and daylight-saving boundaries, and two independent copper-layer counts.
  A fresh nonempty cache must cause no request. Unknown/empty/stale data starts
  exactly one request after the main dialog is shown, never on construction, resize,
  filter changes, duplicate show events, or repaint. A frozen saved selection
  must not suppress that request; zero filtered rows must not trigger one.
- Confirm successful changed, unchanged and empty responses save rows plus time
  atomically. Reopen without accepting a board selection and inspect reuse.
  Failed fetch, malformed response, canceled work, stale callback and failed
  cache write must not advance persistent freshness or replace old rows. Closing
  the main dialog before the queued initial check must prevent it from starting.
  Open the picker before and after startup/completion: it must share the request,
  retain filters/selection and update live. Close/reopen the picker mid-request:
  the check must continue and save once. Main OK/Cancel/native destruction must
  cancel, detach observers and reject stale delivery. Change the observed board
  layer count during loading and confirm independent cache keys and cancellation
  of the old check. Keep board selection/review unchanged throughout automatic
  checking, and ensure catalog status is not overwritten by section-review status.
- Exercise real wx construction, resize and scrolling at small/large display
  scaling; confirm the fixed footer and green/gray checklist remain readable,
  current-image navigation is correct, and failed/hidden renders never count.
- Load ordinary version-4 net-class records without writes; verify preserved
  targets, references, stackup intent, calculation summaries and historical dates,
  with old report approval cleared. Saving version 5 must atomically replace the
  previous configuration. Versions 1–3 and version-4 legacy-filter records must
  show actionable errors and require explicit reset before recreation.
  Exercise child/outer Cancel, concurrent saves, rollback, board copy/rename and
  unknown-schema errors; none may silently reset or broaden matching.
- Confirm each compact width result is tied to all relevant inputs, model and
  assumptions; current results and approval survive a normal version-5 reopen,
  meaningful changes require review, and stale results never appear current.
  Verify exact width and differing neckdown rows, and confirm saved settings
  contain no raw solver archive.
- Export offline HTML/XLSX/ZIP and inspect actual widths, nominal comparisons,
  immutable stackup details, fee/preference, timestamp currentness, escaped vendor
  data and embedded CC icon attribution. Failure must preserve the previous ZIP.
- Exercise macOS native UI and the existing Linux native KiCad CI job. Windows
  qualification remains separate. Execute the native stages and retain their
  results and artifacts; unit tests of those checks do not establish native
  behavior.
