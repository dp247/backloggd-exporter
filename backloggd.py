"""Personal Backloggd exporter. Python 3.11+."""
import argparse
import asyncio
import csv
from datetime import datetime
import json
import os
import re
from dataclasses import asdict, dataclass
from http.cookiejar import MozillaCookieJar, LoadError
from pathlib import Path
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, SelectionList, Static

BASE = 'https://backloggd.com'
STATUSES = ('played', 'playing', 'backlog', 'wishlist')
PLATFORMS = json.loads(Path(__file__).with_name('data').joinpath('platforms.json').read_text(encoding='utf-8'))

class ExportError(Exception):
    pass

@dataclass
class Game:
    game_id: str
    title: str
    url: str
    platform_filter: str
    statuses: str
    rating_raw: str
    status_title: str
    log_id: str
    is_played: bool = False
    is_playing: bool = False
    is_backlog: bool = False
    is_wishlist: bool = False
    is_liked: bool = False
    played_status: str = ''
    played_timestamp: str = ''
    playing_timestamp: str = ''
    backlog_timestamp: str = ''
    wishlist_timestamp: str = ''
    liked_timestamp: str = ''
    rating_stars: float | None = None
    display_status: str = 'Unknown'

def find_preloaded_log(scope, game_id: str):
    """Find the hidden log beside the game card."""
    container = scope.find(id=f"preloaded-log-{game_id}")

    if container is None:
        return None

    # Support metadata directly on the container.
    if container.has_attr("data-log-id"):
        return container

    return container.find(attrs={"data-log-id": True})


def parse_games(html: str, platform: str = '') -> list[Game]:
    soup = BeautifulSoup(html, 'html.parser')
    streams = soup.select('turbo-stream[target="user_games"], turbo-stream[target="user-games-container"]')
    if not streams:
        raise ExportError('Expected game Turbo Stream missing. This may be a login/challenge page or changed markup.')
    games = []
    for stream in streams:
        template = stream.find('template')
        if template is None:
            raise ExportError('Game stream has no template.')
        # BeautifulSoup treats text inside templates as TemplateString; reparse the fragment.
        template = BeautifulSoup(template.decode_contents(), 'html.parser')
        for card in template.select('.game-cover[game_id]'):
            gid = card['game_id']
            title = card.select_one('.game-text-centered')
            link = card.select_one('a.cover-link[href]')
            if title is None or link is None:
                raise ExportError('Game card missing title or link; refusing incomplete export.')
            # Search the whole template, because the log is outside the card.
            log = find_preloaded_log(template, gid)

            # Fall back to the complete response.
            if log is None:
                log = find_preloaded_log(soup, gid)

            attrs = log.attrs if log is not None else {}
            states = [state for state, attr in [('played','data-is-play'), ('playing','data-is-playing'), ('backlog','data-is-backlog'), ('wishlist','data-is-wishlist')] if attrs.get(attr)]
            row = Game(gid, title.get_text(' ', strip=True), urljoin(BASE,link['href'] if link else ''), platform, '|'.join(states), str(attrs.get('data-rating','')), str(attrs.get('data-status-title','')), str(attrs.get('data-log-id','')))
            for state, attr in [('played', 'data-is-play'), ('playing', 'data-is-playing'), ('backlog', 'data-is-backlog'), ('wishlist', 'data-is-wishlist'), ('liked', 'data-is-liked')]:
                timestamp = str(attrs.get(attr, ''))
                setattr(row, f'{state}_timestamp', timestamp)
                setattr(row, f'is_{state}', bool(timestamp))
            row.played_status = row.status_title.strip().lower() if row.is_played else ''
            if row.rating_raw.isdigit() and 1 <= int(row.rating_raw) <= 10:
                row.rating_stars = int(row.rating_raw) / 2
            if row.is_playing:
                row.display_status = 'Playing'
            elif row.is_played:
                row.display_status = row.played_status.title() or 'Played'
            elif row.is_backlog:
                row.display_status = 'Backlog'
            elif row.is_wishlist:
                row.display_status = 'Wishlist'
            games.append(row)
        # Only an actually empty game payload is the end marker.
        if not games and template.get_text(strip=True):
            raise ExportError('No games found for the selected platform/statuses.')
    return games


