# Controlled-impedance documentation

JLCPCB Tools can include an illustrated impedance form in a board's Gerber ZIP.
The form records the designer's intended impedance and identifies selected copper
routes. Matching a net class identifies the intended signals; it does not verify
impedance, reference-plane continuity, differential spacing, or the chosen stackup.

## Configure and review a board

1. Save the PCB and open JLCPCB Tools. Select **Controlled impedance** beside the
   fabrication layer selector, or open **Configure…**. Initial enablement opens
   the configuration dialog when there is no current approved configuration.
   With **Fill zones** enabled, opening or reopening review first refills the live
   board using the existing export preparation. With it disabled, review uses
   existing fills; unfilled or explicitly stale copper is rejected during capture.
2. Choose **Add…**, select **Net class**, and choose the engineering class for
   the signals. Set the target and transmission-line type, then configure the
   discovered routed signal layers. Set reference planes and applicable gaps,
   or explicitly exclude a layer from the workbook. Physically equivalent
   layer arrangements synchronize their settings automatically, with mapped
   reference layers and an explicit per-layer override as described below.
   Choose **Apply settings** to accept the edits into the parent window and
   close the specification editor. You can apply unfinished settings without
   reviewing captures; use **Save draft and close** in the parent window to
   persist them for later.
   To review now, inspect the capture on the right, then choose **Approve image**.
   Each click approves only the displayed image and selects the next image
   needing approval. Viewing or preparing an image does not approve it. Once
   every required image is approved, review advances to the next pending layer.
   You can also choose images directly; matching image approvals, including
   partial progress, survive applying settings and reopening the editor.
   The layer and image controls identify what still needs configuration or review.
   Completed layer review marks the net class **Approved** in the parent
   Review column. When every matched net class has current layer reviews, the
   workbook is approved automatically—no separate Approve click is required for
   that path. Reopening **Edit / Review…** restores viewed images and approved
   layers when fingerprints still match; otherwise those rows need review again.
   Missing values and unavailable previews are shown inline. Switching layers
   retains edits; **Cancel** discards the specification editor's changes.
   All actual widths are included;
   a width change at a neckdown or layer transition does not hide that copper.
   Matching is net-class-only; no width or net-name filter can silently hide
   part of the selected class. Retired filtered configurations require the
   explicit **Reset settings** action, then recreation using
   net classes and fresh review; loading never broadens their selection.
3. Workbook rows populate automatically when the main dialog opens and after
   accepting specification changes. The rows list uses most of the main pane
   (about three-quarters of the width) as a multi-column table: include
   checkbox, row number, signal layer, net class, target Ω, centerline length,
   net names, width (or **Covered stub**), target width, and segment count.
   When a current calculated target differs from the routed width by more than
   0.01 mm, `!=` appears between the two widths; both values and the symbol turn
   amber. The workbook uses the same grid content measurement as the variants
   table. Columns fit their headers, text, and checkbox artwork with a compact
   margin, resizing as rows refresh. **Incl.**, **Row**, and **Target Ω** keep
   those content widths when the window gets wider.
   Alternating row backgrounds use the same theme-aware shading as the variants
   part list; selection, muted covered stubs, and amber width warnings remain visible.
   Click an inclusion checkbox or press Space on the selected row to toggle it.
   Checked rows appear first; unchecked rows, including the covered stubs, stay
   available at the bottom. Rows keep their existing order within each group.
   Changing a checkbox moves its row into the appropriate group while retaining
   its selection and preview.
   Arrow keys move through the table; Tab at the last column and Shift+Tab at
   the first column continue to the surrounding dialog controls.
   The **Row** column numbers every entry from 1, including unchecked rows and
   covered stubs. Numbers stay the same when toggling inclusion or reopening an
   unchanged board; they are recalculated when the matching entries change.
   Grouping by inclusion can therefore display nonconsecutive row numbers.
   The remaining strip is a compact thumbnail preview; click it to open a large
   image for inspection without changing export inclusion. The first relevant
   row is selected for preview; after adding or editing a specification, its
   rows are preferred.
   Select a candidate to preview the PCB image with its selected traces enclosed
   by a yellow box. Check the **Incl.** boxes for sections that belong in the
   report. Single-ended regions grow around nearby pieces of each signal while
   preserving readable detail. Redundant contained regions can share a row that
   lists every represented net. Each KiCad-recognized differential pair
   automatically becomes one row and one yellow box per physical layer and actual width.
   Multiple pairs in the same class remain separate; no manual combining is needed.
4. The specifications list includes a **Review** column: **Needs configuration**
   for unfinished settings, **Needs approval** for net classes with complete
   settings and matching copper that still need layer/image review,
   **No matching rows** when the class has no matched copper, and **Approved**
   after that class's specification dialog review is current. Pending Review
   cells use white text on a darkish yellow background; **Approved** uses white
   text on green. Use **Edit / Review…** to open the specification dialog for
   the selected class. Below the workbook rows, **Needs approval** accompanies
   **Approve N workbook rows** when include-set changes or a draft still needs
   an explicit workbook stamp; completing **Edit / Review…** for every matched
   class approves the workbook automatically. Choose **Approve N workbook rows**
   after changing include checkboxes without reopening the specification editor,
   then **Save and close**. While review is incomplete, the dialog labels that
   action **Save draft and close** so it is clear generation still needs
   approval. A green check and **N workbook rows approved** identify the
   completed review. Approval and export require every specification to have at
   least one included section; an unfinished draft can be saved before then.
   The dialog checks the live board again before accepting approval. If the
   board changed, it refreshes the rows and requires another review instead of
   approving the changed rows in that click.
5. Generate the fabrication files. When a fabrication layer count is selected,
   it must match the PCB's complete copper stack. Generation prepares copper
   zones before checking the saved review, matching **Configure…**. If approval
   is missing or no longer current, Generate opens the configuration dialog so
   you can scan and approve; it does not treat that recoverable state as a
   fabrication-data failure. After you save a current approval, generation
   continues; cancelling or leaving review incomplete stops export cleanly. Use
   the **Review** column to see which net classes still need included rows.

