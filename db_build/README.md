# Database Building and Conversion

The code in this directory handles building the sqlite database files
that are downloaded by the user.

## Main Concepts

### The Components Database

The components database is compatible with the 'cache.sqlite3' database that was
originally released as part of the yaqswx/jlcparts distribution. It contains
information about every component JLC has ever stocked. The components database
is effectively a local cache of all of the data available in the public JLC API.

### The Parts Database(s)

The Parts database is the database that is consumed by the UI in this package.
It is built from the Components database, but does some additional data
normalization, price compression, and uses full text search indexing to make the
component search fast in the UI. There are multiple parts databases built,
which can be selectable in the UI:

#### Recently Stocked (default)

This filters the Parts database based on parts which have been seen in-stock at
some point in the last year. Removing components that haven't been in stock for
more than one year reduces the database size by over 90%.

#### Basic and Preferred Only

This filters the Parts database to the ~1500 components that are either 'Basic'
or 'Preferred'. Useful for high speed in very simple or price-concious builds.

#### Empty

This is an empty Parts database. This is useful for users who only use the plugin
for PCB generation and do not create BOMs, or who have manually added LCSC numbers
to all of their components.

#### All Components (old default)

This is the unfiltered Parts database - all of the components which have ever been
seen at JLC are searchable.

This is much larger, and has worse performance, but may be useful for some users
who have old parts in their BOM and don't want to lose them from the search index
despite not being in-stock -- It is possible at JLC to pre-order parts which are
not otherwise in stock.

### The JLC API

JLC provides a public API for searching components - this is ultimately the source
of truth for building the cached Components database.

## The build process

The database build process is approximately:

1. Download and reassemble the components DB artifact.

1. Scrape the JLC API to update the components DB.

1. Set stock=0 on any items which have not been seen in the API scrape for more
   than 1 week.

1. For any item that has been out of stock for more than 1 year, remove the
   'extra' and 'price' information from the Components DB and compact it. This
   reduces the size of the database by > 2/3. The 'extra' information is not
   used in the Parts database, and the price information for something out of
   stock for > 1 year will be irrelevant.

1. Using the updated components database, create each of the fitlered parts
   databases.

1. Archive and split each of the parts databases into a separate directory.

1. Archive and split the components database. This allows the next run of the
   download script to have an updated copy of the components database.

1. From the github workflow, upload each of the artifact directories.
