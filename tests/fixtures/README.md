# Public bookmaker reference data

`bookmakers_public_sample.json` is a minimal projection of the user's
`bookmakers-capture-20261108-121029.json`, captured on 2026-11-08 at 09:10–09:13 UTC.
It contains six public DOM observations: BetBoom homepage, EPL listing and match
detail, and Winline homepage observations. Only visible sports text and public
event links are retained; account, media, tracking, headers and unrelated JSON
are not included. Footers and unrelated sidebar content were removed.

These are recorded observations, not generated matches or current odds. Tests
use a fixed clock. They verify decoding this captured layout, not continued
accessibility or complete coverage of the live bookmaker sites.

`winline_live_report.json` retains the two public card examples printed by
the user's server diagnostic on 2026-11-08. Both are rejected by pre-match
parsing: the first explicitly shows an in-play clock; the second has no
kickoff timestamp. The printed report did not include an observation time,
so none is invented here. Tests supply their own clock and mark constructed
future-card variants as synthetic; these prices are not claimed as recorded
pre-match prices.

`winline_market_projection.json` projects a few labels, periods and prices
from the user's successful Sunderland–Chelsea event report. The canonical
URL and observed CSS class names come from that report. Browser tests build
synthetic HTML around this data and use a future test kickoff; this is not
a captured HTML document or evidence that every current market is parsed.

`multisport_projection.json` — синтетические контракты футбола, хоккея, тенниса
и гонок. Slug разделов взят из публичной навигации в пользовательском снимке.
Рынки и коэффициенты служат только тестовыми входами, не являются текущими
наблюдениями сайта. Даты и признаки полноты добавляются тестовым кодом.