You can choose **Save draft and close** before completing configuration or report
review, including after selecting only a stackup. This saves the selected stackup
and both complete and unfinished specifications in the existing board database
so you can reopen **Configure…** and continue later. A specification still needs
a selected net class and a recognized transmission-line type. Its target may be
blank, its reference selection may be empty, and required dimensions may be left
blank. Supplied values must be valid: a malformed target, nonnumeric gap, or
nonpositive dimension prevents applying the edits and identifies the field while
keeping the working copy open. Missing values mean unfinished configuration,
never an implicit layer exclusion.

**Apply settings** accepts the specification into the parent working copy; it
does not write the database. **Save draft and close** writes that working copy,
including blank required fields, without requiring captures or calculator
responses. Neither action approves the report or creates approval timestamps.
Complete layers can be calculated while unfinished layers wait for configuration.
If controlled impedance is enabled, fabrication export remains blocked until all
required settings and review are complete; saving a draft never silently disables
the report. Closing the dialog cancels any in-flight width refresh. Database
failures keep the dialog and working copy open so edits can be retried.

Opening review does not save the PCB. **Cancel** in the specification editor
discards edits since that editor opened. **Cancel** in the parent window discards
its unsaved impedance settings, including specifications already accepted with
**Apply settings**, but does not undo the in-memory refill. Image navigation and rendering
do not refill copper; a later export still performs its normal preparation.

If a copper zone has no fill, **Empty copper zones** lists each affected zone and
its layer and board coordinates. Empty generated teardrops are logged at INFO
without opening this dialog when KiCad marks them filled with no refill required
and their boundaries can be read. The log retains their identifier, location,
dimensions, and zero-copper fill status. This is a reporting policy; these items
can still contain no filled copper. Ordinary empty zones, unfilled or stale
teardrops, unknown native flags, and unreadable boundaries still require review.
When KiCad exposes the item type, a teardrop entry explicitly says
**generated teardrop**. Entries include a short identifier; the selected item's
details show its full identifier,
zone name when available, outline dimensions in millimetres, and fill status.
The status distinguishes a zone that remains empty after this operation's refill
from existing fill data inspected with refilling disabled. KiCad's overall zone
fill/refill flags are shown separately from the missing copper on the listed layer.
Opening the plugin does not check copper. Configure (including when enabling
controlled impedance opens Configure) and Generate perform this preparation,
reading the current flags each time without saving dismissals.

Select an entry to see its stored boundary highlighted on the board, then use
**Zoom to zone** or **Show whole board** to inspect its location. A physical scale
bar and the outline dimensions show the item's actual size at either zoom level.
The outline identifies the intended region, including any separate contours or
protrusions; other tracks or pads can still occupy that region. Missing fill data
alone does not establish why the item is empty. Choose **Cancel Review** to
return to the PCB and address it, or **Continue Anyway** to review the current
board. Reopening **Configure…** checks the current fills again.
When **Configure…** opens during Generate, it reuses that operation's prepared
copper; empty zones are decided once in the export findings review.

An unchanged, previously approved configuration restores its workbook rows,
saved checked-row selection, and approval when reopened. Population preserves the
selected row during harmless updates. The rows also update automatically after
accepted specification additions, edits or removals and meaningful stackup or
calculation changes. There is no manual **Rescan / reset review** step. If row
population fails, an inline explanation and contextual **Retry** replace an
unexplained empty pane; an empty configuration instead prompts you to add a
specification.

The main dialog appears before preparing its captures. Saved capture evidence
is verified incrementally after the first paint, with the selected preview given
priority. Approval remains pending and saving is unavailable until this check
finishes; preparation does not create new views or approvals. Closing the dialog
cancels its remaining work. Unchanged opening reuses one matching analysis and
updates review labels without rebuilding both tables repeatedly.

For an unchanged approved configuration, **Save and close** checks the live
source and appearance once, reuses verified capture evidence, and writes the
settings. It does not repeat matching or render evicted previews. A changed
source refreshes the rows and requires review; a changed appearance invalidates
the affected image evidence. Failed saves keep the working copy open.

Changing physical settings, checked sections, or the board invalidates the old
report approval. Hidden footprint metadata such as LCSC assignments preserves
review unless visible text uses its value. Changing field visibility or the
displayed value still requires review. Approvals from earlier feature revisions
require one fresh review; saved settings and historical timestamps are retained.
Accepting an unchanged specification or refreshing unchanged
metadata does not unnecessarily reset final report approval. Automatic row
population alone neither approves the report nor records an image view.
Historical image-view and layer-approval timestamps are retained. Unapproved
checkbox exclusions are session-local: save the completed review to retain the
final row selection. **Cancel** discards only this dialog session's edits, including
an unsaved stackup selection. Saving happens before the dialog closes, so database
errors, revision conflicts, and board-identity failures leave the edits visible
instead of losing the selected stackup.

SQLite access uses the existing connection with no lock wait. A competing write
therefore reports a busy database promptly; retry Save after the other operation
finishes. Optional catalog and calculator cache failures do not disable saved
configuration controls or replace authoritative settings with defaults.

New specifications require a net class, without automatically selecting
`Default`. KiCad can include `Default` as a fallback even for nets with other
classes, so choosing it can include unrelated signals. Unassigned copper is not
selected by net-class matching. A missing or renamed class, unavailable native
membership information, overlapping specifications, or a newly routed layer
without settings/exclusion requires correction before approval. Only layers with
current routed traces in the selected class participate in configuration and
review. Saved signal-layer settings with no matching routed traces cannot block
**Apply settings** or saving. Accepting the specification removes these obsolete
signal-layer settings and their review records from the working copy; cancelling
preserves the originals. Reference-plane selections are not removed just because
those layers have no signal traces. If routing later returns to a removed signal
layer, it requires configuration and fresh review.
On KiCad bindings that expose class constituents only as an opaque vector,
assigned class names containing literal `*` or `?` require renaming or native
membership support: the available native membership predicate treats those characters as
wildcards, so the plugin refuses to guess their literal membership.

