#!/usr/bin/env python3

"""Use the amazing work of https://github.com/yaqwsx/jlcparts and convert their database into something we can conveniently use for this plugin.

This replaces the old .csv based database creation that JLCPCB no longer supports.
"""

import os
from pathlib import Path
import sys
import time
from typing import NamedTuple

# Add parent directory to path so we can import common module
# TODO(z2amiller):  Use proper packaging
sys.path.insert(0, str(Path(__file__).parent.parent))
from typing import NamedTuple

# Add parent directory to path so we can import common module
# TODO(z2amiller):  Use proper packaging
sys.path.insert(0, str(Path(__file__).parent.parent))

import click

from common.componentdb import ComponentsDatabase
from common.filemgr import FileManager
from common.jlcapi import CategoryFetch, Component, JlcApi
from common.partsdb import Generate, PartsDatabase
from common.progress import PrintNestedProgressBar, TqdmNestedProgressBar


class PartDatabaseConfig(NamedTuple):
    """Configuration for part database generation."""

    name: str
    chunk_file_name: str
    where_clause: str
    populate_preferred: bool = False


class DatabaseConfig:
    """Predefined database configurations."""

    @staticmethod
    def preferredAndBasic() -> PartDatabaseConfig:
        """Select only preferred and basic parts."""
        return PartDatabaseConfig(
            name="basic-parts-fts5.db",
            chunk_file_name="chunk_num_basic_parts_fts5.txt",
            where_clause="basic = 1 OR preferred = 1",
            populate_preferred=True,
        )

    @staticmethod
    def allParts() -> PartDatabaseConfig:
        """Select all parts.

        This is the most backwards-compatible database, and therefore uses
        the default "parts-fts5.db" name.
        """
        return PartDatabaseConfig(
            name="parts-fts5.db",
            chunk_file_name="chunk_num_fts5.txt",
            where_clause="TRUE",
            populate_preferred=False,
        )

    @staticmethod
    def ignoreObsoleteParts(obsolete_threshold_days: int = 365) -> PartDatabaseConfig:
        """Select all parts except obsolete parts."""
        filter_seconds = int(time.time()) - obsolete_threshold_days * 24 * 60 * 60
        return PartDatabaseConfig(
            name="current-parts-fts5.db",
            chunk_file_name="chunk_num_current_parts_fts5.txt",
            where_clause=f"NOT (stock = 0 AND last_on_stock < {filter_seconds})",
            populate_preferred=True,
        )

    @staticmethod
    def emptyParts() -> PartDatabaseConfig:
        """Select no parts."""
        return PartDatabaseConfig(
            name="empty-parts-fts5.db",
            chunk_file_name="chunk_num_empty_parts_fts5.txt",
            where_clause="FALSE",
        )


def update_components_db_from_api() -> None:
    """Update the component cache database."""
    db = ComponentsDatabase("db_working/cache.sqlite3")
    print("Fetching categories...")
    initial_categories = JlcApi.fetchCategories(instockOnly=True)
    categories = JlcApi.collapseCategories(initial_categories, limit=50000)
    print(f"Found {len(initial_categories)} categories, collaped to {len(categories)}.")

    progress = (
        TqdmNestedProgressBar()
        if sys.stdout.isatty()
        else PrintNestedProgressBar(outer_threshold=1, inner_threshold=2000)
    )

    with progress.outer(len(categories), "Fetching categories") as outer_pbar:
        for category in categories:
            fetcher = CategoryFetch(category)

            with progress.inner(category.count, f"{category}") as inner_pbar:
                for components in fetcher.fetchAll():
                    comp_objs = [Component(comp) for comp in components]
                    db.update_cache(comp_objs)
                    inner_pbar.update(len(components))

            outer_pbar.update()

    db.cleanup_stock()
    db.close()
    def emptyParts() -> PartDatabaseConfig:
        """Select no parts."""
        return PartDatabaseConfig(
            name="empty-parts-fts5.db",
            chunk_file_name="chunk_num_empty_parts_fts5.txt",
            where_clause="FALSE",
        )


def update_components_db_from_api() -> None:
    """Update the component cache database."""
    db = ComponentsDatabase("db_working/cache.sqlite3")
    print("Fetching categories...")
    initial_categories = JlcApi.fetchCategories(instockOnly=True)
    categories = JlcApi.collapseCategories(initial_categories, limit=50000)
    print(f"Found {len(initial_categories)} categories, collaped to {len(categories)}.")

    progress = (
        TqdmNestedProgressBar()
        if sys.stdout.isatty()
        else PrintNestedProgressBar(outer_threshold=1, inner_threshold=2000)
    )

    with progress.outer(len(categories), "Fetching categories") as outer_pbar:
        for category in categories:
            fetcher = CategoryFetch(category)

            with progress.inner(category.count, f"{category}") as inner_pbar:
                for components in fetcher.fetchAll():
                    comp_objs = [Component(comp) for comp in components]
                    db.update_cache(comp_objs)
                    inner_pbar.update(len(components))

            outer_pbar.update()

    db.cleanup_stock()
    db.close()


