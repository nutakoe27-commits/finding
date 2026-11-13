"""Public raw HTML, canonical URLs, crawl discovery and paid-answer isolation."""
from datetime import datetime,timedelta,timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from urllib.robotparser import RobotFileParser

from fastapi.testclient import TestClient
from matchlab import archive
from matchlab.api import create_app
from matchlab.config import Settings
from matchlab.seo import NS,match_path,MatchIndex
from matchlab.storage import Repository
from browser_archive_smoke import feed
from outcome_helpers import forecast,archive_result
from test_archive import header
from test_product import event
from verdict_helpers import statistics


class Parsed(HTMLParser):
    def __init__(self,html):
        super().__init__();self.tags=[];self.feed(html)
    def handle_starttag(self,tag,attrs):self.tags.append((tag,dict(attrs)))
    def values(self,tag,attr):return [attrs.get(attr) for name,attrs in self.tags if name==tag]


class SeoTests(unittest.TestCase):
    def setUp(self):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.settings=Settings(Path(folder.name),public_origin='https://prognozich.online')
        self.repo=Repository(self.settings.database);self.repo.initialize()
        self.now=datetime.now(timezone.utc)
        a=header('101');a.update(members=['Темпл Оулс','Чарльстон Кугарс'],featured_rank=0)
        b=header('102');b['members']=['Первая команда','Вторая команда']
        feed(self.repo,[a,b]);self.a=a;self.b=b
        self.client=TestClient(create_app(self.settings));self.client.__enter__();self.addCleanup(self.client.__exit__,None,None,None)

    def url(self,identifier='101'):
        response=self.client.get('/match/'+identifier,follow_redirects=False)
        self.assertEqual(response.status_code,301,response.text)
        return response.headers['location']

    def schema(self,html):
        import re
        return [json.loads(v) for v in re.findall(r'<script type="application/ld\+json">(.*?)</script>',html,re.S)]

    def test_raw_html_contains_unique_match_title_facts_schema_and_a_paid_service_entry(self):
        path=self.url();response=self.client.get(path);self.assertEqual(response.status_code,200)
        html=response.text
        self.assertIn('<h1>Темпл Оулс — Чарльстон Кугарс</h1>',html)
        self.assertIn('РАЗБОР ОТ ИИ',html);self.assertNotIn('ПРОГНОЗ',html);self.assertIn('разбор ИИ на ',html);self.assertIn(self.a['competition'],html)
        self.assertIn('МСК',html);self.assertIn('Готового вердикта на этой странице нет.',html)
        self.assertIn('href="/#stream/101"',html)
        parsed=Parsed(html)
        canonical=[attrs['href'] for tag,attrs in parsed.tags if tag=='link' and attrs.get('rel')=='canonical']
        self.assertEqual(canonical,[self.settings.public_origin+path])
        event,breadcrumb=self.schema(html)
        self.assertEqual((event['@type'],breadcrumb['@type']),('SportsEvent','BreadcrumbList'))
        self.assertEqual([p['name'] for p in event['competitor']],self.a['members'])
        self.assertNotIn('location',event);self.assertNotIn('endDate',event)
        self.assertNotIn('Снимок коэффициентов',html);self.assertNotIn('Winline',html)

    def test_alias_wrong_slug_and_trailing_slash_redirect_to_one_canonical_without_host_poisoning(self):
        path=self.url()
        for alias in ('/match/101/wrong','/match/101/','/match/101/wrong/extra'):
            r=self.client.get(alias,follow_redirects=False)
            self.assertEqual((r.status_code,r.headers['location']),(301,path))
        html=self.client.get(path+'?utm_source=search',headers={'Host':'untrusted.example'}).text
        self.assertNotIn('untrusted.example',html);self.assertNotIn('utm_source',html)
        self.assertIn('href="'+self.settings.public_origin+path+'"',html)

    def test_home_and_robots_link_the_real_server_rendered_directory_and_sitemap(self):
        html=self.client.get('/').text
        self.assertIn('href="/matches/"',html);self.assertIn('<link rel="canonical" href="https://prognozich.online/">',html)
        self.assertIn('Спортивные разборы от ИИ',html);self.assertNotIn('<!-- SEO_HEAD -->',html)
        robots=self.client.get('/robots.txt')
        self.assertEqual(robots.status_code,200);self.assertIn('Disallow: /api/',robots.text)
        self.assertIn('Sitemap: https://prognozich.online/sitemap.xml',robots.text)
        self.assertNotIn('Disallow: /static/',robots.text)
        rules=RobotFileParser();rules.parse(robots.text.splitlines())
        self.assertTrue(rules.can_fetch('Googlebot','https://prognozich.online/go/winline/101?source=stream'))
        self.assertFalse(rules.can_fetch('Googlebot','https://prognozich.online/api/analyses'))
        self.assertTrue(rules.can_fetch('Googlebot','https://prognozich.online'+self.url()))
        self.assertFalse(any(attrs.get('id')=='event-source' for tag,attrs in Parsed(html).tags))
        head=self.client.head(self.url());self.assertEqual(head.status_code,200);self.assertEqual(head.content,b'')

    def test_split_xml_sitemaps_and_directory_pagination_are_complete_and_do_not_fake_lastmod(self):
        with patch('matchlab.seo.SITEMAP_SIZE',1),patch('matchlab.seo.PAGE_SIZE',1):
            root=ET.fromstring(self.client.get('/sitemap.xml').content)
            urls=[v.text for v in root.findall('.//{'+NS+'}loc')]
            self.assertEqual(len(urls),3)
            all_matches=[]
            for part in (1,2):
                xml=self.client.get(f'/sitemap-matches-{part}.xml')
                self.assertEqual(xml.status_code,200);self.assertNotIn('<lastmod>',xml.text)
                all_matches += [v.text for v in ET.fromstring(xml.content).findall('.//{'+NS+'}loc')]
            self.assertEqual(set(all_matches),{self.settings.public_origin+self.url('101'),self.settings.public_origin+self.url('102')})
            pages=self.client.get('/sitemap-pages.xml').text;self.assertIn('/matches/?page=2',pages)
            listing=self.client.get('/matches/');self.assertIn('rel="next" href="/matches/?page=2"',listing.text)
            second=self.client.get('/matches/?page=2');self.assertIn('страница 2',second.text)
            self.assertIn('rel="prev" href="/matches/"',second.text)
            self.assertEqual(self.client.get('/matches/?page=1',follow_redirects=False).status_code,301)
            for url in ('/matches/?page=3','/matches/?page=-1','/matches/?page=abc','/sitemap-matches-3.xml','/sitemap-matches-0.xml'):
                self.assertEqual(self.client.get(url).status_code,404,url)

    def test_unknown_disabled_incomplete_and_outright_pages_are_real_noindex_404s(self):
        tt=header('103');tt.update(sport_id=20,sport_label='Настольный теннис')
        outright=header('104');outright['members']=['Победитель турнира']
        feed(self.repo,[self.a,self.b,tt,outright])
        for url in ('/match/999','/match/not-a-number','/match/103','/match/104'):
            r=self.client.get(url);self.assertEqual(r.status_code,404,url);self.assertEqual(r.headers['x-robots-tag'],'noindex')
        sitemap=self.client.get('/sitemap-matches-1.xml').text
        self.assertNotIn('/match/103/',sitemap);self.assertNotIn('/match/104/',sitemap)

    def test_durable_pages_survive_feed_removal_and_only_confirmed_result_is_called_finished(self):
        old=header('105');old.update(members=['Старая команда','Другой соперник'],kickoff_at=(self.now-timedelta(days=1)).isoformat())
        pending=header('106');pending.update(members=['Неизвестная команда','Соперник'],kickoff_at=(self.now-timedelta(days=1)).isoformat())
        feed(self.repo,[self.a,old,pending])
        current={**self.a,'observed_at':datetime.now(timezone.utc).isoformat()}
        feed(self.repo,[current])
        self.assertFalse(archive.document(self.repo,'105')['source_present'])
        archive_result(self.repo,archive.document(self.repo,'105'),'2:1','finished-105')
        html=self.client.get(self.url('105')).text
        self.assertIn('Матч завершён',html);self.assertIn('2:1',html)
        other=self.client.get(self.url('106')).text
        self.assertIn('итог ещё не подтверждён',other);self.assertNotIn('Матч завершён',other)
        self.assertIn('/match/105/',self.client.get('/sitemap-matches-1.xml').text)

    def test_paid_private_content_and_model_invocations_never_reach_public_html_or_sitemaps(self):
        doc=archive.document(self.repo,'101')
        forecast(self.repo,'a'*32,event=doc,at=self.now-timedelta(seconds=1),score=None)
        paths=[self.url(),'/matches/','/sitemap.xml','/sitemap-matches-1.xml']
        with patch('matchlab.qwen._completion',side_effect=AssertionError('crawler called AI')),patch.object(Repository,'event_analysis',side_effect=AssertionError('private forecast accessed')):
            for path in paths:
                html=self.client.get(path).text
                self.assertNotIn('PRIVATE_',html);self.assertNotIn('a'*32,html)
        with self.repo.connection() as db:
            for table in ('ai_usage','statistics_request_jobs','ai_analysis_jobs','analysis_credit_ledger'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_html_and_jsonld_escape_source_names_instead_of_creating_scripts(self):
        bad=header('107');bad['members']=['Альфа </script><script>alert(1)</script>','Бета & "Гости"']
        feed(self.repo,[bad])
        html=self.client.get(self.url('107')).text
        self.assertNotIn('</script><script>alert(1)</script>',html)
        self.assertIn('&lt;/script&gt;',html)
        event=self.schema(html)[0];self.assertEqual(event['competitor'][0]['name'],bad['members'][0])
        scripts=[a for t,a in Parsed(html).tags if t=='script']
        self.assertTrue(all(a.get('type')=='application/ld+json' or a.get('src','').startswith('/static/theme.js?') for a in scripts))

    def test_legacy_match_urls_and_index_deduplicate_source_ids_and_use_moscow_dates(self):
        d=event();d.update(kickoff_at='2026-12-31T21:30:00+00:00')
        self.repo.import_event_markets(d)
        path=self.url('123');html=self.client.get(path).text
        self.assertIn('1 января 2027, 00:30 МСК',html);self.assertIn('href="/#match/123"',html)
        copy=event();copy.update(source_event_id='101',url='https://winline.ru/stavki/event/101')
        self.repo.import_event_markets(copy)
        rows=MatchIndex(self.repo).entries()
        self.assertEqual(sum(e['source_event_id']=='101' for e in rows),1)
        self.assertEqual(next(e['home'] for e in rows if e['source_event_id']=='101'),'Темпл Оулс')

    def test_catalogue_and_featured_api_supply_canonical_links_for_crawling(self):
        path=self.url()
        data=self.client.get('/api/bookmakers/events').json()
        self.assertEqual(next(e['public_url'] for e in data['events'] if e['source_event_id']=='101'),path)
        self.assertEqual(self.client.get('/api/bookmakers/featured-events').json()['events'][0]['public_url'],path)

    def test_public_source_statistics_keep_units_and_never_use_another_event_snapshot(self):
        snapshot=statistics(identifier='101')
        snapshot['sections']=[{'title':'Статистика сезона','text':'47\n%\nПобеды\n19\n%'}]
        path=self.url()
        with patch.object(Repository,'statistics',return_value=snapshot):
            html=self.client.get(path).text
        self.assertIn('Показатели участников',html);self.assertIn('47%',html);self.assertIn('19%',html)
        self.assertIn('режим показателей не указан источником',html)
        snapshot['source_event_id']='999'
        with patch.object(Repository,'statistics',return_value=snapshot):html=self.client.get(path).text
        self.assertNotIn('Показатели участников',html)


if __name__=='__main__':unittest.main()
