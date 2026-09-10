# Importing and repairing corrections

The Corrections Manager stores rotation and position corrections for footprint,
value, or reference patterns. Corrections can use a global database or a database
in the current project's `jlcpcb` directory.

## CSV formats

The current exported format is:

```csv
Pattern,Rotation,Offset X,Offset Y
^SOT-23$,-90,0,0
^USB_C_Receptacle$,0,1.44,0
```

Historical files using `Footprint pattern,Correction` and downloaded files using
`Footprint pattern,Rotation,Offset X,Offset Y` are also supported. Recognized
columns may be reordered. The importer rejects unknown or duplicate columns
instead of guessing their meaning.

Rotation must be a finite whole number of degrees. For example, `90`, `-90`, and
`90.0` are accepted; `47u` and `90.5` are rejected. Signed values are preserved.
Offsets must be finite numbers in millimeters. Patterns must be nonempty and
valid for the plugin's regular-expression matching.

Missing trailing offsets default to zero, including two-field records beneath
a four-column header. An explicitly empty numeric cell is an error. A missing
rotation is always an error. UTF-8 files, including files with a byte-order mark,
Windows line endings, and ordinary CSV quoting are supported.

A file containing only a recognized header imports no records. Blank lines are
ignored; an entirely empty file or a row containing only delimiters is rejected.

## Import behavior

The complete file is validated before any corrections are changed. If one row
is invalid, the entire import is rejected and the existing database stays
unchanged. The error identifies the source, line, field, and invalid value.

A successful user import updates existing patterns and adds new ones. If the
same pattern appears more than once, its last occurrence wins. Every row must
still be valid. Downloading corrections preserves existing entries and adds
missing patterns only.

Database writes are transactional. A write failure rolls back earlier inserts
and updates from that operation. Correct the reported problem and import again.

## Repairing existing invalid data

Earlier plugin versions could store invalid numeric text from a CSV. Issue #531
reported a rotation of `47u`, which then prevented the plugin from reopening.

The manager shows invalid records with their original values and errors. Select
one to repair or delete it. If another window changes the selected record or a
record approved for replacement, the operation stops and retains your entered
text. Reselect the current record before retrying. Repair affects only the
selected record, even when several records have the same pattern.

The main window remains accessible while correction errors are unresolved.
Fabrication and correction CSV export require a valid active database; invalid
corrections are never omitted or replaced with zero. Fabrication also checks
transformed placement coordinates before generating files. An unrepresentable
position identifies the footprint and correction to repair, preserving existing
output files.

An empty correction database is valid. A part with no matching rule uses its
uncorrected placement. Unreadable storage or unresolved records are shown as
`Unresolved`, rather than being treated as an empty set of corrections.

## Matching and displayed corrections

The parts table and placement output use the same correction selection rules.
Reference matches take priority over value matches, which take priority over
footprint matches. For each of those fields, patterns matching the end of the
text are tried before ordinary substring matches. Ties retain database order.
For example, `SOT-23-3` wins over `SOT-23` for a footprint named `SOT-23-3`.

A matched correction of zero degrees and zero offset still takes precedence
over lower-priority rules. The table labels the match source as `(ref)`, `(val)`,
or `(fpt)`.

## Legacy data and database scope

Existing patterns in the current correction database take precedence over
legacy rotation archives. This preserves corrections already repaired or
customized after an older migration, including their position offsets. A stale
archive cannot add a conflicting copy of an established pattern.

For patterns absent from the destination, the dedicated `rotations.db` archive
takes precedence over the selected parts database's old rotation table. A
lower-priority archive cannot add a conflicting copy. Original values are
preserved, including invalid values and contradictions within the winning
archive that need repair. The source archives are retained, and imported rows
and their completion records commit together.

Migration is checked at startup, when switching to global corrections, and when
opening Corrections Manager for global corrections. Ordinary list refreshes,
exports, and fabrication checks do not reopen legacy archives. Missing sources,
empty rotation tables, and files without a rotation table are recorded as
examined; they do not block healthy corrections or need repeated examination.

If an archive cannot be read and its corrections are unknown, the manager shows
a warning identifying the file. Healthy active corrections remain usable.
Restore access to the archive and reopen Corrections Manager to retry. Automatic
imports from lower-priority sources and the initial default download wait for
this retry so they cannot take precedence over recovered custom corrections.

If corrections were identified but could not be transferred, the incomplete
transfer remains blocking across reopening. The manager identifies the source
and failure; resolve that failure and reopen the manager to retry. An archive
disappearing afterward does not make a known incomplete transfer successful.

If a custom database schema would change a legacy value during transfer, the
whole transfer is rejected and the archives remain intact. Correct the reported
storage incompatibility before retrying.

Older versions did not record whether a missing pattern was never imported,
deleted, or renamed. If an old archive remains, a missing pattern can be
restored during its first migration. Completed migrations do not replay that
archive over later repairs or deletions. Records restored after a source was
already examined can be imported explicitly using the supported CSV formats.
An interrupted initial default download resumes after recovery. Failure to start
that optional download warns without disabling healthy corrections. Reopening
does not refill corrections that were deliberately deleted.

Automatic legacy CSV imports archive the source only after the import commits.
An archive failure is reported separately from a successful database import.
Rejected CSV input leaves the current stored corrections usable. Warnings about
unreadable archives are separate from errors in active corrections and known
incomplete transfers.

Global-to-local copying completes before the active database changes. A failed
switch preserves the active scope and its data. Errors in an inactive database
do not block a healthy active database.

Switching back to global corrections discards the project's local corrections
after an explicit confirmation and a successful check of the global database.
Invalid local values do not prevent this choice. If global corrections cannot
be used or the switch fails, the local corrections and active scope remain in
place. Other project data is preserved.
