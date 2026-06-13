# 2026 World Cup Daily Hub — Group Stage Production Calendar

All times **Eastern (ET)**. 72 matches, June 11–27, no rest days. Each ET date below is one "edition" of the daily hub.

**A note on the three midnight games:** Three matches kick off at 9–10 p.m. Pacific/Monterrey time, which is 12:00 a.m. ET the *next* calendar day. The fixtures CSV records their true ET datetime, but for editorial purposes they belong to the previous evening's slate (you'd watch Australia–Türkiye on "Saturday night, June 13"). They're shown below attached to their natural slate with a 🌙 marker.

**Team-name canon (for model joins):** Mexico, South Africa, South Korea, Czechia, Canada, Bosnia and Herzegovina, Qatar, Switzerland, Brazil, Morocco, Haiti, Scotland, United States, Paraguay, Australia, Türkiye, Germany, Curaçao, Côte d'Ivoire, Ecuador, Netherlands, Japan, Sweden, Tunisia, Belgium, Egypt, Iran, New Zealand, Spain, Cape Verde, Saudi Arabia, Uruguay, France, Senegal, Iraq, Norway, Argentina, Algeria, Austria, Jordan, Portugal, DR Congo, Uzbekistan, Colombia, England, Croatia, Ghana, Panama. Your aggregate ratings file should use these exact strings (matching the project knowledge base) so everything joins against `data/fixtures.csv` cleanly.

---

## Matchday 1 — June 11–17 (every team's opener)

**Thu June 11 — 2 matches (Opening Day)**
- 3:00 PM — ✅ Mexico 2–0 South Africa (Group A) — Estadio Azteca, Mexico City
- 10:00 PM (FS1) — South Korea vs Czechia (A) — Estadio Akron, Guadalajara

**Fri June 12 — 2 matches (host openers)**
- 3:00 PM (Fox) — Canada vs Bosnia and Herzegovina (B) — BMO Field, Toronto
- 9:00 PM (Fox) — United States vs Paraguay (D) — SoFi Stadium, Inglewood

**Sat June 13 — 4 matches**
- 3:00 PM (Fox) — Qatar vs Switzerland (B) — Levi's Stadium, Santa Clara
- 6:00 PM (FS1) — Brazil vs Morocco (C) — MetLife Stadium, East Rutherford ⭐ *headliner: a genuine heavyweight opener*
- 9:00 PM (FS1) — Haiti vs Scotland (C) — Gillette Stadium, Foxborough
- 🌙 12:00 AM (FS1) — Australia vs Türkiye (D) — BC Place, Vancouver

**Sun June 14 — 4 matches**
- 1:00 PM (Fox) — Germany vs Curaçao (E) — NRG Stadium, Houston
- 4:00 PM (Fox) — Netherlands vs Japan (F) — AT&T Stadium, Arlington ⭐
- 7:00 PM (FS1) — Côte d'Ivoire vs Ecuador (E) — Lincoln Financial Field, Philadelphia
- 10:00 PM (FS1) — Sweden vs Tunisia (F) — Estadio BBVA, Monterrey

**Mon June 15 — 4 matches**
- 12:00 PM (Fox) — Spain vs Cape Verde (H) — Mercedes-Benz Stadium, Atlanta *(tournament favorite debuts)*
- 3:00 PM (Fox) — Belgium vs Egypt (G) — Lumen Field, Seattle
- 6:00 PM (FS1) — Saudi Arabia vs Uruguay (H) — Hard Rock Stadium, Miami Gardens
- 9:00 PM (FS1) — Iran vs New Zealand (G) — SoFi Stadium, Inglewood

**Tue June 16 — 4 matches**
- 3:00 PM (Fox) — France vs Senegal (I) — MetLife Stadium, East Rutherford ⭐ *headliner: hardest group's marquee opener*
- 6:00 PM (Fox) — Iraq vs Norway (I) — Gillette Stadium, Foxborough
- 9:00 PM (Fox) — Argentina vs Algeria (J) — Arrowhead Stadium, Kansas City *(champions debut)*
- 🌙 12:00 AM (FS1) — Austria vs Jordan (J) — Levi's Stadium, Santa Clara

**Wed June 17 — 4 matches**
- 1:00 PM (Fox) — Portugal vs DR Congo (K) — NRG Stadium, Houston
- 4:00 PM (Fox) — England vs Croatia (L) — AT&T Stadium, Arlington ⭐ *headliner: biggest MD1 fixture on paper*
- 7:00 PM (FS1) — Ghana vs Panama (L) — BMO Field, Toronto
- 10:00 PM (FS1) — Uzbekistan vs Colombia (K) — Estadio Azteca, Mexico City

