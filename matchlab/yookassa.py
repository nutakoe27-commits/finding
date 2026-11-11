"""Small test-store adapter. Credentials and provider errors never reach clients/logs."""
from decimal import Decimal,InvalidOperation
import logging
import re
from urllib.parse import urlsplit

import httpx

API='https://api.yookassa.ru/v3'
LOG=logging.getLogger('uvicorn.error')


class ProviderUnavailable(Exception):
    def __init__(self,category='provider_unavailable',*,definitive=False,retry_after=5):
        super().__init__(category);self.definitive=definitive;self.retry_after=retry_after
class ProviderMismatch(Exception):pass


def kopecks(amount):
    try:
        value=Decimal(str(amount))
        if not value.is_finite() or value<0 or value!=value.quantize(Decimal('.01')):raise ValueError
        return int(value*100)
    except (ValueError,InvalidOperation,TypeError):raise ProviderMismatch('amount') from None


class YooKassa:
    def __init__(self,settings):self.settings=settings

    def request(self,method,path,*,data=None,key=None):
        if not self.settings.yookassa_shop_id.isdigit() or not self.settings.yookassa_secret_key.startswith('test_'):
            raise ProviderUnavailable('configuration',definitive=True)
        try:
            response=httpx.request(method,API+path,auth=(self.settings.yookassa_shop_id,self.settings.yookassa_secret_key),
                headers={'Idempotence-Key':key} if key else {},json=data,timeout=25,follow_redirects=False)
            response.raise_for_status();value=response.json()
            if not isinstance(value,dict):raise ValueError
            return value
        except httpx.HTTPStatusError as error:
            status=error.response.status_code;parameter='-'
            try:
                candidate=error.response.json().get('parameter','')
                if isinstance(candidate,str) and re.fullmatch(r'(?:receipt|amount|confirmation|metadata|capture|description|payment_method_data)(?:\.[a-z_0-9]+){0,5}',candidate):parameter=candidate
            except (ValueError,AttributeError):pass
            LOG.warning('YooKassa unavailable: http_status=%s parameter=%s',status,parameter)
            try:retry=max(5,min(300,int(error.response.headers.get('retry-after','5'))))
            except ValueError:retry=5
            raise ProviderUnavailable('provider_http',definitive=method=='POST' and status in (400,401,403,404,422),retry_after=retry) from None
        except (httpx.HTTPError,ValueError):
            LOG.warning('YooKassa unavailable: category=transport_or_response')
            raise ProviderUnavailable('provider_unavailable') from None

    def create(self,order,origin):
        return self.request('POST','/payments',key=order['id'],data={
            'amount':{'value':f"{order['amount_kopecks']//100}.{order['amount_kopecks']%100:02d}",'currency':'RUB'},'capture':True,
            'confirmation':{'type':'redirect','return_url':origin+'/#account'},
            'description':f"prognozich: {order['credits']} разборов (тест)",
            'metadata':{'order_id':order['id'],'user_id':order['user_id']}})

    def payment(self,identifier):
        if not re.fullmatch(r'[a-zA-Z0-9-]{10,80}',identifier):raise ProviderMismatch('payment_id')
        return self.request('GET','/payments/'+identifier)

    def refund(self,identifier):
        if not re.fullmatch(r'[a-zA-Z0-9-]{10,80}',identifier):raise ProviderMismatch('refund_id')
        return self.request('GET','/refunds/'+identifier)


def confirmation_url(value):
    confirmation=value.get('confirmation') or {}
    if not isinstance(confirmation,dict):raise ProviderMismatch('confirmation')
    url=confirmation.get('confirmation_url')
    if url is None:return None
    if not isinstance(url,str):raise ProviderMismatch('confirmation')
    try:p=urlsplit(url);port=p.port
    except ValueError:raise ProviderMismatch('confirmation') from None
    if p.scheme!='https' or p.hostname not in ('yoomoney.ru','yookassa.ru') or p.username or p.password or port not in (None,443):
        raise ProviderMismatch('confirmation')
    return url
