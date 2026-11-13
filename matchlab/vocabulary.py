"""Presentation wording. Stored evidence, decisions and payment records stay intact."""
import re
from urllib.parse import urlsplit

PHRASES=(
    ('Рекомендуем пропустить матч','Решение: не заходим'),
    ('рекомендует пропустить матч','выбирает «Не заходим»'),
    ('рекомендовать пропустить матч','выбрать «Не заходим»'),
    ('Ставка выиграла','Зашло'),('Ставка проиграла','Не зашло'),
    ('ставка выиграла','зашло'),('ставка проиграла','не зашло'),
    ('не гарантируют выигрыш','не гарантируют результат'),
    ('не гарантирует выигрыш','не гарантирует результат'),
    ('Пропустить матч','Не заходим'),('пропустить матч','не заходим'),
    ('Пропустить','Не заходим'),('пропустить','не заходим'),
    ('Прогноз ИИ','Разбор ИИ'),('прогноз ИИ','разбор ИИ'),
    ('Линия букмекера','Контекст данных'),('линия букмекера','контекст данных'),
    ('Коэффициенты букмекера','Коэффициенты источника'),('коэффициенты букмекера','коэффициенты источника'),
    ('решения о ставке','решения по матчу'),('решение о ставке','решение по матчу'),
    ('помеху ставке','ограничение выбора'),('на ставку','на решение «Заходим»'),
    ('на эту ставку','для этого решения'),('без обязательной ставки','без обязательного выбора'),
    ('обязательной ставки','обязательного выбора'),('надёжную ставку','безошибочное решение'),
    ('ставку выгодной','выбор обоснованным'),('выгодность ставки','обоснованность выбора'),
    ('Ставить','Заходим'),('ставить','заходим'),('СТАВИТЬ','ЗАХОДИМ'),
    ('ПРОПУСТИТЬ','НЕ ЗАХОДИМ'),
)
FORMS={
    'прогноз':'разбор','прогноза':'разбора','прогнозу':'разбору','прогнозом':'разбором',
    'прогнозе':'разборе','прогнозы':'разборы','прогнозов':'разборов','прогнозам':'разборам',
    'прогнозами':'разборами','прогнозах':'разборах',
    'ставка':'решение','ставки':'решения','ставку':'выбор','ставке':'выборе',
    'ставкой':'решением','ставок':'решений','ставкам':'решениям','ставками':'решениями','ставках':'решениях',
    'выигрыш':'зашло','проигрыш':'не зашло',
}
WORDS=re.compile(r'(?<![\w])('+ '|'.join(sorted(FORMS,key=len,reverse=True))+r')(?![\w])',re.I)
SOURCE=re.compile(r'(?<![\w])(?:winline|винлайн|betboom|бетбум)(?![\w])',re.I)
BOOKMAKER=re.compile(r'букмекер(?:а|у|ом|е|ы|ов|ам|ами|ах)?(?![\w])',re.I)


def blocked_url(value):
    try:
        url=urlsplit(str(value));host=(url.hostname or '').casefold()
        return (url.path.startswith('/go/') or host=='statshub.yunai.tech' or
                bool(re.search(r'(?:^|\.)(?:winline|fonbet|betboom|ligastavok|1xbet|bet365|melbet|leon|marathonbet|bwin|pari)\.[a-z.]+$',host)))
    except ValueError:return True


def words(value):
    for before,after in PHRASES:value=value.replace(before,after)
    def word(match):
        source=match[0];target=FORMS[source.casefold()]
        return target.upper() if source.isupper() else target.capitalize() if source[0].isupper() else target
    value=WORDS.sub(word,value)
    value=SOURCE.sub('источник данных',value)
    return BOOKMAKER.sub('источник данных',value)


def text(value):
    value=str(value or '')
    # These old money-size instructions are not part of the current presentation.
    value=re.sub(r'^Лимит риска:[^\n]*(?:\n|$)','',value,flags=re.M)
    value=re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)',lambda m:m[1] if blocked_url(m[2]) else m[0],value)
    value=re.sub(r'https?://[^\s<>\])]+',lambda m:'' if blocked_url(m[0]) else m[0],value)
    return words(value).strip()


def analysis(payload):
    """Localize a response copy after authorization; never change the saved snapshot."""
    def walk(value,key=''):
        if isinstance(value,dict):return {k:walk(v,k) for k,v in value.items()}
        if isinstance(value,list):return [walk(v,key) for v in value]
        if not isinstance(value,str):return value
        if value.startswith(('http://','https://','/go/')) and blocked_url(value):return None
        return text(value) if key in ('text','risk_note','limitations','literal','unavailable_reason') else value
    return walk(payload)