## Matchday 2 — June 18–23

**Thu June 18 — 4 matches:** Czechia–South Africa 12pm (Atlanta), Switzerland–Bosnia 3pm (Inglewood), Canada–Qatar 6pm (Vancouver), Mexico–South Korea 9pm (Guadalajara)

**Fri June 19 — 4 matches:** United States–Australia 3pm (Seattle), Scotland–Morocco 6pm (Foxborough), Brazil–Haiti 8:30pm (Philadelphia), Türkiye–Paraguay 11pm (Santa Clara)

**Sat June 20 — 4 matches:** Netherlands–Sweden 1pm (Houston), Germany–Côte d'Ivoire 4pm (Toronto), Ecuador–Curaçao 8pm (Kansas City), 🌙 Tunisia–Japan 12am (Monterrey) *(re-verify kickoff before this edition)*

**Sun June 21 — 4 matches:** Spain–Saudi Arabia 12pm (Atlanta), Belgium–Iran 3pm (Inglewood), Uruguay–Cape Verde 6pm (Miami Gardens), New Zealand–Egypt 9pm (Vancouver)

**Mon June 22 — 4 matches:** Argentina–Austria 1pm (Arlington), France–Iraq 5pm (Philadelphia), Norway–Senegal 8pm (East Rutherford) ⭐, Jordan–Algeria 11pm (Santa Clara)

**Tue June 23 — 4 matches:** Portugal–Uzbekistan 1pm (Houston), England–Ghana 4pm (Foxborough), Panama–Croatia 7pm (Toronto), Colombia–DR Congo 10pm (Guadalajara)

## Matchday 3 — June 24–27 (six games/day; same-group games simultaneous)

These are the heavy-production days: six matches each, in three simultaneous pairs. This is where the **qualification-scenarios section becomes the lead story** — each pair decides a group, and the eight-best-thirds race spans all groups.

**Wed June 24 — Groups A, B, C decided:** Switzerland–Canada + Bosnia–Qatar (3pm), Scotland–Brazil + Morocco–Haiti (6pm), Czechia–Mexico + South Africa–South Korea (9pm)

**Thu June 25 — Groups D, E, F decided:** Curaçao–Côte d'Ivoire + Ecuador–Germany (4pm), Japan–Sweden + Tunisia–Netherlands (7pm), Türkiye–United States + Paraguay–Australia (10pm)

**Fri June 26 — Groups G, H, I decided:** Norway–France + Senegal–Iraq (3pm), Cape Verde–Saudi Arabia + Uruguay–Spain (8pm) ⭐, Egypt–Iran + New Zealand–Belgium (11pm)

**Sat June 27 — Groups J, K, L decided:** Panama–England + Croatia–Ghana (5pm), Colombia–Portugal + DR Congo–Uzbekistan (7:30pm), Jordan–Argentina + Algeria–Austria (10pm)

---

## Production cadence notes

- **Lightest days:** June 11–12 (2 games each) — good runway to refine the format before volume ramps.
- **Heaviest days:** June 24–27 (6 games each, 24 games in 4 days) — match cards for these should be pre-baked well in advance so edition day is only standings math + scenario writing + news checks.
- **Daily edition workflow:** (1) update results/standings in the CSV from the prior day, (2) web-check overnight team news for today's teams, (3) pull pre-baked match cards, (4) refresh predictions, (5) write the "stakes" sections, which can't be pre-baked.
- **Third-place tracker** should debut around June 18 (start of MD2) and become the centerpiece June 22–27.

## Sources & verification

Schedule compiled June 11, 2026 from Yahoo Sports' full schedule (cross-checked against ESPN, NBC Sports, Al Jazeera, and Sky Sports). The three midnight-ET kickoffs were independently verified: Australia–Türkiye confirmed at 9:00 PM PT June 13 via Football Australia (socceroos.com.au) and Destination Vancouver; Austria–Jordan's 12am ET June 17 slot is consistent across Yahoo and Sky Sports' UK-time listing; Tunisia–Japan (12am ET June 21) follows the same pattern but should be re-verified before that edition. Internal consistency check passed: all 12 groups' matchday-3 pairs share identical kickoff times, as FIFA's simultaneity rule requires.
