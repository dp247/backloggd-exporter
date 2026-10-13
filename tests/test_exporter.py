import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
import pytest
from backloggd import Exporter, ExportError, build_url, fetch_games, parse_games, write_csv, cookies_from_file
from textual.widgets import Input, Button, DataTable

EMPTY = '<turbo-stream action="append" target="user_games"><template></template></turbo-stream>'
def sample(gid='1',title='Example, Game'):
 return f'''<turbo-stream target="user_games"><template>
 <div class="game-cover" game_id="{gid}"><a class="cover-link" href="/games/example/"></a><div class="game-text-centered">{title}</div></div>
 <div id="preloaded-log-{gid}"><div data-log-id="20" data-is-backlog="2026-01-01" data-is-playing="yes" data-rating="8"></div></div>
 </template></turbo-stream>'''

def test_parse():
 r=parse_games(sample(),'ps3')[0]
 assert (r.title,r.statuses,r.rating_raw,r.platform_filter)==('Example, Game','playing|backlog','8','ps3')
 assert parse_games(EMPTY)==[]
 with pytest.raises(ExportError):parse_games('<html>challenge</html>')

def test_url():
 assert build_url('dp247','ps3',['backlog','played']).endswith('played_platform:ps3;type:played,backlog')
 assert 'played_platform' not in build_url('dp247','',['wishlist'])

@pytest.mark.asyncio
async def test_pages():
 pages=[]
 def handle(req):
  page=int(req.url.params['page']);pages.append(page)
  return httpx.Response(200,text=sample(str(page)) if page<3 else EMPTY)
 rows=await fetch_games('dp247','ps3',['backlog'],transport=httpx.MockTransport(handle),delay=0)
 assert len(rows)==2 and pages==[1,2,3]

@pytest.mark.asyncio
async def test_repeat_and_block():
 for body,code in [(sample(),200),('blocked',403),('challenge',200)]:
  with pytest.raises(ExportError):
   await fetch_games('dp247','ps3',['backlog'],transport=httpx.MockTransport(lambda r:httpx.Response(code,text=body)),delay=0)

def test_csv(tmp_path):
 path=tmp_path/'games.csv'
 write_csv(parse_games(sample(title='=SUM(A1)')),str(path))
 assert "'=SUM(A1)" in path.read_text(encoding='utf-8-sig')
 write_csv([],str(path))
 assert "'=SUM(A1)" not in path.read_text(encoding='utf-8-sig')
 nested=tmp_path/'new'/'dir'/'games.csv'
 write_csv([],str(nested))
 assert nested.exists()

def test_cookie_scope(tmp_path):
 path=tmp_path/'cookies.txt'
 path.write_text('# Netscape HTTP Cookie File\n#HttpOnly_.backloggd.com\tTRUE\t/\tTRUE\t\ttest\tfake\n.example.com\tTRUE\t/\tTRUE\t\tother\tfake\n')
 assert len(list(cookies_from_file(str(path))))==1

@pytest.mark.asyncio
async def test_ui(tmp_path,monkeypatch):
 import backloggd
 async def fake_fetch(*args,**kwargs):return parse_games(sample(),'ps3')
 monkeypatch.setattr(backloggd,'fetch_games',fake_fetch)
 settings=dict(username='dp247',cookies='',platform='ps3',statuses=['backlog'],output=str(tmp_path/'ui.csv'))
 app=Exporter(settings)
 async with app.run_test(size=(100,40)) as pilot:
  await pilot.click('#load');await app.workers.wait_for_complete();await pilot.pause()
  assert app.query_one(DataTable).row_count==1
  assert not app.query_one('#export',Button).disabled
  await pilot.click('#export');await pilot.pause()
  assert (tmp_path/'ui.csv').exists()

@pytest.mark.parametrize('raw,expected', [('0',None),('1',0.5),('8',4.0),('9',4.5),('10',5.0),('11',None),('',None),('bad',None)])
def test_star_rating(raw,expected):
 assert parse_games(sample().replace('data-rating="8"',f'data-rating="{raw}"'))[0].rating_stars == expected

def test_multi_status_detail():
 html=sample().replace('data-is-playing="yes"','data-is-play="2025-08-05 15:28:50 UTC" data-status-title="Shelved"')
 row=parse_games(html)[0]
 assert row.is_played and row.is_backlog and not row.is_playing
 assert row.played_status=='shelved' and row.display_status=='Shelved'
 assert row.played_timestamp=='2025-08-05 15:28:50 UTC'
 assert row.backlog_timestamp=='2026-01-01'
 assert row.statuses=='played|backlog'
 row=parse_games(html.replace('data-is-play=', 'data-is-playing="now" data-is-play='))[0]
 assert row.display_status=='Playing' and row.played_status=='shelved'

def test_missing_metadata():
 html=sample();html=html[:html.index('<div id="preloaded-log-')]+ '</template></turbo-stream>'
 row=parse_games(html)[0]
 assert row.display_status=='Unknown' and row.rating_stars is None
 assert not any((row.is_played,row.is_playing,row.is_backlog,row.is_wishlist,row.is_liked))

def test_typed_csv(tmp_path):
 import csv
 path=tmp_path/'typed.csv'
 write_csv(parse_games(sample()),str(path))
 with path.open(encoding='utf-8-sig',newline='') as f:row=next(csv.DictReader(f))
 assert row['is_playing']=='True' and row['is_played']=='False'
 assert row['rating_stars']=='4.0' and row['display_status']=='Playing'
