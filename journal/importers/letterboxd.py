import csv
import os
import tempfile
import zipfile
from datetime import timedelta
from random import randint

import pytz
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _
from loguru import logger
from markdownify import markdownify as md

from catalog.common import *
from catalog.common.downloaders import *
from catalog.models import *
from journal.models import *
from users.models import *

_tz_sh = pytz.timezone("Asia/Shanghai")


class LetterboxdImporter(Task):
    class Meta:
        app_label = "journal"  # workaround bug in TypedModel

    TaskQueue = "import"
    DefaultMetadata = {
        "total": 0,
        "processed": 0,
        "skipped": 0,
        "imported": 0,
        "failed": 0,
        "visibility": 0,
        "failed_urls": [],
        "file": None,
    }

    @classmethod
    def validate_file(cls, uploaded_file):
        try:
            return zipfile.is_zipfile(uploaded_file)
        except Exception:
            return False

    @classmethod
    def get_item_by_url(cls, url):
        try:
            h = BasicDownloader(url).download().html()
        except Exception:
            logger.error(f"Unable to fetch {url}")
            return None
        tu = h.xpath("//a[@data-track-action='TMDB']/@href")
        iu = h.xpath("//a[@data-track-action='IMDb']/@href")
        if not tu:
            scripts = h.xpath('//script[@type="application/ld+json"]/text()')
            if not scripts:
                logger.error(f"Unknown TMDB for {url}")
                return None
            s: str = scripts[0]  # type:ignore
            script_content = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL).strip()
            schema_data = json.loads(script_content)
            if (
                "itemReviewed" not in schema_data
                or "sameAs" not in schema_data["itemReviewed"]
            ):
                logger.error(f"Unable to parse {url}")
                return None
            u2 = schema_data["itemReviewed"]["sameAs"]
            try:
                h = BasicDownloader(u2).download().html()
            except Exception:
                logger.error(f"Unable to fetch {u2}")
                return None
            tu = h.xpath("//a[@data-track-action='TMDB']/@href")
            iu = h.xpath("//a[@data-track-action='IMDb']/@href")
        if not tu:
            logger.error(f"Unknown TMDB for {url}")
            return None
        site = SiteManager.get_site_by_url(tu[0])  # type:ignore
        if not site:
            return None
        if site.ID_TYPE == IdType.TMDB_TV:
            site = SiteManager.get_site_by_url(f"{site.url}/season/1")
            if not site:
                return None
        try:
            site.get_resource_ready()
            return site.get_item()
        except Exception:
            logger.warning(f"Fetching {url}: TMDB {site.url} failed")
            if not iu:
                logger.warning(f"Fetching {url}: no IMDB, giving up")
                return None
            imdb_url = str(iu[0])  # type:ignore
            logger.warning(
                f"Fetching {url}: TMDB {site.url} failed, try IMDB {imdb_url}"
            )
            site = SiteManager.get_site_by_url(imdb_url)
            if not site:
                return None
            try:
                site.get_resource_ready()
                return site.get_item()
            except Exception:
                logger.warning(f"Fetching {url}: IMDB {imdb_url} failed")
                return None

    def mark(self, url, shelf_type, date, rating=None, text=None, tags=None):
        item = self.get_item_by_url(url)
        if not item:
            logger.error(f"Unable to get item for {url}")
            self.progress(-1, url)
            return
        owner = self.user.identity
        mark = Mark(owner, item)
        if (
            mark.shelf_type == shelf_type
            or mark.shelf_type == ShelfType.COMPLETE
            or (
                mark.shelf_type in [ShelfType.PROGRESS, ShelfType.DROPPED]
                and shelf_type == ShelfType.WISHLIST
            )
        ):
            self.progress(0)
            return
        visibility = self.metadata["visibility"]
        shelf_time_offset = {
            ShelfType.WISHLIST: " 20:00:00",
            ShelfType.PROGRESS: " 21:00:00",
            ShelfType.COMPLETE: " 22:00:00",
        }
        dt = parse_datetime(date + shelf_time_offset[shelf_type])
        if dt:
            dt += timedelta(seconds=randint(0, 3599))
            dt = dt.replace(tzinfo=_tz_sh)
        rating_grade = round(float(rating) * 2) if rating else None
        comment = None
        if text:
            text = md(text)
            if len(text) < 360:
                comment = text
            else:
                title = _("a review of {item_title}").format(item_title=item.title)
                Review.update_item_review(item, owner, title, text, visibility, dt)
        tag_titles = [s.strip() for s in tags.split(",")] if tags else None
        mark.update(
            shelf_type,
            comment_text=comment,
            rating_grade=rating_grade,
            tags=tag_titles,
            visibility=visibility,
            created_time=dt,
        )
        self.progress(1)

    def progress(self, mark_state: int, url=None):
        self.metadata["processed"] += 1
        match mark_state:
            case 1:
                self.metadata["imported"] += 1
            case 0:
                self.metadata["skipped"] += 1
            case _:
                self.metadata["failed"] += 1
                if url:
                    self.metadata["failed_urls"].append(url)
        self.message = f"{self.metadata['imported']} imported, {self.metadata['skipped']} skipped, {self.metadata['failed']} failed"
        self.save(update_fields=["metadata", "message"])

    def import_list(self, fn):
        with open(fn) as f:
            reader = csv.DictReader(
                f,
                delimiter=",",
                fieldnames=["pos", "name", "year", "url", "desc"],
            )
            line_no = 0
            collection = None
            for row in reader:
                line_no += 1
                if line_no == 1:
                    if row["pos"] != "Letterboxd list export v7":
                        logger.error(
                            f"Unknown list format: {row['pos']}, skipping {fn}"
                        )
                        break
                elif line_no == 3:
                    collection = Collection.objects.create(
                        title=row["name"] or "no name",
                        brief=row["desc"] or "",
                        owner=self.user.identity,
                    )
                elif line_no > 4 and collection:
                    url = row["url"]
                    item = self.get_item_by_url(url)
                    if item:
                        collection.append_item(item, note=row["desc"])
                        self.progress(1)
                    else:
                        logger.error(f"Unable to get item for {url}")
                        self.progress(-1, url)

    def run(self):
        uris = set()
        filename = self.metadata["file"]
        with zipfile.ZipFile(filename, "r") as zipref:
            with tempfile.TemporaryDirectory() as tmpdirname:
                logger.debug(f"Extracting {filename} to {tmpdirname}")
                zipref.extractall(tmpdirname)
                if os.path.exists(tmpdirname + "/reviews.csv"):
                    with open(tmpdirname + "/reviews.csv") as f:
                        reader = csv.DictReader(f, delimiter=",")
                        for row in reader:
                            uris.add(row["Letterboxd URI"])
                            self.mark(
                                row["Letterboxd URI"],
                                ShelfType.COMPLETE,
                                row["Watched Date"],
                                row["Rating"],
                                row["Review"],
                                row["Tags"],
                            )
                if os.path.exists(tmpdirname + "/ratings.csv"):
                    with open(tmpdirname + "/ratings.csv") as f:
                        reader = csv.DictReader(f, delimiter=",")
                        for row in reader:
                            if row["Letterboxd URI"] in uris:
                                continue
                            uris.add(row["Letterboxd URI"])
                            self.mark(
                                row["Letterboxd URI"],
                                ShelfType.COMPLETE,
                                row["Date"],
                                row["Rating"],
                            )
                if os.path.exists(tmpdirname + "/watched.csv"):
                    with open(tmpdirname + "/watched.csv") as f:
                        reader = csv.DictReader(f, delimiter=",")
                        for row in reader:
                            if row["Letterboxd URI"] in uris:
                                continue
                            uris.add(row["Letterboxd URI"])
                            self.mark(
                                row["Letterboxd URI"],
                                ShelfType.COMPLETE,
                                row["Date"],
                            )
                if os.path.exists(tmpdirname + "/watchlist.csv"):
                    with open(tmpdirname + "/watchlist.csv") as f:
                        reader = csv.DictReader(f, delimiter=",")
                        for row in reader:
                            if row["Letterboxd URI"] in uris:
                                continue
                            uris.add(row["Letterboxd URI"])
                            self.mark(
                                row["Letterboxd URI"],
                                ShelfType.WISHLIST,
                                row["Date"],
                            )
                if os.path.isdir(tmpdirname + "/lists"):
                    for fn in os.listdir(tmpdirname + "/lists"):
                        if not fn.endswith(".csv"):
                            continue
                        self.import_list(tmpdirname + "/lists/" + fn)
        self.metadata["total"] = self.metadata["processed"]
        self.message = f"{self.metadata['imported']} imported, {self.metadata['skipped']} skipped, {self.metadata['failed']} failed"
        self.save(update_fields=["metadata", "message"])