def cookies_from_file(filename: str):
    jar = MozillaCookieJar()
    if not filename:
        return jar
    try:
        jar.load(
            str(Path(filename).expanduser()),
            ignore_discard=True,
            ignore_expires=True,
        )
    except (OSError, LoadError):
        raise ExportError(
            "Could not load Netscape cookie file. "
            "Check the path and export format."
        ) from None

    for cookie in list(jar):
        # Browser exporters commonly represent session cookies with 0.
        if cookie.expires == 0:
            cookie.expires = None
            cookie.discard = True

        if (
            cookie.is_expired()
            or cookie.domain.lstrip(".")
            not in ("backloggd.com", "www.backloggd.com")
        ):
            jar.clear(cookie.domain, cookie.path, cookie.name)

    if not list(jar):
        raise ExportError(
            "No usable Backloggd cookies found. "
            "Export a fresh cookie file."
        )
    return jar


def build_url(username: str, platform: str, statuses: list[str]) -> str:
    if not re.fullmatch(r'[A-Za-z0-9_-]+',username):
        raise ExportError('Enter a username containing letters, digits, underscores or hyphens.')
    if platform and platform not in PLATFORMS:
        raise ExportError('Unknown platform slug.')
    if not statuses or any(s not in STATUSES for s in statuses):
        raise ExportError('Select at least one valid status.')
    filters = (f'played_platform:{quote(platform, safe="")};' if platform else '') + 'type:' + ','.join(s for s in STATUSES if s in statuses)
    # Use the known backlog route, with the explicit type filter as in the supplied URL.
    return f'{BASE}/u/{username}/games/backlog/by/title:asc/{filters}'


async def fetch_games(username, platform, statuses, cookie_file='', progress=lambda page,count: None, *, transport=None, delay=0.8, max_pages=200):
    url = build_url(username,platform,statuses)
    cookies = cookies_from_file(cookie_file)
    result, seen = [], set()
    async with httpx.AsyncClient(cookies=cookies, timeout=30, follow_redirects=False, transport=transport,
        headers={
            'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Priority': 'u=0, i',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:157.0) Gecko/20100101 Firefox/157.0'}) as client:
        for page in range(1,max_pages+1):
            try:
                response = await client.get(url,params={'format':'turbo_stream','page':page})
            except httpx.RequestError:
                raise ExportError('Network request failed. Check your connection and try again.') from None
            if response.status_code in (401,403):
                raise ExportError('Backloggd blocked the request. Export fresh browser cookies; clearance may be browser/IP-bound.')
            if response.status_code == 429:
                raise ExportError('Backloggd rate limit reached. Wait before trying again.')
            if response.status_code != 200:
                raise ExportError(f'Backloggd returned HTTP {response.status_code}; no export written.')
            rows = parse_games(response.text,platform)
            if not rows:
                progress(page,len(result))
                return result
            new = [r for r in rows if r.game_id not in seen]
            if not new:
                raise ExportError('Pagination repeated a page; stopped to avoid an incomplete export.')
            for row in new:
                seen.add(row.game_id)
                result.append(row)
            progress(page,len(result))
            await asyncio.sleep(delay)
    raise ExportError('Page safety limit reached; stopped to avoid an incomplete export.')


def write_csv(rows: list[Game], filename: str):
    path = Path(filename).expanduser()
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f,fieldnames=list(Game.__dataclass_fields__))
        writer.writeheader()
        for row in rows:
            data = asdict(row)
            # Keep spreadsheet applications from evaluating scraped strings as formulas.
            writer.writerow({k: "'"+v if isinstance(v, str) and v.lstrip().startswith(('=','+','-','@')) else v for k,v in data.items()})
    return path.resolve()

