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
