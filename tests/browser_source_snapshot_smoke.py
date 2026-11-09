"""Check the SDK projection's full/stream modes against an isolated browser fixture."""

import asyncio
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from winline.browser import SNAPSHOT


async def main():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, executable_path=shutil.which("chromium")
        )
        try:
            page = await browser.new_page()
            await page.evaluate("""() => {
              const events=[{id:1,idSport:1,isLive:0,members:['А','Б']},{id:2,idSport:1,isLive:1,members:['В','Г']}];
              const line=(id,idEvent,isLive)=>({id,idEvent,isLive,idTipMarket:1,V:[1.9,2.5],tipLine:{freeTextR:'Исход',R:['1','2']}});
              window.fixtureLines=[line(10,1,0),line(10,2,1),...Array.from({length:500},(_,i)=>line(i+100,1,0)),line(11,2,1)];
              const sport={id:1,name:'Футбол',strings:{}};
              const format={formatGameSegments:({sourceStr})=>sourceStr,formatString:({sourceStr})=>sourceStr,formatClearScore:({sourceStr})=>sourceStr};
              const svc={query:({isLive})=>fixtureLines.filter(l=>l.isLive===isLive),getEventMainLine:(id,live)=>fixtureLines.find(l=>l.idEvent===id && l.isLive===live && l.id===10)};
              window.apiWlb={version:'fixture',client:{websocket:{readyState:1}},getEvents:()=>({toArray:()=>events}),getSports:()=>({byId:()=>sport,serialize:()=>[sport]}),getCountries:()=>({serialize:()=>[]}),getChampionships:()=>({serialize:()=>[]}),getService:name=>name==='LineService'?svc:format};
            }""")
            full = json.loads(await page.evaluate(SNAPSHOT))
            light = json.loads(
                await page.evaluate(SNAPSHOT, {"all_lines": False, "event_ids": []})
            )
            selected = json.loads(
                await page.evaluate(SNAPSHOT, {"all_lines": False, "event_ids": [2]})
            )
            assert len(full["lines"]) == 503
            assert len(light["events"]) == 2 and len(light["lines"]) == 2
            assert {
                (l["idEvent"], l["id"], l["isLive"]) for l in selected["lines"]
            } == {(1, 10, 0), (2, 10, 1), (2, 11, 1)}
            await page.evaluate("fixtureLines[1].V[0]=3.1")
            updated = json.loads(
                await page.evaluate(SNAPSHOT, {"all_lines": False, "event_ids": []})
            )
            assert updated["events"][1]["_main_line"]["V"][0] == 3.1
            assert updated["events"][0]["_main_line"]["V"][0] == 1.9
            print(
                "SDK stream projection: all headers, fresh main prices, selected markets and legacy full mode OK"
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