class SettingsScreen(ModalScreen[dict[str, str] | None]):
    """Edit settings and return them when Apply is pressed."""

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
    }

    SettingsScreen #settings-dialog {
        width: 80;
        max-width: 95%;
        height: 24;
        max-height: 95%;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }

    SettingsScreen #settings-title {
        height: 2;
        text-style: bold;
    }

    SettingsScreen Label {
        width: 100%;
        height: 1;
    }

    SettingsScreen Input {
        width: 100%;
        height: 3;
    }

    SettingsScreen #settings-error {
        height: 2;
        color: $error;
    }

    SettingsScreen #settings-buttons {
        height: 3;
        align-horizontal: right;
    }

    SettingsScreen Button {
        margin-left: 1;
    }
    """

    def __init__(self, settings: dict):
        super().__init__()

        # Copy the values so Cancel leaves the original settings alone.
        self.initial_settings = settings.copy()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="settings-dialog"):
            yield Static("Settings", id="settings-title")

            yield Label("Username")
            yield Input(
                value=self.initial_settings["username"],
                placeholder="Enter your Backloggd username",
                id="settings-username",
            )

            yield Label("Cookie file")
            yield Input(
                value=self.initial_settings["cookies"],
                placeholder="Path to your Netscape cookie export",
                id="settings-cookies",
            )

            yield Label("Save location")
            yield Input(
                value=self.initial_settings["output"],
                placeholder="Save directory - defaults to the current working directory.",
                id="settings-save-location",
            )

            yield Static(
                "",
                id="settings-error",
                markup=False,
            )

            with Horizontal(id="settings-buttons"):
                yield Button("Cancel", id="settings-cancel")
                yield Button(
                    "Apply",
                    id="settings-apply",
                    variant="primary",
                )

    def on_mount(self):
        self.query_one("#settings-username", Input).focus()

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()

        if event.button.id == "settings-cancel":
            self.action_cancel()

        elif event.button.id == "settings-apply":
            values = {
                "username": self.query_one(
                    "#settings-username", Input
                ).value.strip(),
                "cookies": self.query_one(
                    "#settings-cookies", Input
                ).value.strip(),
                "output": self.query_one(
                    "#settings-save-location", Input
                ).value.strip(),
            }

            if not re.fullmatch(
                r"[A-Za-z0-9_-]+", values["username"]
            ):
                self.query_one("#settings-error", Static).update(
                    "Enter a valid username."
                )
                self.query_one("#settings-save-location", Input).focus()
                return

            if not values["output"]:
                values["output"] = os.getcwd()

            self.dismiss(values)

    def action_cancel(self):
        self.dismiss(None)


class Exporter(App):
    TITLE = 'Backloggd Exporter'
    BINDINGS = [
        ("ctrl+q", "quit", "Quit"),
        ("ctrl+l", "load", "Load games"),
        ("ctrl+s", "settings", "Settings"),
        ("escape", "cancel", "Cancel load"),
        ("f2", "sort_id", "Sort by ID"),
        ("f3", "sort_name", "Sort by name"),
        ("f4", "sort_stars", "Sort by rating"),
    ]
    CSS = """
    Screen {
        layout: vertical;
    }
    #main {
        height: 1fr;
        margin-top: 1;
    }
    #table-area {
        width: 2fr;
        height: 100%;
    }
    #games {
        height: 1fr;
    }
    #table-info {
        height: 3;
        padding: 1 1;
    }
    #message {
        width: 1fr;
        height: 1;
    }
    #game-count {
        width: 16;
        height: 1;
        text-align: right;
    }
    #sidebar {
        width: 1fr;
        min-width: 32;
        max-width: 44;
        height: 100%;
        padding: 0 1;
    }
    #filters {
        height: 1fr;
    }
    #sidebar Label {
        width: 100%;
        height: 1;
        margin-left: 1;
    }
    #status-label {
        margin-top: 1;
    }
    #platform {
        width: 100%;
        height: 3;
        margin-bottom: 1;
    }
    #statuses {
        width: 100%;
        height: 6;
        padding: 0 1;
    }
    #buttons {
        height: 6;
        margin-bottom: 1;
        grid-size: 2;
        grid-gutter: 0 1;
        grid-rows: 3;
    }
    #buttons Button {
        width: 100%;
        min-width: 12;
    }
    """
    def __init__(self,settings):
        super().__init__()
        self.settings = settings
        self.rows = []
        self.busy = False
        self.sort_column = "game_id"
        self.sort_reverse = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="table-area"):
                yield DataTable(id="games")
                with Horizontal(id="table-info"):
                    yield Static(
                        "Please select a platform and statuses, then load games.",
                        id="message",
                        markup=False,
                    )
                    yield Static("0 games", id="game-count")

            with Vertical(id="sidebar"):
                with VerticalScroll(id="filters"):
                    yield Label("Platform")
                    yield Select(
                        [
                            ("Any platform", ""),
                            *[
                                (label, slug)
                                for slug, label in sorted(
                                    PLATFORMS.items(),
                                    key=lambda item: item[1].casefold(),
                                )
                            ],
                        ],
                        value=self.settings["platform"],
                        allow_blank=False,
                        id="platform",
                    )
                    yield Label("Status", id="status-label")
                    yield SelectionList(
                        *[
                            (
                                status.title(),
                                status,
                                status in self.settings["statuses"],
                            )
                            for status in STATUSES
                        ],
                        id="statuses",
                    )
                with Grid(id="buttons"):
                    yield Button("Load games", id="load", variant="primary")
                    yield Button("Export CSV", id="export", disabled=True, variant="success")
                    yield Button("Cancel load", id="cancel", disabled=True, variant="error")
                    yield Button("Settings", id="settings")

        yield Footer()

    def on_mount(self):
        self.title = self.TITLE
        table = self.query_one("#games", DataTable)
        table.cursor_type = "row"

        table.add_column("Game ID", key="game_id")
        table.add_column("Name", key="name")
        table.add_column("Status", key="status")
        table.add_column("Stars", key="stars")
        table.add_column("URL", key="url")

    def populate_table(self):
        table = self.query_one("#games", DataTable)
        table.clear()

        for game in self.rows:
            table.add_row(
                game.game_id,
                game.title,
                game.display_status,
                (
                    ""
                    if game.rating_stars is None
                    else f"{game.rating_stars:g}"
                ),
                game.url,
                key=game.game_id,
            )

        self.apply_table_sort()

    def update_static(self, element, text):
        self.query_one(f'#{element}', Static).update(text)

    def on_button_pressed(self,event):
        if event.button.id == 'load': self.action_load()
        elif event.button.id == 'cancel': self.action_cancel()
        elif event.button.id == 'export':
            try:
                platform = self.query_one("#platform", Select).value
                platform_name = platform or "all"

                csv_name = (
                    f"backloggd_export_{platform_name.replace('--','')}_"
                    f"{datetime.now():%Y%m%d_%H%M%S}.csv"
                )

                save_path = os.path.join(self.settings["output"], csv_name)
                path = write_csv(self.rows, save_path)
                self.update_static('message', f'Exported {len(self.rows)} games to {path}')
            except (OSError,ExportError) as exc:
                self.update_static('message', str(exc))
        elif event.button.id == "settings":
            self.action_settings()

    def action_load(self):
        if self.busy: return
        self.rows = []
        self.query_one(DataTable).clear()
        self.update_game_count(0)
        self.query_one('#export',Button).disabled = True
        self.load_rows()

    def action_cancel(self):
        if self.busy:
            self.workers.cancel_all()
            self.update_static('message', 'Load cancelled.')

    def action_settings(self):
        if self.busy:
            self.notify("Finish or cancel the current load before changing settings.", severity="warning")
            return

        self.push_screen(
            SettingsScreen(self.settings),
            self.apply_settings,
        )


    def apply_settings(self, values: dict[str, str] | None):
        if values is None:
            return

        source_changed = (
            values["username"] != self.settings["username"]
            or values["cookies"] != self.settings["cookies"]
        )

        self.settings.update(values)

        if source_changed:
            # Avoid exporting an old account's results after changing source.
            self.rows = []
            self.query_one("#games", DataTable).clear()
            self.query_one("#export", Button).disabled = True

            self.notify("Settings applied. Load games again.")
        else:
            self.notify("Settings applied.")

    def update_game_count(self, count: int):
        noun = "game" if count == 1 else "games"
        self.update_static('game-count', f"{count:,} {noun} loaded")

    @work(exclusive=True)
    async def load_rows(self):
        self.busy = True
        self.query_one('#load',Button).disabled = True
        self.query_one('#cancel',Button).disabled = False
        try:
            username = self.settings["username"]
            platform = str(self.query_one('#platform',Select).value)
            statuses = list(self.query_one('#statuses',SelectionList).selected)
            self.update_static('message', 'Loading…')
            self.rows = await fetch_games(username,platform,statuses,self.settings["cookies"].strip(),lambda page,count: self.update_static('message', f'Page {page}: {count} games loaded…'))
            self.populate_table()
            self.query_one('#export',Button).disabled = False
            self.update_game_count(len(self.rows))
            self.update_static('message', f'Load complete. Click "Export CSV" to save the games')
            self.notify(f'{len(self.rows)} games loaded.')
        except (ExportError,OSError) as exc:
            self.update_static('message', str(exc)) 
        finally:
            self.busy = False
            self.query_one('#load',Button).disabled = False
            self.query_one('#cancel',Button).disabled = True

    def sort_table(self, column: str, reverse: bool | None = None):
        """Choose a column, or toggle its direction if already selected."""
        if column not in ("game_id", "name", "stars"):
            return

        if reverse is None:
            if column == self.sort_column:
                reverse = not self.sort_reverse
            else:
                reverse = False

        self.sort_column = column
        self.sort_reverse = reverse

        self.apply_table_sort()


    def apply_table_sort(self):
        table = self.query_one("#games", DataTable)

        if self.sort_column == "game_id":
            table.sort(
                "game_id",
                key=lambda value: int(value),
                reverse=self.sort_reverse,
            )

        elif self.sort_column == "name":
            table.sort(
                "name",
                key=lambda value: str(value).casefold(),
                reverse=self.sort_reverse,
            )

        elif self.sort_column == "stars":
            def star_key(value):
                if value == "":
                    return (True, 0.0)

                rating = float(value)

                return (
                    False,
                    -rating if self.sort_reverse else rating,
                )

            table.sort("stars", key=star_key)

    def on_data_table_header_selected(
        self,
        event: DataTable.HeaderSelected,
    ):
        column = event.column_key.value

        if column in ("game_id", "name", "stars"):
            self.sort_table(column)

    def action_sort_id(self):
        self.sort_table("game_id")


    def action_sort_name(self):
        self.sort_table("name")


    def action_sort_stars(self):
        self.sort_table("stars")

def main():
    config_default = os.path.join(os.getcwd(),'config', 'config.json')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=config_default,help='Optional JSON settings file')
    parser.add_argument('--username')
    parser.add_argument('--cookies',help='Path to Netscape cookie export')
    parser.add_argument('--platform',choices=['']+list(PLATFORMS))
    parser.add_argument('--statuses',help='Comma-separated statuses')
    parser.add_argument('--output')
    parser.add_argument('--cli',action='store_true',help='Export without the TUI')
    args = parser.parse_args()
    settings = {'username':'','cookies':'','platform':'','statuses':['played','playing','backlog','wishlist'],'output':''}
    try:
        config = Path(args.config)
        if config.exists():
            settings.update(json.loads(config.read_text(encoding='utf-8')))
        settings['cookies'] = os.getenv('BACKLOGGD_COOKIES_FILE',settings['cookies'])
        for key in ('username','cookies','platform','output'):
            if getattr(args,key) is not None: settings[key] = getattr(args,key)
        if args.statuses is not None: settings['statuses'] = args.statuses.split(',')
        build_url(settings['username'],settings['platform'],settings['statuses'])
        if args.cli:
            rows = asyncio.run(fetch_games(settings['username'],settings['platform'],settings['statuses'],settings['cookies']))
            print(f'Exported {len(rows)} games to {write_csv(rows,settings["output"])}')
        else:
            Exporter(settings).run()
    except (ExportError,OSError,ValueError,TypeError,KeyError) as exc:
        parser.exit(1,f'Error: {exc}\n')

if __name__ == '__main__':
    main()
