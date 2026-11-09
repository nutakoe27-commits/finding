#!/usr/bin/env python3
"""Preview or create the app's JavaScript goals in the owner's Metrika counter."""
import argparse
import getpass
import json
import re
import sys
import warnings
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

COUNTER = 114139580
URL = f'https://api-metrika.yandex.net/management/v1/counter/{COUNTER}/goals'
GOALS = (
    ('registration_success','Успешная регистрация'),
    ('login_success','Успешный вход'),
    ('match_view','Открытие матча'),
    ('ai_intent','Нажатие «Разбор ИИ»'),
    ('ai_request_start','Отправка запроса на разбор'),
    ('ai_model_result','Показ результата модели'),
    ('ai_summary_result','Показ автоматической сводки'),
    ('ai_request_error','Ошибка запроса разбора'),
    ('favorite_add','Добавление в избранное'),
    ('winline_click','Переход на Winline'),
    ('promo_redeemed','Активация промокода'),
    ('push_subscribed','Подписка на уведомления'),
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(opener, token, method, body=None):
    payload=json.dumps(body,ensure_ascii=False).encode('utf-8') if body is not None else None
    req=Request(URL,data=payload,method=method,headers={'Authorization':'OAuth '+token,'Content-Type':'application/json','Accept':'application/json'})
    with opener.open(req,timeout=20) as response:
        raw=response.read(2_000_001)
        if len(raw)>2_000_000: raise ValueError('Слишком большой ответ API.')
        return json.loads(raw)


def plan(existing):
    if not isinstance(existing,list) or not all(isinstance(goal,dict) for goal in existing):
        raise ValueError('Неожиданный список целей от Яндекса.')
    result=[]
    for identifier,name in GOALS:
        matching=[goal for goal in existing if goal.get('type')=='action' and isinstance(goal.get('conditions'),list) and
                  any(isinstance(condition,dict) and condition.get('type')=='exact' and condition.get('url')==identifier
                      for condition in goal['conditions'])]
        same_name=[goal for goal in existing if goal.get('name')==name]
        inactive=any(str(goal.get('status','')).lower() in ('deleted','disabled','archived') for goal in matching)
        state='review' if inactive else 'exists' if matching else 'conflict' if same_name else 'create'
        result.append((identifier,name,state))
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true',help='Создать только отсутствующие цели; без флага выполняется проверка.')
    args=parser.parse_args(argv)
    try:
        if not sys.stdin.isatty(): raise ValueError('Нужен интерактивный терминал для скрытого ввода токена.')
        with warnings.catch_warnings():
            warnings.simplefilter('error',getpass.GetPassWarning)
            token=getpass.getpass('OAuth-токен Яндекс Метрики (не будет показан): ').strip()
        if not re.fullmatch(r'[A-Za-z0-9._~-]{16,512}',token):
            raise ValueError('Токен не указан.')
        opener=build_opener(NoRedirect())
        data=request(opener,token,'GET')
        if not isinstance(data,dict): raise ValueError('Неожиданный ответ API.')
        rows=plan(data.get('goals'))
        labels={'exists':'уже существует','review':'найдена неактивная цель, проверьте в кабинете','conflict':'имя занято другой целью, пропущено','create':'будет создана'}
        print(f'Счётчик {COUNTER}:')
        for identifier,name,state in rows: print(f'  {identifier}: {name} — {labels[state]}')
        if not args.apply:
            print('Проверка завершена; цели не изменены. Для создания запустите с --apply.')
            return 0
        created=0
        for identifier,name,state in rows:
            if state!='create': continue
            payload={'goal':{'name':name,'type':'action','conditions':[{'type':'exact','url':identifier}]}}
            result=request(opener,token,'POST',payload)
            if not isinstance(result,dict) or not isinstance(result.get('goal'),dict) or not result['goal'].get('id'):
                raise ValueError('Яндекс не подтвердил создание цели.')
            created+=1
            print(f'Создана: {identifier}')
        print(f'Готово: создано {created}; существующие цели не изменялись.')
        return 0
    except HTTPError as error:
        print(f'Ошибка API Яндекса: HTTP {error.code}. Уже созданные цели сохранены; повторный запуск проверит их.',file=sys.stderr)
    except (ValueError,URLError,TimeoutError,OSError,json.JSONDecodeError,getpass.GetPassWarning) as error:
        print(f'Не удалось завершить настройку: {type(error).__name__}. Проверьте доступ и повторите.',file=sys.stderr)
    return 1


if __name__=='__main__':
    raise SystemExit(main())
