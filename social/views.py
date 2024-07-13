from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from catalog.models import *
from journal.models import *
from takahe.models import PostInteraction, TimelineEvent
from takahe.utils import Takahe

from .models import *

PAGE_SIZE = 10


@require_http_methods(["GET"])
@login_required
def feed(request, typ=0):
    if not request.user.registration_complete:
        return redirect(reverse("users:register"))
    user = request.user
    podcast_ids = [
        p.item_id
        for p in user.shelf_manager.get_latest_members(
            ShelfType.PROGRESS, ItemCategory.Podcast
        )
    ]
    recent_podcast_episodes = PodcastEpisode.objects.filter(
        program_id__in=podcast_ids
    ).order_by("-pub_date")[:10]
    books_in_progress = Edition.objects.filter(
        id__in=[
            p.item_id
            for p in user.shelf_manager.get_latest_members(
                ShelfType.PROGRESS, ItemCategory.Book
            )[:10]
        ]
    )
    tvshows_in_progress = Item.objects.filter(
        id__in=[
            p.item_id
            for p in user.shelf_manager.get_latest_members(
                ShelfType.PROGRESS, ItemCategory.TV
            )[:10]
        ]
    )
    return render(
        request,
        "feed.html",
        {
            "feed_type": typ,
            "recent_podcast_episodes": recent_podcast_episodes,
            "books_in_progress": books_in_progress,
            "tvshows_in_progress": tvshows_in_progress,
        },
    )


def focus(request):
    return feed(request, typ=1)


@login_required
@require_http_methods(["GET"])
def data(request):
    since_id = int(request.GET.get("last", 0))
    typ = int(request.GET.get("typ", 0))
    identity_id = request.user.identity.pk
    events = TimelineEvent.objects.filter(
        identity_id=identity_id,
        type__in=[TimelineEvent.Types.post, TimelineEvent.Types.boost],
    )
    match typ:
        case 1:
            events = events.filter(
                subject_post__type_data__object__has_key="relatedWith"
            )
        case _:  # default: no replies
            events = events.filter(subject_post__in_reply_to__isnull=True)
    if since_id:
        events = events.filter(id__lt=since_id)
    events = list(
        events.select_related(
            "subject_post",
            "subject_post__author",
            # "subject_post__author__domain",
            "subject_identity",
            # "subject_identity__domain",
            "subject_post_interaction",
            "subject_post_interaction__identity",
            # "subject_post_interaction__identity__domain",
        )
        .prefetch_related(
            "subject_post__attachments",
            # "subject_post__mentions",
            # "subject_post__emojis",
        )
        .order_by("-id")[:PAGE_SIZE]
    )
    interactions = PostInteraction.objects.filter(
        identity_id=identity_id,
        post_id__in=[event.subject_post_id for event in events],
        type__in=["like", "boost"],
        state__in=["new", "fanned_out"],
    ).values_list("post_id", "type")
    for event in events:
        if event.subject_post_id:
            event.subject_post.liked_by_current_user = (
                event.subject_post_id,
                "like",
            ) in interactions
        event.subject_post.boosted_by_current_user = (
            event.subject_post_id,
            "boost",
        ) in interactions
    return render(request, "feed_events.html", {"feed_type": typ, "events": events})


@require_http_methods(["GET"])
@login_required
def notification(request):
    if not request.user.registration_complete:
        return redirect(reverse("users:register"))
    user = request.user
    podcast_ids = [
        p.item_id
        for p in user.shelf_manager.get_latest_members(
            ShelfType.PROGRESS, ItemCategory.Podcast
        )
    ]
    recent_podcast_episodes = PodcastEpisode.objects.filter(
        program_id__in=podcast_ids
    ).order_by("-pub_date")[:10]
    books_in_progress = Edition.objects.filter(
        id__in=[
            p.item_id
            for p in user.shelf_manager.get_latest_members(
                ShelfType.PROGRESS, ItemCategory.Book
            )[:10]
        ]
    )
    tvshows_in_progress = Item.objects.filter(
        id__in=[
            p.item_id
            for p in user.shelf_manager.get_latest_members(
                ShelfType.PROGRESS, ItemCategory.TV
            )[:10]
        ]
    )
    return render(
        request,
        "notification.html",
        {
            "recent_podcast_episodes": recent_podcast_episodes,
            "books_in_progress": books_in_progress,
            "tvshows_in_progress": tvshows_in_progress,
        },
    )


class NotificationEvent:
    def __init__(self, tle) -> None:
        self.event = tle
        self.type = tle.type
        self.template = tle.type
        self.created = tle.created
        self.identity = (
            APIdentity.objects.filter(pk=tle.subject_identity.pk).first()
            if tle.subject_identity
            else None
        )
        self.post = tle.subject_post
        if self.type == "mentioned":
            # for reply, self.post is the original post
            self.reply = self.post
            self.replies = [self.post]
            self.post = self.post.in_reply_to_post() if self.post else None
        self.piece = Piece.get_by_post_id(self.post.id) if self.post else None
        self.item = getattr(self.piece, "item") if hasattr(self.piece, "item") else None
        if self.piece and self.template in ["liked", "boosted", "mentioned"]:
            cls = self.piece.__class__.__name__.lower()
            self.template += "_" + cls


@login_required
@require_http_methods(["GET"])
def events(request):
    match request.GET.get("type"):
        case "follow":
            types = ["followed", "follow_requested"]
        case "mention":
            types = ["mentioned"]
        case _:
            types = ["liked", "boosted", "mentioned", "followed", "follow_requested"]
    es = Takahe.get_events(request.user.identity.pk, types)
    last = request.GET.get("last")
    if last:
        es = es.filter(created__lt=last)
    nes = [NotificationEvent(e) for e in es[:PAGE_SIZE]]
    return render(
        request,
        "events.html",
        {"events": nes},
    )