@click.command()
@click.option(
    "--skip-cleanup",
    is_flag=True,
    show_default=True,
    default=False,
    help="Disable cleanup, intermediate database files will not be deleted",
)
@click.option(
    "--components-db-base-url",
    default="http://yaqwsx.github.io/jlcparts/data",
    show_default=True,
    help="Base URL to fetch the components database from",
)
@click.option(
    "--fetch-components-db",
    is_flag=True,
    show_default=True,
    default=False,
    help="Fetch the components db from the remote server",
)
@click.option(
    "--fix-components-db-descriptions",
    is_flag=True,
    show_default=True,
    default=False,
    help="Fix descriptions in the components db by pulling from the 'extra' field",
)
@click.option(
    "--update-components-db",
    is_flag=True,
    show_default=True,
    default=False,
    help="Update the local components db using LCSC API data",
)
@click.option(
    "--clean-components-db",
    is_flag=True,
    show_default=True,
    default=False,
    help="Clean the local components db by removing old and out-of-stock parts",
)
@click.option(
    "--archive-components-db",
    "--components-db-base-url",
    default="http://yaqwsx.github.io/jlcparts/data",
    show_default=True,
    help="Base URL to fetch the components database from",
)
@click.option(
    "--fetch-components-db",
    is_flag=True,
    show_default=True,
    default=False,
    help="Fetch the components db from the remote server",
)
@click.option(
    "--fix-components-db-descriptions",
    is_flag=True,
    show_default=True,
    default=False,
    help="Fix descriptions in the components db by pulling from the 'extra' field",
)
@click.option(
    "--update-components-db",
    is_flag=True,
    show_default=True,
    default=False,
    help="Update the local components db using LCSC API data",
)
@click.option(
    "--clean-components-db",
    is_flag=True,
    show_default=True,
    default=False,
    help="Clean the local components db by removing old and out-of-stock parts",
)
@click.option(
    "--archive-components-db",
    is_flag=True,
    show_default=True,
    default=False,
    help="Archive the components db after updating from the API",
    help="Archive the components db after updating from the API",
)
@click.option(
    "--skip-generate",
    is_flag=True,
    show_default=True,
    default=False,
    help="Skip the DB generation phase",
)
@click.option(
    "--obsolete-parts-threshold-days",
    show_default=True,
    default=365,
    type=int,
    help="""
        Setting this to > 0 will generate an additional dataabase that ignores parts
        that have been out of stock for more than the specified number of days.
    """,
)
def main(
    skip_cleanup: bool,
    fetch_components_db: bool,
    components_db_base_url: str,
    fix_components_db_descriptions: bool,
    update_components_db: bool,
    clean_components_db: bool,
    archive_components_db: bool,
    fetch_components_db: bool,
    components_db_base_url: str,
    fix_components_db_descriptions: bool,
    update_components_db: bool,
    clean_components_db: bool,
    archive_components_db: bool,
    skip_generate: bool,
    obsolete_parts_threshold_days: int,
):
    """Perform the database steps."""

    working_directory = "db_working"
    components_db = f"{working_directory}/cache.sqlite3"

    if fetch_components_db:
        print("Fetching components database...")
        fm = FileManager(
            file_path=Path(components_db),
            sentinel_filename="cache_chunk_num.txt",
        )
        fm.download_and_reassemble(
            url=components_db_base_url,
            output_dir=Path(working_directory),
            cleanup=not skip_cleanup,
        )

    if not os.path.exists(working_directory):
        os.mkdir(working_directory)

    if fix_components_db_descriptions:
        print("Fixing components database descriptions")
        db = ComponentsDatabase(components_db)
        db.fix_description()
        db.close()

    if update_components_db:
        update_components_db_from_api()

    if clean_components_db:
        print("Cleaning components database")
        db = ComponentsDatabase(components_db)
        db.truncate_old()
        db.close()

    configs = [
        DatabaseConfig.preferredAndBasic(),
        DatabaseConfig.allParts(),
        DatabaseConfig.emptyParts(),
    ]
    if obsolete_parts_threshold_days > 0:
        configs.insert(
            0, DatabaseConfig.ignoreObsoleteParts(obsolete_parts_threshold_days)
        )

    archive_dir = Path("archive")
    if not archive_dir.exists():
        os.makedirs(archive_dir)
    for config in configs:
        if not skip_generate:
            print(f"Generating {config.name}...")
            componentdb = ComponentsDatabase(components_db)
            partsdb = PartsDatabase(
                output_db=Path(working_directory) / config.name,
                archive_dir=Path(archive_dir),
                chunk_num=Path(config.chunk_file_name),
                skip_cleanup=skip_cleanup,
            )
            progress = (
                TqdmNestedProgressBar()
                if sys.stdout.isatty()
                else PrintNestedProgressBar()
            )
            generator = Generate(
                componentdb=componentdb,
                partsdb=partsdb,
                progress=progress,
                populate_preferred=config.populate_preferred,
            )
            generator.generate(where_clause=config.where_clause)

    if archive_components_db:
        fm = FileManager(
            file_path=Path(components_db),
            chunk_size=50 * 1024 * 1024,  # 50 MB
            sentinel_filename="cache_chunk_num.txt",
        )
        fm.compress_and_split(
            output_dir=Path(archive_dir), delete_original=skip_cleanup
        fm.compress_and_split(
            output_dir=Path(archive_dir), delete_original=skip_cleanup
        )


if __name__ == "__main__":
    main()
