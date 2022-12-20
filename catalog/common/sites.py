"""
Site and SiteManager

Site should inherite from AbstractSite
a Site should map to a unique set of url patterns.
a Site may scrape a url and store result in ResourceContent
ResourceContent persists as an ExternalResource which may link to an Item
"""
from typing import *
import re
from .models import ExternalResource
from dataclasses import dataclass, field
import logging


_logger = logging.getLogger(__name__)


@dataclass
class ResourceContent:
    lookup_ids: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    cover_image: bytes = None
    cover_image_extention: str = None

    def dict(self):
        return {'metadata': self.metadata, 'lookup_ids': self.lookup_ids}

    def to_json(self) -> str:
        return json.dumps({'metadata': self.metadata, 'lookup_ids': self.lookup_ids})


class AbstractSite:
    """
    Abstract class to represent a site
    """
    SITE_NAME = None
    ID_TYPE = None
    WIKI_PROPERTY_ID = 'P0undefined0'
    DEFAULT_MODEL = None
    URL_PATTERNS = [r"\w+://undefined/(\d+)"]

    @classmethod
    def validate_url(self, url: str):
        u = next(iter([re.match(p, url) for p in self.URL_PATTERNS if re.match(p, url)]), None)
        return u is not None

    @classmethod
    def validate_url_fallback(self, url: str):
        return False

    @classmethod
    def id_to_url(self, id_value):
        return 'https://undefined/' + id_value

    @classmethod
    def url_to_id(self, url: str):
        u = next(iter([re.match(p, url) for p in self.URL_PATTERNS if re.match(p, url)]), None)
        return u[1] if u else None

    def __str__(self):
        return f'<{self.__class__.__name__}: {self.url}>'

    def __init__(self, url=None):
        self.id_value = self.url_to_id(url) if url else None
        self.url = self.id_to_url(self.id_value) if url else None
        self.resource = None

    def get_resource(self):
        if not self.resource:
            self.resource = ExternalResource.objects.filter(url=self.url).first()
            if self.resource is None:
                self.resource = ExternalResource(id_type=self.ID_TYPE, id_value=self.id_value, url=self.url)
        return self.resource

    def scrape(self) -> ResourceContent:
        """subclass should implement this, return ResourceContent object"""
        data = ResourceContent()
        return data

    def get_item(self):
        p = self.get_resource()
        if not p:
            # raise ValueError(f'resource not available for {self.url}')
            return None
        if not p.ready:
            # raise ValueError(f'resource not ready for {self.url}')
            return None
        model = p.get_preferred_model()
        if not model:
            model = self.DEFAULT_MODEL
        t, v = model.get_best_lookup_id(p.get_all_lookup_ids())
        if t is not None:
            p.item = model.objects.filter(primary_lookup_id_type=t, primary_lookup_id_value=v).first()
        if p.item is None:
            obj = model.copy_metadata(p.metadata)
            obj['primary_lookup_id_type'] = t
            obj['primary_lookup_id_value'] = v
            p.item = model.objects.create(**obj)
        return p.item

    @property
    def ready(self):
        return bool(self.resource and self.resource.ready)

    def get_resource_ready(self, auto_save=True, auto_create=True, auto_link=True, preloaded_content=None, ignore_existing_content=False):
        """
        Returns an ExternalResource in scraped state if possible
        
        Parameters
        ----------
        auto_save : bool
            automatically saves the ExternalResource and, if auto_create, the Item too
        auto_create : bool
            automatically creates an Item if not exist yet
        auto_link : bool
            automatically scrape the linked resources (e.g. a TVSeason may have a linked TVShow)
        preloaded_content : ResourceContent or dict
            skip scrape(), and use this as scraped result
        ignore_existing_content : bool
            if ExternalResource already has content, ignore that and either use preloaded_content or call scrape()
        """
        if auto_link:
            auto_create = True
        if auto_create:
            auto_save = True
        p = self.get_resource()
        resource_content = {}
        if not self.resource:
            return None
        if not p.ready or ignore_existing_content:
            if isinstance(preloaded_content, ResourceContent):
                resource_content = preloaded_content
            elif isinstance(preloaded_content, dict):
                resource_content = ResourceContent(**preloaded_content)
            else:
                resource_content = self.scrape()
            p.update_content(resource_content)
        if not p.ready:
            _logger.error(f'unable to get resource {self.url} ready')
            return None
        if auto_create and p.item is None:
            self.get_item()
        if auto_save:
            p.save()
            if p.item:
                p.item.merge_data_from_external_resources()
                p.item.save()
        if auto_link:
            for linked_resource in p.required_resources:
                linked_site = SiteManager.get_site_by_url(linked_resource['url'])
                if linked_site:
                    linked_site.get_resource_ready(auto_link=False, preloaded_content=linked_resource.get('content'))
                else:
                    _logger.error(f'unable to get site for {linked_resource["url"]}')
            p.item.update_linked_items_from_external_resource(p)
            p.item.save()
        return p


class SiteManager:
    registry = {}

    @staticmethod
    def register(target) -> Callable:
        id_type = target.ID_TYPE
        if id_type in SiteManager.registry:
            raise ValueError(f'Site for {id_type} already exists')
        SiteManager.registry[id_type] = target
        return target

    @staticmethod
    def get_site_by_id_type(typ: str):
        return SiteManager.registry[typ]() if typ in SiteManager.registry else None

    @staticmethod
    def get_site_by_url(url: str):
        cls = next(filter(lambda p: p.validate_url(url), SiteManager.registry.values()), None)
        if cls is None:
            cls = next(filter(lambda p: p.validate_url_fallback(url), SiteManager.registry.values()), None)
        return cls(url) if cls else None

    @staticmethod
    def get_id_by_url(url: str):
        site = SiteManager.get_site_by_url(url)
        return site.url_to_id(url) if site else None

    @staticmethod
    def get_site_by_resource(resource):
        return SiteManager.get_site_by_id_type(resource.id_type)


ExternalResource.get_site = lambda resource: SiteManager.get_site_by_id_type(resource.id_type)
# ExternalResource.get_site = SiteManager.get_site_by_resource