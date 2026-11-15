"use strict";
window.AdminModel = (() => {
  const $=id=>document.getElementById(id),root=$('admin-model');
  const node=(tag,cls='',text='')=>{const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;};
  const sports={football:'Футбол',hockey:'Хоккей',tennis:'Теннис',basketball:'Баскетбол',volleyball:'Волейбол'};
  const pct=n=>Number.isFinite(n)?(n*100).toLocaleString('ru-RU',{maximumFractionDigits:1})+'%':'—';
  const odds=n=>Number.isFinite(n)&&n>1?n.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:3}):'—';
  const quote=item=>{const p=item.prices;if(!p)return detail('Нет сохранённых цен','Старый отчёт обработчика');return detail([['home','П1'],['draw','Х'],['away','П2']].filter(([key])=>p[key]!=null).map(([key,label])=>label+' '+odds(p[key])).join(' · '),'Снимок: '+when(item.sample_as_of)+' МСК');};
  const number=n=>Number.isFinite(n)?n.toLocaleString('ru-RU',{maximumFractionDigits:3}):'—';
  const when=t=>t?new Date(t).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}):'—';
  const visible=()=>location.hash==='#admin'&&!root.hidden&&Account.user?.role==='admin';
  let timer,serial=0;
  function stop(){clearTimeout(timer);serial++;root.removeAttribute('aria-busy');}
  function metric(label,value,note){const n=node('article','ar-metric');n.append(node('span','',label),node('strong','',value),node('small','',note));return n;}
  function table(headers,rows){const t=node('table','admin-table'),head=node('thead'),tr=node('tr'),body=node('tbody');for(const h of headers)tr.append(node('th','',h));head.append(tr);for(const values of rows){const row=node('tr');for(const v of values){const td=node('td');td.append(v instanceof Node?v:document.createTextNode(v??'—'));row.append(td);}body.append(row);}t.append(head,body);return t;}
  function detail(main,sub){const n=node('div','am-detail');n.append(node('strong','',main),node('small','muted',sub));return n;}
  function render(data){
    const available=['ready','degraded'].includes(data.state);
    const messages={not_configured:'Сбор ещё не подключён. Установите отдельный обработчик модели на VPS-1.',waiting:'Ожидаем первый отчёт обработчика.',error:'Обработчик не смог обновить отчёт. Проверьте prognozich-model-worker.service на VPS-1.',ready:'Автоматический сбор включён. Проверяем только новые матчи.',degraded:'Не удалось получить очередной ответ модели. Сохранённые вероятности остаются в проверке.'};
    $('am-message').textContent=(messages[data.state]||'Отчёт недоступен.')+(data.stale?' Отчёт устарел: обновления нет более 5 минут или часы сервера расходятся.':'');
    $('am-message').classList.toggle('is-error',!available||data.stale||data.state==='degraded');
    $('am-updated').textContent=data.checked_at?'Проверено '+when(data.checked_at)+' МСК':'Ожидаем подключение';
    $('am-data').hidden=!available;if(!available)return;
    const m=data.evaluation?.overall,variants=data.variant_metrics;
    $('am-variants').hidden=!variants;
    $('am-variants').replaceChildren();
    if(variants){const a=variants.statistics,b=variants.statistics_price;const policy=data.decision_policy;
      $('am-variants').append(metric('A · Только статистика',pct(a.accuracy),`${a.hits} из ${a.evaluated} проверенных выборов; всего ${a.selected}`),metric('B · Статистика → цена',pct(b.accuracy),`${b.won} верных / ${b.lost} неверных; ${b.pending} ожидают итога`),metric('B · Отобрано',String(b.bets),`${b.skipped} пропущено · доля отбора ${pct(b.coverage)}`),metric('B · Виртуальный ROI',pct(b.roi),`${number(b.profit_units)} ед. при 1 ед. на решение; проверено ${b.settled}`));
      $('am-variants').append(node('p','am-experiment',`Эксперимент: A выбирает участника только по статистике. B использует ту же вероятность и затем проверяет цену: вероятность ≥ ${pct(policy.min_probability)}, вероятность × коэффициент − 1 ≥ ${pct(policy.min_expected_return)}. Пороги предварительные. Пропуски не считаются угаданными; деньги не используются.`));
    }
    $('am-metrics').replaceChildren(metric('Зафиксировано',number(data.recorded_events),'Новые матчи после обучения'),metric('Проверено',number(data.scored_events),'Только подтверждённые результаты'),metric('Ожидают проверки',number(data.pending_events),'Нет итога или его нельзя сопоставить'),metric('Ошибка вероятностей · Brier',number(m?.brier),'От 0 до 1 · меньше — лучше'));
    $('am-version').textContent=(data.feature_profile==='statistics_only.v1'?'Без коэффициентов в модели. ':'Прежняя модель: коэффициенты входят в признаки. ')+`CatBoost · CPU · ${data.training_events.toLocaleString('ru-RU')} матчей в обучении (${data.training_rows.toLocaleString('ru-RU')} строк). Версия: ${data.model_sha256.slice(0,12)}. Ответ фиксируется минимум за ${data.lead_minutes} мин. до начала.`+(data.unsupported_sports?.length?' Пока нет пригодных статистических признаков: '+data.unsupported_sports.map(s=>sports[s]||s).join(', ')+'.':'');
    $('am-quality').textContent=m?`Log loss: ${number(m.log_loss)}. Brier по коэффициентам на той же выборке: ${number(data.evaluation?.bookmaker_baseline?.brier)}. Проверено ${m.events} матчей / ${m.rows} оценок участников. Это предварительная проверка, точность процентов ещё не подтверждена.`:'Первые метрики появятся после завершения новых матчей. Обучающие матчи сюда не входят.';
    const chart=$('am-chart');chart.replaceChildren();
    if(!m)chart.append(node('p','muted','Пока нет завершённых проверок.'));
    for(const bin of m?.calibration||[]){const row=node('div','am-bin');row.append(node('span','',`${bin.bin*10}–${(bin.bin+1)*10}% · ${bin.rows} оценок`));const bars=node('div','am-bars');for(const [key,label] of [['predicted','Модель'],['observed','Фактически']]){const line=node('div','am-bar-line'),track=node('div','am-track'),bar=node('div','am-bar '+key);bar.style.width=(bin[key]*100)+'%';track.append(bar);line.append(node('span','',label),track,node('strong','',pct(bin[key])));bars.append(line);}row.append(bars);chart.append(row);}
    const sportRows=Object.entries(data.evaluation?.sports||{}).map(([sport,s])=>[sports[sport]||sport,String(s.events),number(s.brier),number(s.log_loss)]);
    $('am-sports').replaceChildren(sportRows.length?table(['Спорт','Матчей','Brier ↓','Log loss ↓'],sportRows):node('p','muted','Статистика по видам спорта появится после проверки результатов.'));
    const rows=(data.matches||[]).map(item=>{const e=item.event,probs=item.probabilities,scope=item.scope==='regulation'?'Основное время':'Победа в матче';const outcome=item.state==='scored'?(item.winner==='draw'?'Ничья':e[item.winner]):'Ожидаем подтверждения';return [detail(e.home+' — '+e.away,`${sports[e.sport]||e.sport} · ${e.competition||''} · ${when(e.kickoff_at)} МСК`),detail(pct(probs.home),e.home),detail(pct(probs.away),e.away),detail(item.market,scope),quote(item),detail(when(item.issued_at)+' МСК','Снимок: '+when(item.sample_as_of)+' МСК'),detail(item.variants?.statistics?.side?e[item.variants.statistics.side]:'—','A · Только статистика'),detail(item.variants?(item.variants.statistics_price.decision==='bet'?'Ставим':'Пропускаем'):'—',item.variants?({paper_rule_passed:'Порог вероятности и цена подходят',missing_price:'Нет сохранённой цены',probability_below_threshold:'Вероятность ниже порога',price_below_threshold:'Цена не даёт нужного запаса',no_preference:'Нет предпочтения'}[item.variants.statistics_price.reason]||item.variants.statistics_price.reason):'Для новой версии без коэффициентов'),detail(outcome,item.reason||'Результат подтверждён')];});
    $('am-matches').replaceChildren(rows.length?table(['Матч','Участник 1','Участник 2','Рынок','Коэффициенты из снимка','Зафиксировано','Выбор A','Решение B','Проверка'],rows):node('p','muted','Подходящих новых матчей пока нет. Нужны свежие предматчевые снимки и не менее часа до начала.'));
    $('am-count').textContent=`Последние ${rows.length} из ${data.recorded_events} матчей. Метрики рассчитаны по всем сохранённым проверкам. Коэффициенты взяты из исходного снимка и не обновляются задним числом.`;
    $('am-diagnostics').textContent=JSON.stringify({пропущены_при_сборе:data.skipped,ожидают_проверки:data.unscored_reasons,ошибка_инференса:data.inference_error},null,2);
  }
  async function load(){stop();const id=serial;if(!visible())return;root.setAttribute('aria-busy','true');try{const data=await Account.api('/api/admin/participant-model');if(id===serial&&visible())render(data);}catch(error){if(id===serial){$('am-message').textContent='Не удалось загрузить отчёт. '+error.message;$('am-message').classList.add('is-error');}}finally{if(id===serial){root.removeAttribute('aria-busy');if(visible())timer=setTimeout(load,60000);}}}
  document.addEventListener('accountchange',()=>{stop();$('am-data').hidden=true;for(const id of ['am-metrics','am-variants','am-chart','am-sports','am-matches','am-diagnostics','am-version','am-quality','am-message'])$(id).replaceChildren();$('am-updated').textContent='Ожидаем подключение';});
  document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();else if(visible())load();});
  window.addEventListener('hashchange',()=>{if(!visible())stop();});
  return {load,stop};
})();
