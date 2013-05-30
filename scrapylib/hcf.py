"""
HCF Middleware

This SpiderMiddleware uses the HCF backend from hubstorage to retrieve the new
urls to crawl and store back the links extracted.
"""
from collections import defaultdict
from scrapy import signals, log
from scrapy.exceptions import NotConfigured, DontCloseSpider
from scrapy.http import Request
from hubstorage import HubstorageClient


class HcfMiddleware(object):

    def __init__(self, crawler):

        self.crawler = crawler
        hs_endpoint = self._get_config(crawler, "HS_ENDPOINT")
        hs_auth = self._get_config(crawler, "HS_AUTH")
        hs_projectid = self._get_config(crawler, "HS_PROJECTID")
        self.hs_frontier = self._get_config(crawler, "HS_FRONTIER")
        self.hs_slot = self._get_config(crawler, "HS_SLOT")

        self.hsclient = HubstorageClient(auth=hs_auth, endpoint=hs_endpoint)
        self.project = self.hsclient.get_project(hs_projectid)
        self.fclient = self.project.frontier

        self.new_links = defaultdict(list)
        self.batch_ids = []

        # crawler.signals.connect(self.idle_spider, signals.spider_idle)
        crawler.signals.connect(self.close_spider, signals.spider_closed)

    def _get_config(self, crawler, key):
        value = crawler.settings.get(key)
        if not value:
            raise NotConfigured('%s not found' % key)
        return value

    def _msg(self, msg):
        log.msg('(HCF) %s' % msg)

    @classmethod
    def from_crawler(cls, crawler):
        return cls(crawler)

    def process_start_requests(self, start_requests, spider):
        has_new_requests = False
        for req in self._get_new_requests():
            has_new_requests = True
            yield req

        # if there are no links in the hcf, use the start_requests
        if not has_new_requests:
            self._msg('Using start_requests')
            for r in start_requests:
                yield r

    def process_spider_output(self, response, result, spider):
        skip_hcf = response.meta.get('skip_hcf', False)
        slot_callback = getattr(spider, 'slot_callback', self._get_slot)
        for item in result:
            if isinstance(item, Request) and not skip_hcf:
                request = item
                if request.method == 'GET':  # XXX: Only POST support for now.
                    slot = slot_callback(request)
                    self.new_links[slot].append(request.url)
                else:
                    yield item
            else:
                yield item

    def idle_spider(self, spider):
        self._save_new_links()
        self.fclient.flush()
        self._delete_processed_ids()
        has_new_requests = False
        for request in self._get_new_requests():
            self.crawler.engine.schedule(request, spider)
            has_new_requests = True
        if has_new_requests:
            raise DontCloseSpider

    def close_spider(self, spider, reason):
        # Only store the results if the spider finished normally, if it
        # was shutdown there is not way to know whether all the url batches
        # were processed and it is better not to delete them from the frontier
        # (so they will be picked by anothe process).
        if reason == 'finished':
            self._save_new_links()
            self._delete_processed_ids()
            # XXX: Start new job
        self.fclient.close()
        self.hsclient.close()

    def _get_new_requests(self):
        """ Get a new batch of links from the HCF."""
        num_batches = 0
        num_links = 0
        for batch in self.fclient.read(self.hs_frontier, self.hs_slot):
            num_batches += 1
            self.batch_ids.append(batch['id'])
            for r in batch['requests']:
                num_links += 1
                yield Request(r[0])
        self._msg('Read %d new batches from slot(%s)' % (num_batches, self.hs_slot))
        self._msg('Read %d new links from slot(%s)' % (num_links, self.hs_slot))

    def _save_new_links(self):
        """ Save the new extracted links into the HCF."""
        for slot, links in self.new_links.items():
            fps = [{'fp': l} for l in links]
            self.fclient.add(self.hs_frontier, slot, fps)
            self._msg('Stored %d new links in slot(%s)' % (len(links), slot))
        self.new_links = defaultdict(list)

    def _delete_processed_ids(self):
        """ Delete in the HCF the ids of the processed batches."""
        self.fclient.delete(self.hs_frontier, self.hs_slot, self.batch_ids)
        self._msg('Deleted %d processed batches in slot(%s)' % (len(self.batch_ids),
                                                                self.hs_slot))
        self.batch_ids = []

    def _get_slot(self, request):
        """ Determine to which slot should be saved the request."""
        return '0'