`Default` uses the same automatic width discovery as every other net class; it
does not switch to manual width matching. If a differential specification selects
unpaired nets, the preview explains the incompatible selection. Select a class
containing complete pairs, assign a more specific class in KiCad, or choose a single-ended type
when appropriate. The plugin never silently omits unpaired nets.

Missing or invalid required inputs have red field labels plus **Required** or
**Invalid** text. The indicators follow the active type and layer,
and clear when corrected; automatic widths and inapplicable fields are not marked.
Missing fabrication dimensions do not prevent an otherwise valid route preview.
An unavailable nominal width also colors its explanation as a warning. This
persists during automatic retry and clears when inputs change or a calculation
succeeds. Initial calculations, including a slow pending request, stay neutral.

Targets remain explicit designer input, defaulting to **50 Ω single-ended** and
**90 Ω differential**. KiCad 10 tuning profiles can associate impedance targets
and per-layer geometry with net classes. This implementation does not import
profile values: the installed Python bindings do not reliably expose their live
payload. Enter and confirm the values in the plugin; it does not guess a profile
from another project or treat net-class clearance as a differential/ground gap.
Unsaved edits to a profile's opaque per-layer payload under the same name are
not guaranteed to invalidate review; saved adjacent project changes are detected.
The plugin's manually confirmed layer values remain authoritative for its form.
See [KiCad's tuning-profile documentation](https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html#tuning-profiles).

Specification dimensions can be entered in **mm** or **mil**. Entered dimensions
are stored as integer nanometres, while actual routed widths are discovered
automatically and displayed in mm/mil. To select a narrower set of signals,
assign those signals an appropriate net class in KiCad. The plugin does not
change native class assignments or route geometry.

New specifications suggest adjacent copper layers in physical stack order: one
neighbor for an outer layer, both above and below for an inner layer. The
checklist can override these defaults. Manual choices are preserved on save and
reopening; **Use adjacent layers** restores automatic selection.

Within a net-class specification, layers with equivalent physical constructions
automatically share differential spacing, coplanar ground gap, and corresponding
reference selections. The editor identifies linked layers and provides an
explicit per-layer override. For example, on a symmetric six-layer construction,
**F.Cu → In1.Cu** corresponds to **B.Cu → In4.Cu**. Inner-layer arrangements can
also be linked when their construction and references are equivalent. Missing
construction data or asymmetric arrangements do not produce guessed links.
Existing differing saved values are retained as overrides. Link groups and
overrides survive saving and reopening; a stackup change recomputes equivalence
without overwriting existing settings.

Inclusion and capture approval stay separate for each layer. Changing linked
physical settings invalidates the affected layers' approvals, and sharing settings
does not approve another layer's captures. Changing signal layers restores the
selected layer's settings and review state, including any override.
Adjacent copper is only a starting point, not proof of a ground plane.
Reopening the specification editor retains saved values and restores
**approved** / **viewed** markers when stored fingerprints still match the
current board, settings, and captures. Stored dimensions alone are not evidence
of current visual approval: a changed board, setting, or capture returns those
layers and images to **needs review**. A failed or unavailable capture cannot be
approved as though it had been shown.

The editor opens before generating captures. After its first image is displayed,
it prepares the remaining review images so they are ready when selected, starting
with the current signal layer. Preparing an image does not mark it viewed or
approve it. Saved approvals stay pending until their current images have been
verified. Native capture work yields between images; a single slow capture can
still briefly pause the interface. Closing the editor stops preparation.

Select an image from the list above the preview using the mouse or keyboard.
Ordinary images use normal text, covered stubs stay muted, and checkmarks plus
**approved** labels identify explicit image approvals. Images that were only
displayed are labeled **viewed**. Row colors follow the current
light or dark system appearance.

Failed previews and background captures retry automatically after one second,
then two, four, eight, sixteen, and at most thirty seconds between attempts.
Errors stay visible while recovery is pending. Selecting another image gives it
priority; changed capture inputs and closing the editor cancel obsolete work.
Generating or retrying an image never substitutes for a successful visible
display or an explicit image approval.

Reference planes selected for currently routed signal layers must still exist in
the enabled board stack. Editing reports missing active references without
discarding the saved configuration. Unused saved signal-layer entries, including
layers removed from the board stack, are left out of the private edit and are
removed only when the changes are applied and saved.

Choose at least one reference layer distinct from the signal layer. Differential
types require **Differential pair spacing**, the edge-to-edge gap between the two
traces. Coplanar types also require **Coplanar ground gap**, the edge-to-edge gap
between the signal copper and coplanar ground. These are separate dimensions for
coplanar differential pairs. The output converts dimensions to mil and copper
layers to physical stack numbers, **L1** through **Ln**. Its spacing cell contains
both labeled dimensions when both apply. The supplied template's impedance
heading retains its ±10% wording.

Single-ended straight tracks and arcs are grouped by specification, physical
layer, actual width, and net. Nearby pieces can share a region even when their
endpoints are separated by a pad. A whole nearby group is preferred when its
strokes and gaps remain readable at the exported image scale; otherwise the
algorithm grows smaller regions while limiting unrelated routing and pad/via
clutter. Every native track belongs to exactly one resulting row.
After growth, fully contained regions with compatible specification, layer, and
width are consolidated, preserving all member tracks and net labels. A contained
closeup remains when consolidation would make previously readable detail too
small. Partially overlapping boxes remain separate.
Differential matching uses KiCad's native `BOARD.DpCoupledNet`
identity, not proximity or shared class membership. Both members must match the
selection and have matching layer/width coverage. Missing mates, asymmetric
coverage, or unavailable native identity require correction before approval;
the plugin does not export a half-pair as a differential row. All matching copper
of one pair on a layer at one width shares a row, including disconnected islands
on that pair. Vias are context, not width matches or cross-layer grouping bridges.
Branches, including junctions between different trace widths, and zero-length
tracks produce review warnings that name the layer and net (never opaque section
digests). Tracks whose copper lies entirely inside a
same-net pad or via flash are listed as covered stubs: the Width column shows
**Covered stub**, the Target width and Segments cells stay blank, the
centerline length remains visible, the row stays previewable for review
(click the thumbnail to enlarge), and the include checkbox cannot put that
row in the JLCPCB workbook export. In the impedance specification dialog,
image-list entries for those sections are likewise labeled
**Covered stub** (muted) so they are obvious as not default export candidates.
By default, **Skip covered-stub images during impedance review** (plugin
Settings) skips those entries during layer image approval; uncheck it to stop
on each stub. Stubs remain listed for optional manual inspection in either
mode.
Mixed routes keep their
uncovered copper as a normal row and split the covered segments into excluded
rows. A short controlled-impedance run that is not fully under pad/via copper
remains exportable; length alone never excludes a row. All workbook rows show
centerline length so short stubs are obvious next to long runs. Containment
includes the full trace width and round end caps, using the actual rotated
rectangle, rounded rectangle, oval, or custom pad perimeter, including holes.
Native arcs require an outer stroke outline copied from KiCad; sampled centerlines
alone cannot prove containment. Native pad outlines use an inner approximation
and native arc strokes an outer approximation, so uncertain edge-touching cases
remain reviewable. Routing also remains exportable when precise per-layer pad
or via flash geometry is unavailable or coverage between separate flashes cannot
be proven. Updates to this containment policy require a new review while retaining
historical row identities and recorded review times. After upgrading to this
policy, saved image views and layer approvals require fresh verification,
including unchanged straight routes. Native pair
identity does not establish electrical coupling or validate the entered spacing.

Native tracks and arcs remain whole; a long route made of multiple tracks can
receive several readable regions. An indivisible track that falls below the
readability target remains listed with a warning. Length never causes copper
to be discarded. Differential rows enclose both complete member routes together.

The main view starts with the entire segment plus a margin of at least 2 mm or
15% of its longest extent on each side, and a minimum of 15% of both board
dimensions when outline bounds are available. The aspect-fitted view is then
15% tighter in both dimensions, making the copper larger without changing image
resolution. Its minimum board-relative context is therefore 12.75%; the view
expands only if needed to keep the complete route and its highlight safely
inside the frame. Each 800×420 image is one
full-frame board-context crop, without a separate locator. The background from
the user's configured PCB Editor theme is retained. Represented native trace
colors mix 30% toward white or black. The mixed color with greater background
contrast is chosen using linear luminance, brightening or darkening traces as
needed while preserving colors that already have greater contrast.
Surrounding active-layer copper mixes 50% toward the background. These color
changes preserve native geometry, physical stroke widths and transparency while
making represented traces easier to distinguish. Inactive copper is omitted, not
drawn through the active layer's ground pour. Tracks, filled zones, pads and vias
come from the selected layer's native
plot only; component outlines, silk, references, values and board edges provide
dimmed non-copper context. Through-hole pads and vias remain visible where they
exist on the selected layer. This composition is not an exact native editor
canvas screenshot. A fully opaque, bright yellow (`#FFFF00`) rectangular box with a
3-pixel stroke sits 16 image pixels outside the selected copper without repainting its center.
The boundary is screen-visible as zoom
changes, not additional copper or a scale-accurate clearance indication.

Without valid board-outline bounds, the full image still shows the complete
segment with the same tighter framing and safe highlight fit. The scan and workbook each have a 1,000-section
count limit; this does not limit route length. Use more specific net classes if
a scan exceeds that count. Existing reviews from the former tiled capture policy
require fresh approval of the automatically populated rows. Manual section
combining is no longer stored: readable signal regions and native differential
pairs determine the rows automatically.

The review digest also records the capture-policy revision. Approvals from before
the region-growth or surrounding-copper emphasis changes require approval of
the newly populated rows, while saved targets, layer settings, exclusions and
matching selections are retained.

Meaningful specification changes or changes to checked sections require approval
again; repopulating unchanged rows does not. Board changes invalidate the saved review,
including changes to surrounding copper, zone definitions, stack information,
title/properties, and live-resolved board/project text. Net-class mode also
fingerprints native class membership and available class constraints, including
tuning-profile names; class reassignment is not inferred from widths.
Group/generator membership and adjacent native `.kicad_dru` rules and
`.kicad_pro` design settings, net settings, and text variables are included too.
Save project/rule edits before reviewing. Since the native API does not reliably
expose the active project's path, all directly adjacent definitions are included
conservatively; unrelated project metadata and view state are ignored. Unreadable
or malformed definitions block review with an error rather than being skipped.
Changed filled geometry or fill-readiness flags invalidate captures and review;
refilling to identical geometry/readiness need not do so. **Cancel** keeps
the previously saved configuration. Unchecking the toolbar option disables the
form while retaining specifications and review information for later use.

## Review timestamps

Both the specification editor and final section-review dialog record the latest
successful visible display of each workbook-image preview. The specification
editor additionally records approved image fingerprints and the latest approval
time for each signal layer. A layer stays pending until all its required images
are approved. Its image list uses checkmarks and green text for approved ordinary
images, normal text for images needing review, and muted text for covered stubs.
Native row selection identifies the current image. Image
view dates are intentionally absent from the review UI; the layer's **Layer
approval** field can show **Approved at** or **Previous approval**. The settings
form scrolls independently of its separated, fixed approval footer. Reopening
the specification editor restores current image/layer review when fingerprints
still match; otherwise it starts a fresh review. Older configurations show
**Not recorded**; loading them never invents a viewing or approval date.

Each view is tied to its exact capture, including the source board, selection,
rendering policy, and actual PNG bytes. Layer approvals additionally identify the
target, type, reference planes, gaps, and complete set of reviewed captures on that
layer. The HTML companion shows both timestamps in UTC and labels whether they
describe the current capture/settings or previous ones. A changed image can make
an old timestamp historical without erasing it. Historical records do not grant
current image review or layer approval. The vendor XLSX template is unchanged.
All of a layer's approved captures must be present and match the export before
any row describes that layer approval as current. If rows are omitted from the
report, the layer's earlier approval is conservatively shown as historical.

Revisiting an image or successfully refreshing it records a new display time;
resizing, repainting, populating rows, or exporting does not. Automatic selection
of a row can display its preview; that successful visible display is recorded
separately from population. **Approve image** records approval of only the current
successfully displayed image. **Apply settings** preserves partial approvals
without approving other viewed images or restamping an
unchanged already-approved layer. Excluding a layer does not count
as approving it.

Both kinds of timestamp remain private drafts until the dialogs in which they
were edited are accepted and the outer configuration save succeeds. **Cancel**
in either dialog leaves its previously saved data unchanged. Records contain
only the latest event per logical image or layer, not a full event history or
user identity. Accepting a specification removes image records for routes no
longer in it; deleting a specification removes its tracking. They use the computer's clock, are not a tamper-proof audit log,
and a display timestamp is not proof of human inspection.

## Project storage and board identity

Configuration lives in the existing `jlcpcb/project.db`. There are no per-board
configuration sidecars. Each saved PCB has an opaque board ID associated with its
path relative to the project. Impedance specifications, selected stackups, and
review records are scoped to that ID. The downloaded stackup catalog is shared
within the project. Part assignments remain authoritative on the PCB, including
native design-variant fields. Generation counters and notice metadata retain their
project-wide SQLite storage. Supplier corrections and preferences retain their
existing library storage; controlled impedance does not migrate these domains.

Opening the plugin reads existing impedance settings without creating tables or
registering a board. A project without impedance storage stays unchanged until
settings or a downloaded stackup catalog are saved. Existing settings are restored
read-only, including when the PCB uses design variants.

A copied or renamed PCB with a new filename starts with disabled, empty impedance
settings. It does not inherit specifications or visual approval. A whole-project
directory move preserves relative identities and their saved settings. After
**Save As**, close and reopen JLCPCB Tools; an already-open window rejects the
changed filename.

Legacy assignment tables can coexist with impedance data. Controlled impedance
does not read, convert, downgrade, or remove them. The existing assignment
recovery workflow remains separate from impedance configuration; the ordinary
parts adapter reads current PCB fields rather than restoring assignments from
those tables.

Impedance payload version 6 stores net-class-only intent, frozen stackups,
compact nominal-width results, review tracking, unfinished settings, and
layer-linkage metadata. Storage validation accepts missing required settings;
calculation, approval, and export validation still require complete valid inputs.
A missing target is stored as an empty string, no chosen reference planes as an
empty list, and missing differential spacing or coplanar ground gap as `null`.
Valid dimensions remain exact integer nanometres. Malformed supplied values
cannot be saved as raw invalid text. The saved specification is the current
working specification; there is no older approved copy behind an unfinished
draft that could be exported instead.

Ordinary version-4 and version-5 net-class configurations convert in memory
without a write, retaining targets, layer settings, exclusions, stackup intent,
calculation summaries, and historical timestamps. Existing differing layer values
are preserved as overrides rather than normalized on load. Version-4 conversion
also clears its former report approval and approved-row selection and discards
obsolete manual section groups; inspect and approve the automatically determined
rows again. Raw solver records are reduced to the validated result summary and
input fingerprints rather than carried into the new payload.

The first explicit save replaces the previous configuration with version 6.
**Cancel**, failed saves, and revision conflicts leave the original record
untouched. The database envelope version is independent and unchanged. No
configuration history or recovery table is created or used.

Experimental versions 1–3 and version-4 records containing retired width/net
filters are unsupported. They show an actionable error rather than creating
inactive entries or offering inline conversion. Use the existing explicit
**Reset settings** action to replace the existing configuration with disabled
defaults, then recreate specifications using net classes. The confirmation warns
that this cannot be undone. There is no implicit reset or silent broadening of
matching.

Draft saving uses the same version-6 fields and board-scoped record. An unfinished
report has no current reviewed digest or approved row IDs; valid historical
review evidence is retained without authorizing unfinished settings. No sidecar
files, separate draft table, or database schema migration are needed. Earlier
plugin revisions cannot load version-6 drafts. Use this revision or newer to
reopen them rather than resetting them in an older plugin.

Configuration saves use revision checks so a second plugin window cannot silently
overwrite newer edits. Malformed or unsupported settings remain an error and block
generation. **Configure…** offers an explicitly confirmed **Reset settings**
action that replaces the invalid configuration with disabled defaults. Reset
rejects a record that changed while confirmation was open. Recovery tables left
by earlier development versions are ignored and left untouched.

If impedance storage cannot be read, **Configure…** remains available to retry.
Generate offers **Cancel**, **Retry reading impedance settings**, or
**Generate without impedance workbook and HTML report this time**, with Cancel
selected initially. Explicit omission excludes both reports for that generation
and preserves the saved impedance settings. The next Generate requires a fresh
decision if storage is still unreadable. PCB identity and part-storage checks
remain required.

## Generated files and failure behavior

An enabled export adds two root-level files to the existing Gerber ZIP in
`jlcpcb/production_files`: `Required_impedance_control.xlsx` and
`Required_impedance_control.html`. Each included section becomes one workbook
row and one HTML section, in the same order and using the same captured 800×420
PNG. The HTML report shows net and specification names, the signal and reference
layers in both KiCad and physical L1…Ln notation, impedance targets, and trace
widths and applicable gaps in mm and mil. Its images and styles are embedded;
it opens offline after extraction without any supporting files or scripts.
Both reports remain usable after their temporary source images are deleted.
A disabled export contains neither report, including after an enabled export.
In the XLSX, Trace Width and Trace Spacing mil values are rounded to at most
four digits after the decimal.

The bundled resource is the exact supplied `Required_impedance_control-2.xlsx`,
stored as `impedance/resources/Required_impedance_control.xlsx`. Its SHA-256 is
checked before generation. The openpyxl writer replaces the example rows and
drawings and extends the report and print range. The preservation contract is
the template's content and visible layout, including headers, styles, comments,
columns, and print settings—not byte-identical XML or unchanged ZIP members.
Visual workbook interoperability checks for this writer remain deferred.
A different vendor template requires an explicit writer/contract update;
replacing the resource alone will fail validation.

The plugin bundles pure-Python `openpyxl` 3.1.5 and `et-xmlfile` 2.0.0. A thin
PNG image adapter embeds the already captured bytes without Pillow or image
conversion. Excel, Numbers, and separately installed Python spreadsheet packages
are not runtime requirements. Both writers consume the same immutable report rows
and captured-image bytes; they do not independently reread source image paths.

Preflight prepares copper, then checks settings, current approval, layer count,
template integrity, and report values before further fabrication work. When
approval is missing or stale, Generate opens **Configure…** for review instead of
failing with an error dialog. The board and configuration are checked again after
pre-generation operations and after both reports are rendered. Missing matches,
invalid values, unsupported templates, rendering errors, or workbook/HTML failures
stop export rather than omitting either enabled report.

Before either report writer runs, each generated PNG is checked against its
latest saved image evidence. A current layer approval supplies evidence only
when no image event exists. A changed image stops generation with an instruction
to reopen **Configure…**; historical timestamps alone cannot authorize it.
Older configurations without image evidence remain compatible. Current source,
output variant, actual report board, and appearance are checked again before
archiving and, for staged variant output, before publication.

Previews, SVG plots, and source PNGs use temporary directories that are cleaned up
after success, cancellation, or failure. These standalone assets and the source
template are never added to the Gerber ZIP; PNGs are embedded inside the reports.
The archive uses an explicit file list and is validated
before atomic replacement. A failure before ZIP publication preserves the prior
ZIP; individual Gerber/drill files may already have been regenerated. Later BOM,
CPL, or post-generation hook failures are outside the ZIP publication transaction.

## Implementation and validation

The feature is isolated in `impedance/`:

- `model.py` and `matching.py` define validated immutable data, complete-route
  matching, automatic row identity, and fingerprints without KiCad or wx imports.
- `database.py` adds impedance-only board identity, configuration, and
  catalog tables to `project.db`; `repository.py` validates stored feature data.
- `review_tracking.py` contains immutable latest-event records, strict UTC
  serialization, and capture/settings fingerprints. Tracking remains outside the
  manufacturing-selection fingerprint and does not create image sidecars.
- `dialog.py` owns specification editing and review; `dialog_preview.py` shares
  `WorkbookPreview` and its `PreviewCanvas` between the specification and main
  dialogs. `integration.py` connects controls to the main window and generation
  checks and owns revision-scoped capture reuse through `CaptureSession`. A
  bounded capture cache reuses native plots and PNG bytes only for the same board
  and appearance revision; changed source, filled geometry, fill readiness, or
  palette invalidates reuse. Capture never refills or edits the live board.
- `stackup_model.py`, `jlcpcb_stackups.py`, and `stackup_dialog.py` hold frozen
  vendor catalog data, bounded JSON retrieval, and the native selection workflow.
  `catalog_cache.py` handles the successful-check timestamp and daily freshness
  policy independently of board intent. `catalog_controller.py` shares one
  background check between the main dialog and its stackup selector, starting
  when the main dialog opens rather than waiting for the selector. The selector
  requires that parent-owned controller; it subscribes to updates and detaches
  on close without owning a second request lifecycle.
  Temporary catalog failures use bounded retries and a quiet automatic refresh;
  `catalog_refresh.py` coordinates in-flight requests and cooldowns across dialog
  lifetimes without persisting failed checks as successful freshness metadata.
  `stackup_construction.py` displays the in-memory schematic cross-section;
  `palette.py` reads saved KiCad copper colors without changing settings.
  `stackup_text.py` formats the selected construction for explicit clipboard copy.
  `calculation_geometry.py` maps reviewed dimensions to provider models;
  `jlcpcb_calculator.py` correlates asynchronous vendor results, and
  `width_checks.py` provides nominal dimensional comparisons. See
  [Stackup selection and width checks](stackup-width-checks.md).
- `pcbnew_adapter.py` extracts the live board into plain records. `render.py`
  plots the native layers off-screen with `pcbnew.PLOT_CONTROLLER`, composites
  their native colors, adds the annotation, and rasterizes through KiCad's
  `wx.svg`. `board_copy.py` takes an independently owned native snapshot of
  current in-memory items and fill caches. Plotting operates on that copy, not
  the live board or canvas. Copper layers share a non-mirrored top-view orientation.
- `net_metadata.py` reads native class membership, constraints, and pair identity
  without changing assignments. `board_copy.py` also scopes temporary native
  board lifetimes without promoting borrowed SWIG wrappers to Python ownership.
  `text_context.py` preserves resolved live text on independent copies, and
  `palette.py` shares theme lookup and color decoding while keeping capture
  opacity/dimming validation independent of stackup copper-color validation.
- `service.py` owns the immutable `CapturedImage` and `ReportRow` contracts and
  orchestrates the paired export, reusing the exact rows and images for both
  reports. `workbook.py` populates the known template with bundled openpyxl.
  `html_report.py` creates the self-contained offline companion with escaped text,
  embedded PNGs, and readable mm/mil dimensions.
  `fabrication_archive.py` validates and atomically publishes archive entries.

The regression suite uses the current net-class-only selection and version-6
payload contracts. Targeted checks cover impedance payload upgrades, isolation,
explicit resets, and preservation of existing parts/counter storage, exact matching and
arcs, whole-route framing and board context, automatic pairing and approval, coordinate transforms,
workbook fields/media/preservation, archive contents and failures, and main-window
generation sequencing. Pure test sources use fake KiCad/wx adapters alongside
SQLite and workbook integration scenarios. Native capture test sources plot
synthetic PCB fixtures with KiCad CLI and run the production annotation and wx.svg
PNG renderer. They decode complete PNG data and check yellow highlights, endpoint
coverage, native colors, visible component landmarks, and subdued ground planes. This supplements, rather
than replaces, the live `pcbnew.PLOT_CONTROLLER` acceptance check below. Use this
repository's `myenv` for pytest and Ruff; do not substitute another Python environment.

`test_impedance_performance_budget.py` measures real matching, configuration
load/write, constructors, handlers, queued work, and dismissal using a 492-trace,
four-net-class fixture with connected routes, multiple layers, arcs, neckdowns,
and covered stubs. Opening and Save-and-close must each take at most 250 ms in
every one of three samples; failures include per-function times and call counts.
Cold/warm caches, draft/approved settings, ordinary/named output variants, and
evicted preview bytes are covered. Geometry measurements are shared only within
one analysis, with no persistent spatial index.

These Python budgets use explicit native boundaries and cannot establish desktop
timing. `test_impedance_performance_native.py` adds isolated native-control and
full native Configure-path checks. The latter includes real zone preparation,
snapshot extraction, raster rendering, and selected-preview readiness; remaining
background verification is measured separately. Native first-use opening must
take at most 450 ms; warm openings and every Save-and-close retain the 250 ms
maximum. A separate gate measures three fresh processes, each with feature
imports inside the opening timer and its own 450 ms limit. Run the enabled
native lane before claiming these targets on KiCad itself. A collected or skipped
native test is not evidence that its timing or lifecycle passed.

Targeted automated checks exercise pure logic, simulated KiCad/wx adapters, SQLite,
and actual workbook/HTML/archive output. Native rendering, event ordering and
layout require the separate native checks; live solver compatibility requires a
provider check. Before release, exercise this native and end-to-end acceptance checklist:

- Open a new, draft, and unchanged approved configuration: rows populate without
  a manual scan, the approved selection and approval return when still current,
  and a stackup-only draft has an explanatory empty state. Add, edit and remove
  specifications and change stackup/calculation inputs: affected rows update
  automatically, the relevant preview is selected, and meaningful changes require
  explicit approval. An unchanged edit or metadata-only refresh retains approval.
  Harmless updates retain the selected row; deleted rows cannot leave a stale
  preview. Population failure shows an explanation and **Retry**, and a successful
  retry restores rows without granting approval. Check that the approval count,
  **Needs approval**, and green approved state track the checked rows.
- Change the board between population and approval: the first click updates the
  rows without approving the changed result; a subsequent explicit click can
  approve it. Apply settings with blank target, references, or required gaps
  without reviewing captures, save the unfinished draft, and reopen it with
  completed values intact and missing values still marked as needing
  configuration. Malformed supplied values must leave the editor open with all
  edits retained. Draft saving must not invent approval or claim session-local
  unchecked rows were persisted. Cancel and save failures retain their
  transactional behavior. Saving must not wait for unavailable captures or an
  in-flight calculator response.
- First visible display, direct selection, next-image navigation, refresh, and
  context revisits record the right image once; hidden rendering, resize,
  repaint, failed capture, and export do not create successful view evidence.
  Include the final section-review preview, not just the specification editor.
- Multi-image/multi-layer review records separate approval
  times only after successful review; **Apply settings** and exclusions do not
  restamp approvals. Image LRU eviction does not lose approved capture identity.
- Reopening and changes to source, palette, type, target, references, or gaps
  retain truthful historical dates without authorizing stale review.
- Child Cancel, outer Cancel, rejected saves, and concurrent revision conflicts
  do not alter persisted tracking. Successful save/reopen preserves exact UTC.
- Equivalent outer and inner layer constructions synchronize gaps and mapped
  references; show the linked group and preserve explicit overrides after
  reopening. Old differing settings remain overrides. Missing or asymmetric
  constructions must not be guessed equivalent. Changing a stackup must not
  overwrite existing settings, and linked edits invalidate each affected
  layer's approval without granting approval to another layer.
- An obsolete saved signal layer with no routed traces does not block applying
  settings or saving, even if it has missing dimensions. Accepting removes it
  without removing selected reference planes. Cancel preserves it. Routing
  returning to that signal layer requires configuration and fresh review.
- Ordinary version-4 loading retains intent and historical tracking but clears
  old approval without writing; version-5 loading retains its valid intent.
  The first explicit version-6 save replaces the
  prior record atomically; Cancel, conflicts, and injected save failures
  leave it untouched. Versions 1–3 and version-4 legacy-filter records require
  explicit reset and recreation, never inline conversion or implicit
  defaults. Verify separate boards, fresh settings for new filenames, whole-project
  moves, explicit resets, and strict invalid-payload rejection preserve tracking
  ownership. Repeated routing edits and specification
  removal must not accumulate obsolete records.
- HTML distinguishes missing/current/historical times against the actual
  exported PNGs and complete layer row set, including changes to another row on
  the same layer and omitted report rows, uses UTC, and does not mutate data
  or the intended XLSX layout. Native dialog labels fit and show explicit local
  timezone offsets, including daylight-saving transitions.

Captures use the board's **current native fill caches**; the renderer itself is
read-only. Plugin review prepares those fills according to **Fill zones** before
taking its initial snapshot. Standalone capture callers must supply filled boards.
The renderer rejects unfilled or explicitly flagged zones, but those native flags
are not a complete freshness check. It does not run a filler with guessed project
rules or certify copper clearance. Current filled geometry and fill-readiness
flags participate in the source fingerprint: a visual fill change invalidates
cached captures and prior review, while identical refill output can be reused.

The safe native snapshot currently requires KiCad 10's named-net serialization
and raw native file-plugin API. Unsupported runtimes fail with an actionable
error instead of dropping or remapping geometry.

Variable-bearing tables that the native bindings expose only as opaque cells,
and dimension text that changes when detached, fail explicitly rather than
silently rendering incorrect content or invoking geometry-changing setters.
Palette selection/background use saved PCB Editor preferences with native cached
foreground colors; failed preference saves or external edits can make those
sources diverge. Native plot-cleanup failures require restarting KiCad; uncertain
resources are retained until exit rather than risking premature native deletion.

### Generate example captures

For openable six-layer boards covering top/bottom/inner single-ended and
differential routing, coplanar ground, stripline, and width-changing layer
transitions, see the [RF board index](../examples/impedance/README.md) and
[fixture design notes](rf-impedance-fixtures.md). These add short and 120 mm RF
cases alongside the orientation/length gallery below. Their target impedances
are nominal test intent, not solver-certified fabrication designs.

The reproducible gallery includes a 2 mm short trace, 100/200 mm horizontal traces,
200 mm vertical and diagonal traces, a 240 mm meander, and a 150 mm differential
pair. Source boards, original KiCad plots, annotated SVGs, PNGs, a manifest, and an
HTML gallery are retained in the chosen output directory for inspection:

```sh
myenv/bin/python scripts/generate_impedance_captures.py --output /tmp/impedance-examples --kicad-cli /path/to/kicad-cli
```

This runs an installed KiCad CLI, not a build. PNG generation requires `wx.svg` in
`myenv` and a GUI-capable session. Use `--svg-only` when only native SVG plots are
needed. The regular unit tests do not retain a gallery; run the script explicitly
to produce viewable captures. The source fixtures and native integration checks
are in `tests/impedance_capture_fixtures.py` and
`tests/test_impedance_capture_examples.py`.

The RF gallery now defaults to two combined projects: a 50 Ω SMA single-ended
board and a 90 Ω USB-C differential board. Each contains 14 distinct circuits in
noncoplanar and CPWG banks. Native-filled copies, per-layer plots, dimmed context
SVGs, reviewed configuration metadata, and complete route/pair PNGs are retained.
For manual review without running DRC, fill and save temporary review copies in
PCB Editor first, then render those copies:

```sh
myenv/bin/python scripts/generate_rf_impedance_captures.py --examples /tmp/filled-review-boards --use-existing-fills --output /tmp/rf-impedance-examples --kicad-cli /path/to/kicad-cli
```

Use `--board single-ended-50-ohm` or `--board usb-differential-90-ohm` to select
one board. Isolated `--case` / `--showcase` inputs remain available after generating
legacy fixtures into a separate directory. To inspect the real wx
dialogs against a fixture without saving plugin settings, use
`scripts/preview_impedance_dialogs.py --new` or `--specification 2` in `myenv`.
Class-wide matching is the only mode. The harness accepts repeated
`--context LAYER=/path/to/native-context.svg` arguments for native-colored
previews. With cached contexts, also supply `--background-color '#001023'`, using
the **originating capture manifest's** background rather than assuming the
currently selected theme matches those files. This standalone
dialog harness does not replace an in-editor plugin acceptance check.

Release acceptance combines Python artifact checks with real KiCad execution.
Linux validation uses the existing native KiCad CI job described in
[Linux impedance testing](linux-impedance-testing.md). The checks must verify their
native prerequisites and report missing coverage as a failure, rather than
counting skipped native tests as a successful platform run. Windows testing is
outside the current acceptance scope. Use Python bindings matching the installed
Linux KiCad and wx packages.

Use this checklist when qualifying a release:

1. Open plugin review with **Fill zones** enabled and confirm filling precedes
   the first snapshot and preview. On a small board, inspect front, internal,
   and back-layer previews, arcs, meanders, complete long differential pairs,
   and yellow annotations with native colored context. Confirm subsequent
   previewing and rendering do not refill or change the live board/canvas.
   With **Fill zones** disabled, confirm existing fills are used without mutation
   and unfilled or explicitly stale copper cannot be captured.
2. Export and independently inspect the outer ZIP and embedded XLSX with Python.
   Require both root-level report files. Open the extracted HTML offline after
   removing the scratch assets, and compare its row order, layer/reference labels,
   mm/mil values, and decoded embedded PNG bytes with the workbook. Include long
   names and HTML-sensitive characters in specifications and differential nets.
   Check CRCs, expected signal/reference layers, dimensions and targets, wrapped
   pair/ground spacing, image relationships and anchors, row sizes, print range,
   and semantic preservation of the vendor template's headers, styles, comments,
   columns and print layout. XML/ZIP byte identity is not the contract. Decode the embedded PNGs and verify
   their complete route/pair coverage; counting images alone is insufficient.
   No Excel or Numbers installation is required for generation or these checks.
3. Edit the board after approval and verify generation requires another review.
   Refill unchanged zone definitions: identical geometry/readiness may retain
   review, but changed filled geometry or fill readiness must invalidate captures
   and approval, including within an already-open dialog. Confirm previewing
   never invokes a filler or changes the live board. Disable the feature and
   verify the next ZIP omits both reports. Inject
   HTML/image-write failures and confirm no replacement ZIP, generation-count
   increment, or post-generation hook occurs; the previous ZIP must remain intact.
4. Exercise two boards sharing a directory, fresh impedance settings for copied
   or renamed filenames, and preserved settings after a whole-project move.
   Confirm existing parts and counters are unchanged. Exercise an installed
   plugin package to confirm the template resource is present.
5. Exercise real wx construction, event handling, preview resize, cancellation,
   close/destruction, and saved-configuration reopening. CLI plots and stateful
   wx doubles do not establish native dialog lifecycle behavior.
   In specification review, check one-image and multi-image layers, direct image
   selection, and the separate click needed to approve after revealing the last
   image. Verify image-list markers survive unchanged layer navigation, while
   failed refreshes and source/appearance changes invalidate the relevant layer
   approvals. A kind change must also invalidate class approvals.
   Hidden constructor-time images must not count as viewed, and deferred paint
   callbacks must be harmless after navigation or destruction.

Save new or migrated projects before reviewing: KiCad can materialize previously
implicit project defaults during an export's native save. That changes the
conservatively fingerprinted project definitions and requires approval of freshly
populated rows, even if the explicit route widths and gaps are unchanged. The next export
must succeed once the saved project is stable. A post-preparation validation
failure must be reported as validation, not as a DRC failure, and must not be
silently approved or publish a replacement archive.

Opening a generated workbook in Excel or another compatible viewer is an
additional interoperability check when one is available: inspect its visual
layout, print preview, and any repair prompt. It is not a runtime dependency or a
substitute for automated artifact validation. Likewise, native GTK/Xvfb results cover
the tested Linux/X11 runtime, not every desktop/window manager, Wayland setup,
KiCad release, or spreadsheet application. Record native runs separately from
mocked tests and keep unexecuted platform or installed-package checks explicit.
