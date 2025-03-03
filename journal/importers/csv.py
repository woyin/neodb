import csv
import datetime
import os
import tempfile
import zipfile
from typing import Dict, List, Optional

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _
from loguru import logger

from catalog.common.sites import SiteManager
from catalog.models import Edition, IdType, Item, ItemCategory
from journal.models import Mark, Note, Review, ShelfType
from users.models import Task


class CsvImporter(Task):
    class Meta:
        app_label = "journal"  # workaround bug in TypedModel

    TaskQueue = "import"
    DefaultMetadata = {
        "total": 0,
        "processed": 0,
        "skipped": 0,
        "imported": 0,
        "failed": 0,
        "failed_items": [],
        "file": None,
        "visibility": 0,
    }

    def get_item_by_info_and_links(
        self, title: str, info_str: str, links_str: str
    ) -> Optional[Item]:
        """Find an item based on information from CSV export.

        Args:
            title: Item title
            info_str: Item info string (space-separated key:value pairs)
            links_str: Space-separated URLs

        Returns:
            Item if found, None otherwise
        """
        site_url = settings.SITE_INFO["site_url"] + "/"

        links = links_str.strip().split()
        for link in links:
            if link.startswith("/") or link.startswith(site_url):
                item = Item.get_by_url(link)
                if item:
                    return item
        for link in links:
            site = SiteManager.get_site_by_url(link)
            if site:
                site.get_resource_ready()
                item = site.get_item()
                if item:
                    return item
        # Try using the info string
        if info_str:
            info_dict = {}
            for pair in info_str.strip().split():
                if ":" in pair:
                    key, value = pair.split(":", 1)
                    info_dict[key] = value

            # Check for ISBN, IMDB, etc.
            item = None
            for key, value in info_dict.items():
                if key == "isbn" and value:
                    item = Edition.objects.filter(
                        primary_lookup_id_type=IdType.ISBN,
                        primary_lookup_id_value=value,
                    ).first()
                elif key == "imdb" and value:
                    item = Item.objects.filter(
                        primary_lookup_id_type=IdType.IMDB,
                        primary_lookup_id_value=value,
                    ).first()
                if item:
                    return item
        return None

    def parse_tags(self, tags_str: str) -> List[str]:
        """Parse space-separated tags string into a list of tags."""
        if not tags_str:
            return []
        return [tag.strip() for tag in tags_str.split() if tag.strip()]

    def parse_info(self, info_str: str) -> Dict[str, str]:
        """Parse info string into a dictionary."""
        info_dict = {}
        if not info_str:
            return info_dict

        for pair in info_str.split():
            if ":" in pair:
                key, value = pair.split(":", 1)
                info_dict[key] = value

        return info_dict

    def parse_datetime(self, timestamp_str: str) -> Optional[datetime.datetime]:
        """Parse ISO format timestamp into datetime."""
        if not timestamp_str:
            return None

        try:
            dt = parse_datetime(timestamp_str)
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            return dt
        except Exception as e:
            logger.error(f"Error parsing datetime {timestamp_str}: {e}")
            return None

    def parse_shelf_type(self, status_str: str) -> ShelfType:
        """Parse shelf type string into ShelfType enum."""
        if not status_str:
            return ShelfType.WISHLIST

        status_map = {
            "wishlist": ShelfType.WISHLIST,
            "progress": ShelfType.PROGRESS,
            "complete": ShelfType.COMPLETE,
            "dropped": ShelfType.DROPPED,
        }

        return status_map.get(status_str.lower(), ShelfType.WISHLIST)

    def import_mark(self, row: Dict[str, str]) -> bool:
        """Import a mark from a CSV row."""
        try:
            item = self.get_item_by_info_and_links(
                row.get("title", ""), row.get("info", ""), row.get("links", "")
            )

            if not item:
                logger.error(f"Could not find item: {row.get('links', '')}")
                self.metadata["failed_items"].append(
                    f"Could not find item: {row.get('links', '')}"
                )
                return False

            owner = self.user.identity
            mark = Mark(owner, item)

            shelf_type = self.parse_shelf_type(row.get("status", ""))
            rating_grade = None
            if "rating" in row and row["rating"]:
                try:
                    rating_grade = int(float(row["rating"]))
                except (ValueError, TypeError):
                    pass

            comment_text = row.get("comment", "")
            tags = self.parse_tags(row.get("tags", ""))

            # Parse timestamp
            created_time = (
                self.parse_datetime(row.get("timestamp", "")) or timezone.now()
            )

            if (
                mark.shelf_type
                and mark.created_time
                and mark.created_time >= created_time
            ):
                # skip if existing mark is newer
                self.metadata["skipped"] = self.metadata.get("skipped", 0) + 1
                return True

            # Update the mark
            mark.update(
                shelf_type,
                comment_text=comment_text,
                rating_grade=rating_grade,
                tags=tags,
                created_time=created_time,
                visibility=self.metadata.get("visibility", 0),
            )
            return True
        except Exception as e:
            logger.error(f"Error importing mark: {e}")
            self.metadata["failed_items"].append(
                f"Error importing mark for {row.get('title', '')}"
            )
            return False

    def import_review(self, row: Dict[str, str]) -> bool:
        """Import a review from a CSV row."""
        try:
            item = self.get_item_by_info_and_links(
                row.get("title", ""), row.get("info", ""), row.get("links", "")
            )

            if not item:
                logger.error(f"Could not find item for review: {row.get('links', '')}")
                self.metadata["failed_items"].append(
                    f"Could not find item for review: {row.get('links', '')}"
                )
                return False

            owner = self.user.identity
            review_title = row.get("title", "")  # Second "title" field is review title
            review_content = row.get("content", "")

            # Parse timestamp
            created_time = self.parse_datetime(row.get("timestamp", ""))

            # Check if there's an existing review with the same or newer timestamp
            existing_review = Review.objects.filter(
                owner=owner, item=item, title=review_title
            ).first()
            # Skip if existing review is newer or same age
            if (
                existing_review
                and existing_review.created_time
                and created_time
                and existing_review.created_time >= created_time
            ):
                logger.debug(
                    f"Skipping review import for {item.display_title}: existing review is newer or same age"
                )
                self.metadata["skipped"] = self.metadata.get("skipped", 0) + 1
                return True

            # Create/update the review
            Review.update_item_review(
                item,
                owner,
                review_title,
                review_content,
                created_time=created_time,
                visibility=self.metadata.get("visibility", 0),
            )
            return True
        except Exception as e:
            logger.error(f"Error importing review: {e}")
            self.metadata["failed_items"].append(
                f"Error importing review for {row.get('title', '')}: {str(e)}"
            )
            return False

    def import_note(self, row: Dict[str, str]) -> bool:
        """Import a note from a CSV row."""
        try:
            item = self.get_item_by_info_and_links(
                row.get("title", ""), row.get("info", ""), row.get("links", "")
            )

            if not item:
                logger.error(f"Could not find item for note: {row.get('links', '')}")
                self.metadata["failed_items"].append(
                    f"Could not find item for note: {row.get('links', '')}"
                )
                return False

            owner = self.user.identity
            title = row.get("title", "")  # Second "title" field is note title
            content = row.get("content", "")
            progress = row.get("progress", "")

            # Parse timestamp
            created_time = self.parse_datetime(row.get("timestamp", ""))

            # Extract progress information
            pt, pv = Note.extract_progress(progress)

            # Check if a note with the same attributes already exists
            existing_notes = Note.objects.filter(
                item=item,
                owner=owner,
                title=title,
                progress_type=pt,
                progress_value=pv,
            )

            # If we have an exact content match, skip this import
            for existing_note in existing_notes:
                if existing_note.content == content:
                    logger.debug(
                        f"Skipping note import for {item.display_title}: duplicate note found"
                    )
                    self.metadata["skipped"] = self.metadata.get("skipped", 0) + 1
                    return True

            # Create the note if no duplicate is found
            Note.objects.create(
                item=item,
                owner=owner,
                title=title,
                content=content,
                progress_type=pt,
                progress_value=pv,
                created_time=created_time,
                visibility=self.metadata.get("visibility", 0),
            )
            return True
        except Exception as e:
            logger.error(f"Error importing note: {e}")
            if "failed_items" not in self.metadata:
                self.metadata["failed_items"] = []
            self.metadata["failed_items"].append(
                f"Error importing note for {row.get('title', '')}: {str(e)}"
            )
            return False

    def progress(self, success: bool) -> None:
        """Update import progress."""
        self.metadata["total"] += 1
        self.metadata["processed"] += 1

        if success:
            self.metadata["imported"] += 1
        else:
            self.metadata["failed"] += 1

        self.message = f"{self.metadata['imported']} imported, {self.metadata['skipped']} skipped, {self.metadata['failed']} failed"
        self.save(update_fields=["metadata", "message"])

    def process_csv_file(self, file_path: str, import_function) -> None:
        """Process a CSV file using the specified import function."""
        logger.debug(f"Processing {file_path}")
        with open(file_path, "r") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                success = import_function(row)
                self.progress(success)

    def validate_file(self, filename: str) -> bool:
        """Validate that the given file is a valid CSV export ZIP file.

        Args:
            filename: Path to the file to validate

        Returns:
            bool: True if the file is valid, False otherwise
        """
        return os.path.exists(filename) and zipfile.is_zipfile(filename)

    def run(self) -> None:
        """Run the CSV import."""
        # Ensure failed_items is initialized
        if "failed_items" not in self.metadata:
            self.metadata["failed_items"] = []

        filename = self.metadata["file"]
        logger.debug(f"Importing {filename}")

        # Validate the file before processing
        if not self.validate_file(filename):
            self.save()
            return

        with zipfile.ZipFile(filename, "r") as zipref:
            with tempfile.TemporaryDirectory() as tmpdirname:
                logger.debug(f"Extracting {filename} to {tmpdirname}")
                zipref.extractall(tmpdirname)

                # Look for mark, review, and note CSV files
                for category in [
                    ItemCategory.Movie,
                    ItemCategory.TV,
                    ItemCategory.Music,
                    ItemCategory.Book,
                    ItemCategory.Game,
                    ItemCategory.Podcast,
                    ItemCategory.Performance,
                ]:
                    # Import marks
                    mark_file = os.path.join(tmpdirname, f"{category}_mark.csv")
                    if os.path.exists(mark_file):
                        self.process_csv_file(mark_file, self.import_mark)

                    # Import reviews
                    review_file = os.path.join(tmpdirname, f"{category}_review.csv")
                    if os.path.exists(review_file):
                        self.process_csv_file(review_file, self.import_review)

                    # Import notes
                    note_file = os.path.join(tmpdirname, f"{category}_note.csv")
                    if os.path.exists(note_file):
                        self.process_csv_file(note_file, self.import_note)

        self.message = _("Import complete")
        if self.metadata.get("failed_items", []):
            self.message += f": {self.metadata['failed']} items failed ({len(self.metadata['failed_items'])} unique items)"
        self.save()
