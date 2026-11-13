"""Presentation wording and removal of source promotions, without rewriting history."""
from copy import deepcopy
from pathlib import Path
import re
import unittest

from matchlab import vocabulary


class VocabularyTests(unittest.TestCase):
    def test_action_labels_do_not_rename_the_brand_or_change_sport_results(self):
        value='Прогнозич: прогноз матча. Решение: Ставить. Ставка выиграла. Пропустить матч.'
        result=vocabulary.text(value)
        self.assertEqual(result,'Прогнозич: разбор матча. Решение: Заходим. Зашло. Не заходим.')
        self.assertEqual(vocabulary.text(result),result)
        self.assertEqual(vocabulary.text('Поражение 0:2 и победа 3:0.'),'Поражение 0:2 и победа 3:0.')
        self.assertNotIn('BetBoom',vocabulary.text('Данные BetBoom сохранены.'))

    def test_old_explanations_keep_their_facts_but_not_source_promotions_or_money_sizes(self):
        value='Прогноз: победа 2:0. [Winline](https://winline.ru/stavki/event/1)\nЛимит риска: до 0,5% игрового банка на эту ставку.\nАргументы: 8 побед.'
        result=vocabulary.text(value)
        self.assertIn('2:0',result);self.assertIn('8 побед',result)
        self.assertNotRegex(result,r'(?i)winline|букмекер|https?://|банк|0,5|\bпрогноз\b|\bставку\b')

    def test_blocked_urls_cover_old_redirects_and_source_subdomains_without_breaking_service_links(self):
        for url in ('https://winline.ru/stavki/event/1','https://www.winline.ru/a',
                    'https://betboom.ru/','https://statshub.yunai.tech/winline/ru/match/1','/go/winline/1'):
            with self.subTest(url=url):self.assertTrue(vocabulary.blocked_url(url))
        for url in ('/match/1/home-away','https://prognozich.online/','https://yookassa.ru/','https://sports.ru/'):
            with self.subTest(url=url):self.assertFalse(vocabulary.blocked_url(url))

    def test_private_response_copy_never_changes_saved_decisions_or_raw_snapshot(self):
        source={'text':'Решение: Ставить. Прогноз готов.','context':{'neural_verdict':{'decision':'bet','reason':{'text':'Данные Winline: 8 побед.'}},
                'event':{'url':'https://winline.ru/stavki/event/1'},'neural_input':{'quote':{'source':'winline','odds':{'home':2.1}}}}}
        before=deepcopy(source);shown=vocabulary.analysis(source)
        self.assertEqual(source,before)
        self.assertEqual(shown['context']['neural_verdict']['decision'],'bet')
        self.assertEqual(shown['context']['neural_input'],source['context']['neural_input'])
        self.assertIsNone(shown['context']['event']['url'])
        self.assertIn('Решение: Заходим',shown['text']);self.assertIn('8 побед',shown['context']['neural_verdict']['reason']['text'])

    def test_current_site_has_no_outbound_buttons_and_keeps_payment_terms_consistent(self):
        root=Path(__file__).resolve().parent.parent/'matchlab'/'static'
        html=(root/'index.html').read_text()
        self.assertNotIn('партнёрскими',html)
        self.assertNotIn('не принимает решения',html)
        self.assertNotRegex(html,r'id="(?:event-source|team-statistics-source|admin-link-form)"|data-admin-tab="links"')
        self.assertLess(html.index('/static/wording.js'),html.index('/static/app.js'))
        for p in [root/'index.html',*(root/'legal').glob('*.html')]:
            with self.subTest(file=p.name):
                s=p.read_text()
                self.assertNotRegex(s,r'(?i)(?<!\w)(?:winline|винлайн|букмекер\w*|прогноз|прогнозы|ставка|ставки|ставить)(?!\w)')
        self.assertIn('«Заходим»',(root/'legal'/'pricing.html').read_text())
        self.assertIn('«Не заходим»',(root/'legal'/'pricing.html').read_text())


if __name__=='__main__':unittest.main()
